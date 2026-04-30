import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque

import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.enums import ChatAction

# ===== LOAD =====

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

with open("phrases.json", encoding="utf-8") as f:
    phrases = json.load(f)

bot = Bot(config["token"])
dp = Dispatcher()

TIMEZONE = ZoneInfo(config["timezone"])

chat_history = {}
last_random = {}

# ===== DB =====

async def init_db():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            user_id INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            user_id INTEGER,
            message_id INTEGER,
            text TEXT,
            remind_at INTEGER
        )
        """)

        await db.commit()

async def save_message(chat_id, user_id):
    async with aiosqlite.connect("database.db") as db:
        await db.execute(
            "INSERT INTO messages VALUES (?, ?)",
            (chat_id, user_id)
        )
        await db.commit()

# ===== UTILS =====

def detect_wave(history, keywords, threshold=3):
    return sum(any(k in msg for k in keywords) for msg in history) >= threshold

def detect_mode(history):
    if detect_wave(history, ["принято", "ок", "согласен"]):
        return "work"
    if detect_wave(history, ["ахах", "лол", "😂", "🤣"]):
        return "chat"
    return "neutral"

def parse_time(text):
    now = datetime.now(TIMEZONE)
    text = text.lower()

    if m := re.search(r"через (\d+) мин", text):
        return now + timedelta(minutes=int(m.group(1)))

    if m := re.search(r"через (\d+) час", text):
        return now + timedelta(hours=int(m.group(1)))

    if m := re.search(r"(\d{1,2}):(\d{2})", text):
        h, m_ = int(m.group(1)), int(m.group(2))
        dt = now.replace(hour=h, minute=m_, second=0)
        return dt if dt > now else dt + timedelta(days=1)

    if "завтра" in text:
        return now + timedelta(days=1)

    return None

async def human_reply(message: Message, text: str):
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(random.uniform(1, 2.5))
    await message.reply(text)

# ===== DECISION ENGINE v7.1 =====

async def decide_action(chat_id, text, lower, history):

    # ===== 1. TRIGGERS (жёсткий приоритет) =====
    for t, arr in phrases.get("triggers", {}).items():
        if t in lower:
            return ("reply", random.choice(arr))

    mode = detect_mode(history)

    candidates = []

    # ===== 2. CONTEXT =====

    if len(history) > 5:

        # 🎉 ДР
        if detect_wave(history, ["др", "с днем", "поздравляю"]):
            candidates.append((5, ("reply", "С днём рождения 🎉")))

        # ===== WORK MODE =====
        if mode == "work":
            if detect_wave(history, ["принято", "ок", "понял"]):
                candidates.append((6, ("reply", random.choice(["принято", "ок"]))))

        # ===== CHAT MODE =====
        if mode == "chat":
            if detect_wave(history, ["ахах", "лол", "😂", "🤣"]):
                candidates.append((4, ("reply", random.choice(["😂", "ну да", "смешно"]))))

    # ===== 3. REACTIONS =====
    if any(w in lower for w in phrases.get("agree_words", [])):
        candidates.append((7, ("react", "👍")))

    # ===== 4. RANDOM (ограниченный) =====
    now = datetime.now(TIMEZONE)

    if 9 <= now.hour < 18:
        if last_random.get(chat_id) != now.date():
            if random.random() < 0.1:
                candidates.append((1, ("message", random.choice(
                    phrases.get("random", ["..."])
                ))))

    # ===== ВЫБОР =====
    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_score = candidates[0][0]
    top = [c for c in candidates if c[0] == top_score]

    return random.choice(top)[1]

# ===== HANDLER =====

@dp.message(~F.text.startswith("/"))
async def handle(message: Message):

    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text or ""
    lower = text.lower()

    await save_message(chat_id, user_id)

    history = chat_history.setdefault(chat_id, deque(maxlen=15))
    history.append(lower)

    # ===== REMINDER =====
    if dt := parse_time(text):
        async with aiosqlite.connect("database.db") as db:
            await db.execute(
                "INSERT INTO reminders VALUES (NULL, ?, ?, ?, ?, ?)",
                (chat_id, user_id, message.message_id, text, int(dt.timestamp()))
            )
            await db.commit()

        await human_reply(message, "Принято")
        return

    # ===== DECISION =====
    action = await decide_action(chat_id, text, lower, history)

    if not action:
        return

    action_type, payload = action

    if action_type == "reply":
        await human_reply(message, payload)

    elif action_type == "message":
        await bot.send_message(chat_id, payload)
        last_random[chat_id] = datetime.now(TIMEZONE).date()

    elif action_type == "react":
        await message.react([ReactionTypeEmoji(emoji=payload)])

# ===== REMINDER LOOP =====

async def reminder_loop():
    while True:
        now = int(datetime.now(TIMEZONE).timestamp())

        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute(
                "SELECT id, chat_id, user_id, message_id, text FROM reminders WHERE remind_at <= ?",
                (now,)
            )
            rows = await cur.fetchall()

            for r in rows:
                _, chat_id, user_id, message_id, text = r

                await bot.send_message(
                    chat_id,
                    random.choice(phrases.get("reminder_phrases", ["напоминание"])) +
                    f"\n<a href='tg://user?id={user_id}'>коллега</a>",
                    parse_mode="HTML",
                    reply_to_message_id=message_id
                )

                await db.execute("DELETE FROM reminders WHERE id=?", (r[0],))

            await db.commit()

        await asyncio.sleep(60)

# ===== START =====

async def main():
    await init_db()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())