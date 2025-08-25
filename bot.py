#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Safe SPEAKIFY bot entrypoint.
This version **does not** include any hardcoded API keys.
Use a .env file (or environment variables) to set BOT_TOKEN and OPENAI_API_KEY.
"""

import os
import sqlite3
import logging
import datetime
import math
import re
import time
from enum import Enum, auto

from dotenv import load_dotenv
load_dotenv()

import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI, RateLimitError, APIError

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Bot & Admin Configuration (from env) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
QUESTIONS_PER_PAGE = int(os.getenv("QUESTIONS_PER_PAGE", "5"))
BROADCAST_DELAY_SECONDS = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.1"))
MAX_VOICE_DURATION_SECONDS = int(os.getenv("MAX_VOICE_DURATION_SECONDS", "180"))
DB_NAME = os.getenv("DB_NAME", "ielts_questions.db")

if not BOT_TOKEN:
    logging.critical("CRITICAL ERROR: BOT_TOKEN is not set. Exiting.")
    raise SystemExit(1)

if not OPENAI_API_KEY:
    logging.warning("WARNING: OPENAI_API_KEY is not set. AI features will not work.")
    openai_client = None
else:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

bot = telebot.TeleBot(BOT_TOKEN)

class AdminState(Enum):
    NONE = auto()
    IN_ADMIN_PANEL = auto()
    SELECT_ADD_CATEGORY = auto()
    SELECT_DELETE_CATEGORY = auto()
    SELECT_LIST_CATEGORY = auto()
    AWAITING_BROADCAST_MESSAGE = auto()
    AWAITING_ADD_PART1 = auto()
    AWAITING_ADD_PART2 = auto()
    AWAITING_ADD_PART3 = auto()
    AWAITING_DELETE_ID_PART1 = auto()
    AWAITING_DELETE_ID_PART2 = auto()
    AWAITING_DELETE_ID_PART3 = auto()

class UserState(Enum):
    MAIN_MENU = auto()
    LISTING_MENU = auto()
    AWAITING_ADMIN_MESSAGE = auto()
    AWAITING_VOICE_ANSWER = auto()

ADMIN_STATES = {}
USER_STATES = {}
USER_CURRENT_QUESTION = {}

# --- Database Layer ---
def execute_db_query(query, params=(), fetch=None):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if fetch == "one": return cursor.fetchone()
            if fetch == "all": return cursor.fetchall()
            conn.commit()
            return cursor.rowcount
    except sqlite3.IntegrityError:
        logging.warning(f"Database IntegrityError on query '{query[:30]}...'. Likely a duplicate entry.")
        return 0
    except sqlite3.Error as e:
        logging.error(f"Database error on query '{query[:30]}...': {e}")
        return None if fetch else -1

def create_database():
    execute_db_query('CREATE TABLE IF NOT EXISTS part1_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL UNIQUE)')
    execute_db_query('CREATE TABLE IF NOT EXISTS part2_topics (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL UNIQUE)')
    execute_db_query('CREATE TABLE IF NOT EXISTS part3_discussions (id INTEGER PRIMARY KEY AUTOINCREMENT, discussion TEXT NOT NULL UNIQUE)')
    execute_db_query('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE NOT NULL, first_seen DATETIME, last_interaction DATETIME)')
    logging.info("Database tables checked/created successfully.")

def insert_sample_data():
    sample_data = {
        "part1_questions": [(f"Sample Part 1 Question {i}",) for i in range(1, 15)],
        "part2_topics": [(f"Sample Part 2 Topic {i}",) for i in range(1, 15)],
        "part3_discussions": [(f"Sample Part 3 Discussion {i}",) for i in range(1, 15)]
    }
    for table, data in sample_data.items():
        count = execute_db_query(f"SELECT COUNT(*) FROM {table}", fetch="one")
        if count and count[0] == 0:
            column = "question" if table == "part1_questions" else "topic" if table == "part2_topics" else "discussion"
            for item in data:
                execute_db_query(f"INSERT OR IGNORE INTO {table} ({column}) VALUES (?)", item)
            logging.info(f"Inserted sample data into {table}.")

def get_random_question(table_name):
    column = {"part1_questions": "question", "part2_topics": "topic", "part3_discussions": "discussion"}.get(table_name)
    if not column: return "Invalid category."
    item = execute_db_query(f"SELECT {column} FROM {table_name} ORDER BY RANDOM() LIMIT 1", fetch="one")
    return item[0] if item else "No questions found."

def get_question_by_id(table_name, question_id):
    column = {"part1_questions": "question", "part2_topics": "topic", "part3_discussions": "discussion"}.get(table_name)
    if not column: return "Invalid category."
    item = execute_db_query(f"SELECT {column} FROM {table_name} WHERE id = ?", (question_id,), fetch="one")
    return item[0] if item else f"No item found with ID {question_id}."

def add_question_to_db(table_name, text):
    text = text.strip()
    if not text:
        return False, "Input cannot be empty."
    column = {"part1_questions": "question", "part2_topics": "topic", "part3_discussions": "discussion"}.get(table_name)
    if not column: return False, "Invalid table name."
    rowcount = execute_db_query(f"INSERT OR IGNORE INTO {table_name} ({column}) VALUES (?)", (text,))
    if rowcount is not None and rowcount > 0: return True, "Question added successfully!"
    elif rowcount == 0: return False, "Question already exists."
    else: return False, "An error occurred."

def delete_question_from_db(table_name, question_id):
    rowcount = execute_db_query(f"DELETE FROM {table_name} WHERE id = ?", (question_id,))
    if rowcount is not None and rowcount > 0: return True, "Question deleted successfully!"
    elif rowcount == 0: return False, "Question ID not found."
    else: return False, "An error occurred."

def get_all_questions(table_name):
    column = {"part1_questions": "question", "part2_topics": "topic", "part3_discussions": "discussion"}.get(table_name)
    if not column: return []
    return execute_db_query(f"SELECT id, {column} FROM {table_name} ORDER BY id", fetch="all") or []

def get_item_count(table_name):
    result = execute_db_query(f"SELECT COUNT(id) FROM {table_name}", fetch="one")
    return result[0] if result else -1

# --- User Analytics ---
def add_or_update_user_activity(chat_id):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = execute_db_query("SELECT id FROM users WHERE chat_id = ?", (chat_id,), fetch="one")
    if user:
        execute_db_query("UPDATE users SET last_interaction = ? WHERE chat_id = ?", (now, chat_id))
    else:
        execute_db_query("INSERT INTO users (chat_id, first_seen, last_interaction) VALUES (?, ?, ?)", (chat_id, now, now))
        logging.info(f"Added new unique user: {chat_id}")

def get_user_counts(days=None):
    query = "SELECT COUNT(DISTINCT chat_id) FROM users"
    if days: query += f" WHERE last_interaction >= datetime('now', '-{days} days')"
    result = execute_db_query(query, fetch="one")
    return result[0] if result else -1

def get_all_user_chat_ids():
    results = execute_db_query("SELECT chat_id FROM users", fetch="all")
    return [r[0] for r in results] if results else []

# --- OpenAI Helper Function ---
def get_ielts_feedback(question, transcript):
    if not openai_client:
        return "OpenAI client is not configured. Cannot provide feedback."

    prompt = f\"\"\"You are a friendly and encouraging IELTS speaking coach. Provide concise feedback.\"\"\"
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a friendly IELTS speaking coach."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content
    except RateLimitError:
        logging.error("OpenAI RateLimitError encountered.")
        return "I'm experiencing high demand right now. Please try again in a moment."
    except APIError as e:
        logging.error(f"OpenAI APIError encountered: {e}")
        return "I'm having trouble connecting to my analysis tools. Please try again later."
    except Exception as e:
        logging.error(f"Unexpected error while getting feedback from OpenAI: {e}")
        return "Sorry, I encountered an unexpected error while analyzing your answer. Please try again."

# --- UI & Pagination Helpers ---
def create_pagination_keyboard(page, total_pages, context):
    keyboard = InlineKeyboardMarkup()
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page-1}_{context}"))
    if page < total_pages:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}_{context}"))
    keyboard.add(*row)
    return keyboard

def send_paginated_list(chat_id, table_name, part_name, page=1, message_id=None):
    questions = get_all_questions(table_name)
    if not questions:
        bot.send_message(chat_id, f"No {part_name} found.")
        return

    total_pages = math.ceil(len(questions) / QUESTIONS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start_index = (page - 1) * QUESTIONS_PER_PAGE
    end_index = start_index + QUESTIONS_PER_PAGE
    page_questions = questions[start_index:end_index]

    header = f"📋 **{part_name}** (Page {page}/{total_pages}):\n\n"
    lines = [f"**ID: {q[0]}** - {q[1]}" for q in page_questions]
    text = header + "\n\n".join(lines)

    context = f"{table_name}"
    keyboard = create_pagination_keyboard(page, total_pages, context)

    try:
        if message_id:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in e.description:
            logging.error(f"Error sending/editing paginated list: {e}")

def send_part_selection_menu(chat_id, text, back_button_text):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton("Part 1"), KeyboardButton("Part 2"), KeyboardButton("Part 3"))
    markup.add(KeyboardButton(back_button_text))
    bot.send_message(chat_id, text, reply_markup=markup)

def send_admin_menu(chat_id):
    ADMIN_STATES[chat_id] = AdminState.IN_ADMIN_PANEL
    USER_STATES.pop(chat_id, None)
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("➕ Add Question"), KeyboardButton("➖ Delete Question"))
    markup.add(KeyboardButton("📄 List Questions"), KeyboardButton("📊 User Statistics"))
    markup.add(KeyboardButton("📢 Broadcast"))
    markup.add(KeyboardButton("⬅️ Back to Main"))
    bot.send_message(chat_id, "Welcome to the Admin Panel!", reply_markup=markup)

# --- Handlers ---
@bot.message_handler(commands=['start'])
def start_command(message):
    add_or_update_user_activity(message.chat.id)
    ADMIN_STATES.pop(message.chat.id, None)
    USER_STATES[message.chat.id] = UserState.MAIN_MENU
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("1️⃣ Part 1"), KeyboardButton("2️⃣ Part 2"), KeyboardButton("3️⃣ Part 3"))
    markup.add(KeyboardButton("📜 List All Questions"), KeyboardButton("💬 Chat with Admin"))
    bot.send_message(message.chat.id, "Welcome to the **SPEAKIFY BOT**! 🤖\n\nSelect a part to get a random practice question.", reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['admin'])
def admin_command(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "⛔ Unauthorized.")
        return
    logging.info(f"Admin {message.from_user.id} entered admin panel.")
    send_admin_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('page_'))
def pagination_handler(call):
    try:
        _, page_str, table_name = call.data.split('_', 2)
        page = int(page_str)
        part_map = {"part1_questions": "Part 1 Questions", "part2_topics": "Part 2 Topics", "part3_discussions": "Part 3 Discussions"}
        part_name = part_map.get(table_name, "Questions")
        send_paginated_list(call.message.chat.id, table_name, part_name, page, call.message.message_id)
    except (ValueError, IndexError) as e:
        logging.error(f"Invalid callback data format: {call.data}, error: {e}")
    finally:
        bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('random_') or call.data.startswith('aicheck_'))
def handle_question_buttons(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data.split('_', 1)

    if data[0] == 'random':
        table_name = data[1]
        part_number = {"part1_questions": "1", "part2_topics": "2", "part3_discussions": "3"}.get(table_name, "")
        question = get_random_question(table_name)
        USER_CURRENT_QUESTION[chat_id] = question

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Get Another Question", callback_data=f"random_{table_name}"), InlineKeyboardButton("🤖 AI Check", callback_data=f"aicheck_{table_name}"))

        bot.edit_message_text(f"💬 **Part {part_number} Question:**\n\n{question}",
                              chat_id,
                              message_id,
                              reply_markup=keyboard,
                              parse_mode='Markdown')

    elif data[0] == 'aicheck':
        USER_STATES[chat_id] = UserState.AWAITING_VOICE_ANSWER
        markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(KeyboardButton("❌ Cancel"))
        bot.send_message(chat_id, f"🎤 **AI Examiner is ready!**\n\nPlease send me a **voice message** with your answer (max {int(MAX_VOICE_DURATION_SECONDS / 60)} minutes).\n\nI will analyze it and give you direct feedback and a model answer. Press '❌ Cancel' to return to the main menu.", reply_markup=markup, parse_mode='Markdown')

    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda msg: msg.from_user.id in ADMIN_IDS and re.match(r'^\d+:\d+$', msg.text.strip()))
def handle_admin_get_question_by_id(message):
    chat_id = message.chat.id
    try:
        part_str, q_id_str = message.text.strip().split(':', 1)
        part, q_id = int(part_str), int(q_id_str)

        part_map = {1: "part1_questions", 2: "part2_topics", 3: "part3_discussions"}
        table_name = part_map.get(part)
        if not table_name:
            bot.send_message(chat_id, f"Invalid part number: {part}. Please use 1, 2, or 3.")
            return

        question = get_question_by_id(table_name, q_id)
        part_name = {1: "Part 1 Question", 2: "Part 2 Topic", 3: "Part 3 Discussion"}.get(part)

        if "No item found" in question:
            bot.send_message(chat_id, f"⚠️ {question}")
        else:
            bot.send_message(chat_id, f"💬 **{part_name} (ID: {q_id})**:\n\n{question}", parse_mode='Markdown')

    except (ValueError, IndexError):
        bot.send_message(chat_id, "Invalid format. Please use `part:id` (e.g., `1:15`).")
    except Exception as e:
        logging.error(f"Error in handle_admin_get_question_by_id: {e}")
        bot.send_message(chat_id, "An unexpected error occurred.")

@bot.message_handler(func=lambda msg: ADMIN_STATES.get(msg.from_user.id) == AdminState.IN_ADMIN_PANEL)
def handle_admin_menu(message):
    user_id = message.from_user.id
    text = message.text.strip()
    action_map = {
        "➕ Add Question": AdminState.SELECT_ADD_CATEGORY,
        "➖ Delete Question": AdminState.SELECT_DELETE_CATEGORY,
        "📄 List Questions": AdminState.SELECT_LIST_CATEGORY,
    }
    if text in action_map:
        ADMIN_STATES[user_id] = action_map[text]
        send_part_selection_menu(user_id, f"Which part to {text.split(' ')[1].lower()}?", "⬅️ Admin Menu")
    elif text == "📊 User Statistics":
        show_user_stats(message)
    elif text == "📢 Broadcast":
        ADMIN_STATES[user_id] = AdminState.AWAITING_BROADCAST_MESSAGE
        bot.send_message(user_id, "Send the message you want to broadcast (text, photo, etc.).", reply_markup=ForceReply())
    elif text == "⬅️ Back to Main":
        start_command(message)
    else:
        bot.send_message(user_id, "Invalid option. Please use the menu or send a request like `1:25`.")

def show_user_stats(message):
    stats = (
        f"📊 **Bot Statistics**\n\n"
        f"👥 Total Users: **{get_user_counts()}**\n"
        f"☀️ DAU: **{get_user_counts(1)}** | 🗓️ WAU: **{get_user_counts(7)}** | 📅 MAU: **{get_user_counts(30)}**\n\n"
        f"**--- Content ---**\n"
        f"1️⃣ Part 1: **{get_item_count('part1_questions')}**\n"
        f"2️⃣ Part 2: **{get_item_count('part2_topics')}**\n"
        f"3️⃣ Part 3: **{get_item_count('part3_discussions')}**"
    )
    bot.send_message(message.chat.id, stats, parse_mode='Markdown')
    send_admin_menu(message.chat.id)

@bot.message_handler(func=lambda msg: ADMIN_STATES.get(msg.from_user.id) in [
    AdminState.SELECT_ADD_CATEGORY, AdminState.SELECT_DELETE_CATEGORY, AdminState.SELECT_LIST_CATEGORY
])
def handle_admin_category_selection(message):
    user_id, state, part = message.from_user.id, ADMIN_STATES.get(message.from_user.id), message.text.strip()
    if part == "⬅️ Admin Menu":
        send_admin_menu(user_id)
        return

    part_map = {"Part 1": "part1_questions", "Part 2": "part2_topics", "Part 3": "part3_discussions"}
    table_name = part_map.get(part)
    if not table_name:
        bot.send_message(user_id, "Invalid part.")
        return

    if state == AdminState.SELECT_LIST_CATEGORY:
        send_paginated_list(user_id, table_name, f"{part} Questions")
        send_admin_menu(user_id)
    elif state == AdminState.SELECT_ADD_CATEGORY:
        state_map = {"Part 1": AdminState.AWAITING_ADD_PART1, "Part 2": AdminState.AWAITING_ADD_PART2, "Part 3": AdminState.AWAITING_ADD_PART3}
        ADMIN_STATES[user_id] = state_map[part]
        bot.send_message(user_id, f"Send the new text for **{part}**.", reply_markup=ForceReply(), parse_mode='Markdown')
    elif state == AdminState.SELECT_DELETE_CATEGORY:
        state_map = {"Part 1": AdminState.AWAITING_DELETE_ID_PART1, "Part 2": AdminState.AWAITING_DELETE_ID_PART2, "Part 3": AdminState.AWAITING_DELETE_ID_PART3}
        ADMIN_STATES[user_id] = state_map[part]
        send_paginated_list(user_id, table_name, f"{part} Questions")
        bot.send_message(user_id, f"Send the **ID** of the item to delete from **{part}**.", reply_markup=ForceReply(), parse_mode='Markdown')

@bot.message_handler(func=lambda msg:
                     ADMIN_STATES.get(msg.from_user.id) is not None and
                     "AWAITING" in ADMIN_STATES.get(msg.from_user.id).name and
                     ADMIN_STATES.get(msg.from_user.id) != AdminState.AWAITING_BROADCAST_MESSAGE)
def handle_admin_input(message):
    user_id, state, text = message.from_user.id, ADMIN_STATES.get(message.from_user.id), message.text.strip()
    table_map = {
        AdminState.AWAITING_ADD_PART1: "part1_questions", AdminState.AWAITING_DELETE_ID_PART1: "part1_questions",
        AdminState.AWAITING_ADD_PART2: "part2_topics", AdminState.AWAITING_DELETE_ID_PART2: "part2_topics",
        AdminState.AWAITING_ADD_PART3: "part3_discussions", AdminState.AWAITING_DELETE_ID_PART3: "part3_discussions"
    }
    table_name = table_map.get(state)

    if "AWAITING_ADD" in state.name:
        _, msg = add_question_to_db(table_name, text)
        bot.send_message(user_id, msg)
    elif "AWAITING_DELETE" in state.name:
        try:
            _, msg = delete_question_from_db(table_name, int(text))
            bot.send_message(user_id, msg)
        except ValueError:
            bot.send_message(user_id, "Invalid ID. Please send a number.")
            return
    send_admin_menu(user_id)

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'voice', 'sticker'], func=lambda msg: ADMIN_STATES.get(msg.from_user.id) == AdminState.AWAITING_BROADCAST_MESSAGE)
def handle_broadcast_message(message):
    user_id = message.from_user.id
    logging.info(f"Admin {user_id} initiated a broadcast.")
    bot.send_message(user_id, "Broadcasting your message to all users... This may take a moment.")

    all_chat_ids = get_all_user_chat_ids()
    success_count, fail_count = 0, 0

    for chat_id in all_chat_ids:
        if chat_id == user_id: continue
        try:
            bot.copy_message(chat_id, message.chat.id, message.message_id)
            success_count += 1
            time.sleep(BROADCAST_DELAY_SECONDS)
        except telebot.apihelper.ApiTelegramException as e:
            logging.warning(f"Failed to send broadcast to {chat_id}: {e}")
            fail_count += 1

    summary_message = f"📢 **Broadcast Complete**\n\n✅ Sent successfully to: **{success_count}** users.\n❌ Failed for: **{fail_count}** users."
    bot.send_message(user_id, summary_message, parse_mode='Markdown')
    send_admin_menu(user_id)

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'voice', 'sticker'], func=lambda msg: USER_STATES.get(msg.from_user.id) == UserState.AWAITING_ADMIN_MESSAGE)
def handle_admin_chat_message(message):
    user_id = message.from_user.id
    user_full_name = message.from_user.first_name + (f" {message.from_user.last_name}" if message.from_user.last_name else "")
    user_username = f" (@{message.from_user.username})" if message.from_user.username else ""
    admin_message_header = f"👤 **New message from {user_full_name}{user_username}** (ID: `{user_id}`):\n\n"

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_message_header, parse_mode='Markdown')
            bot.copy_message(admin_id, user_id, message.message_id)
        except telebot.apihelper.ApiTelegramException as e:
            logging.warning(f"Failed to forward user message to admin {admin_id}: {e}")

    bot.send_message(user_id, "✅ Your message has been sent to the admin team!", reply_markup=ReplyKeyboardRemove())
    start_command(message)

@bot.message_handler(content_types=['voice'], func=lambda msg: USER_STATES.get(msg.from_user.id) == UserState.AWAITING_VOICE_ANSWER and openai_client)
def handle_voice_message_for_feedback(message):
    chat_id = message.chat.id

    if message.voice.duration > MAX_VOICE_DURATION_SECONDS:
        bot.send_message(chat_id, f"⚠️ Your voice message is too long ({message.voice.duration}s). Please keep it under {MAX_VOICE_DURATION_SECONDS} seconds.")
        return

    try:
        bot.send_chat_action(chat_id, 'typing')
        bot.send_message(chat_id, "🎧 Got it! Analyzing your response now... this might take a moment.")

        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        audio_path = f"user_audio_{chat_id}.ogg"
        with open(audio_path, "wb") as audio_file:
            audio_file.write(downloaded_file)

        bot.send_chat_action(chat_id, 'upload_voice')
        with open(audio_path, "rb") as audio_file_for_api:
            transcript_response = openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file_for_api)
        os.remove(audio_path)
        transcript = transcript_response.text

        question = USER_CURRENT_QUESTION.get(chat_id, "an IELTS question")
        bot.send_chat_action(chat_id, 'typing')
        feedback = get_ielts_feedback(question, transcript)
        bot.send_message(chat_id, feedback, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"Error during AI check process: {e}")
        bot.send_message(chat_id, "❌ Sorry, I couldn't process that. Please make sure the audio is clear and try again.")

    finally:
        USER_STATES.pop(chat_id, None)
        USER_CURRENT_QUESTION.pop(chat_id, None)
        start_command(message)

@bot.message_handler(func=lambda message: True)
def handle_user_message(message):
    chat_id = message.chat.id
    user_state = USER_STATES.get(chat_id)
    text = message.text.strip() if message.text else ""

    if chat_id in ADMIN_IDS and ADMIN_STATES.get(chat_id):
        if ADMIN_STATES.get(chat_id) == AdminState.IN_ADMIN_PANEL:
             bot.send_message(chat_id, "Invalid option. Please use the menu or send a request like `1:25`.")
        return

    add_or_update_user_activity(chat_id)

    if user_state == UserState.MAIN_MENU:
        action_map = {"1️⃣ Part 1": "part1_questions", "2️⃣ Part 2": "part2_topics", "3️⃣ Part 3": "part3_discussions"}
        if text in action_map:
            table_name = action_map[text]
            question = get_random_question(table_name)
            USER_CURRENT_QUESTION[chat_id] = question
            part_number = text.split(' ')[1].replace('️⃣', '')

            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("Get Another Question", callback_data=f"random_{table_name}"), InlineKeyboardButton("🤖 AI Check", callback_data=f"aicheck_{table_name}"))

            bot.send_message(chat_id, f"💬 **Part {part_number} Question:**\n\n{question}", reply_markup=keyboard, parse_mode='Markdown')
        elif text == "📜 List All Questions":
            USER_STATES[chat_id] = UserState.LISTING_MENU
            send_part_selection_menu(chat_id, "Which part's questions would you like to see?", "⬅️ Main Menu")
        elif text == "💬 Chat with Admin":
            USER_STATES[chat_id] = UserState.AWAITING_ADMIN_MESSAGE
            markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            markup.add(KeyboardButton("❌ Cancel"))
            bot.send_message(chat_id, "📝 Send me your message for the admin team. I will forward it. Or, press '❌ Cancel' to go back.", reply_markup=markup)
        else:
            bot.send_message(chat_id, "Sorry, I didn't understand that. Please use the buttons on the keyboard or type /start to begin.")

    elif user_state == UserState.LISTING_MENU:
        if text == "⬅️ Main Menu":
            start_command(message)
            return
        part_map = {"Part 1": "part1_questions", "Part 2": "part2_topics", "Part 3": "part3_discussions"}
        table_name = part_map.get(text)
        if table_name:
            send_paginated_list(chat_id, table_name, f"{text} Questions")
            bot.send_message(chat_id, f"Use the buttons above to navigate the list. You can select another part from the menu below.")
        else:
            bot.send_message(chat_id, "Please choose a valid part from the menu.")

    elif user_state == UserState.AWAITING_ADMIN_MESSAGE:
        if text == "❌ Cancel":
            bot.send_message(chat_id, "❌ Chat with admin cancelled.", reply_markup=ReplyKeyboardRemove())
            start_command(message)
        else:
            handle_admin_chat_message(message)

    elif user_state == UserState.AWAITING_VOICE_ANSWER:
        if text == "❌ Cancel":
            USER_STATES.pop(chat_id, None)
            USER_CURRENT_QUESTION.pop(chat_id, None)
            bot.send_message(chat_id, "❌ AI Check cancelled.", reply_markup=ReplyKeyboardRemove())
            start_command(message)
        else:
            bot.send_message(chat_id, "Please send a **voice message** or press '❌ Cancel' to go back.", parse_mode='Markdown')

    else:
        start_command(message)

if __name__ == "__main__":
    logging.info("Initializing database...")
    create_database()
    insert_sample_data()
    logging.info("Bot is starting to poll for messages...")

    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            logging.critical(f"Bot polling failed with a critical error: {e}")
            bot.stop_polling()
            time.sleep(15)
            logging.info("Restarting bot polling...")