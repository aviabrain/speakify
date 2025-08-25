## What is this
SPEAKIFY is a Telegram bot for practicing IELTS speaking.  
It stores Part 1 / Part 2 / Part 3 prompts in an SQLite database, gives users random questions, supports an admin panel (CRUD + broadcasts), and can transcribe voice answers (Whisper) and return feedback from GPT-4o-mini.

---

## Features
- Store and manage Part 1 / Part 2 / Part 3 questions/topics in SQLite.  
- Get a random question via buttons.  
- Admin panel for adding / deleting / listing questions.  
- Broadcast messages to users.  
- **AI Check**: user sends voice → bot transcribes via Whisper → GPT returns feedback & example answer.  
- Tracks users (first_seen / last_interaction) for basic analytics.

---

## WARNING — READ THIS
- **DO NOT** commit real `BOT_TOKEN` or `OPENAI_API_KEY`. Ever. Only commit templates and `.env.example`.  
- Default DB filename: `ielts_questions.db` (SQLite). Add it to `.gitignore` if you don't want to push the DB.  
- Voice files are temporarily saved like `user_audio_{chat_id}.ogg` and removed after processing — ignore them in git too.

---

## Quick install (Linux)
```bash
# clone the repo
git clone <YOUR-REPO-URL>
cd <repo-folder>

# create venv and install deps
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# copy env example and edit keys
cp .env.example .env
# edit .env: BOT_TOKEN, OPENAI_API_KEY, ADMIN_IDS, DB_NAME

# run the bot
python bot.py