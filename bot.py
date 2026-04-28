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


# ======================
# LOAD CONFIG
# ======================

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

with open("phrases.json", encoding="utf-8") as f:
    phrases = json.load(f)

bot = Bot(config["token"])
dp = Dispatcher()

TIMEZONE = ZoneInfo(config["timezone"])

chat_history = {}
last_messages = {}

# ======================
# DB
# ======================

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

        await db.execute("""
        CREATE TABLE IF NOT EXISTS promises (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            user_id INTEGER,
            text TEXT,
            created_at INTEGER
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


# ======================
# UTILS
# ======================

def detect_wave(history, keywords, threshold=3):
    return sum(any(k in msg for k in keywords) for msg in history) >= threshold


def is_promise(text):
    return any(x in text.lower() for x in [
        "сделаю", "посмотрю", "возьму", "попробую", "позже"
    ])


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


async def get_target_user(chat_id):
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute("""
            SELECT user_id, COUNT(*) as cnt
            FROM messages
            WHERE chat_id = ?
            GROUP BY user_id
        """, (chat_id,))
        rows = await cur.fetchall()

    if not rows:
        return None

    pool = []
    for uid, cnt in rows:
        pool.extend([uid] * min(cnt, 10))

    return random.choice(pool)


# ======================
# DECISION ENGINE (V7 CORE)
# ======================

async def decide_action(chat_id, user_id, text, lower, history):

    candidates = []

    # ===== 1. TRIGGERS =====
    for t, arr in phrases.get("triggers", {}).items():
        if t in lower:
            candidates.append({
                "type": "trigger",
                "score": 10,
                "action": ("reply", random.choice(arr))
            })

    # ===== 2. CONTEXT WAVE =====
    if len(history) > 5:

        if detect_wave(history, ["др", "с днем", "поздравляю"]):
            candidates.append({
                "type": "context",
                "score": 6,
                "action": ("reply", "С днём рождения 🎉")
            })

        if detect_wave(history, ["принято", "ок", "понял"]):
            candidates.append({
                "type": "context",
                "score": 5,
                "action": ("reply", "принято")
            })

        if detect_wave(history, ["ахах", "лол", "😂", "🤣"]):
            candidates.append({
                "type": "context",
                "score": 4,
                "action": ("reply", random.choice(["😂", "ну да...", "смешно"]))
            })

    # ===== 3. REACTIONS =====
    if any(w in lower for w in phrases.get("agree_words", [])):
        candidates.append({
            "type": "reaction",
            "score": 7,
            "action": ("react", "👍")
        })

    # ===== 4. TROLL =====
    if random.random() < 0.25:
        target = await get_target_user(chat_id)
        if target:
            candidates.append({
                "type": "troll",
                "score": 4,
                "action": ("message",
                           random.choice(phrases.get("trolling", []))
                           .replace("{user}", f"<a href='tg://user?id={target}'>коллега</a>"))
            })

    # ===== 5. IDLE CHAT FILL =====
    if random.random() < 0.1:
        candidates.append({
            "type": "idle",
            "score": 2,
            "action": ("message", random.choice(phrases.get("random", ["..."])))
        })

    if not candidates:
        return None

    # ===== SCORE SELECTION =====
    candidates.sort(key=lambda x: x["score"], reverse=True)

    top_score = candidates[0]["score"]
    top = [c for c in candidates if c["score"] == top_score]

    return random.choice(top)


# ======================
# HANDLER
# ======================

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

    # ===== PROMISE =====
    if is_promise(text):
        async with aiosqlite.connect("database.db") as db:
            await db.execute(
                "INSERT INTO promises VALUES (NULL, ?, ?, ?, ?)",
                (chat_id, user_id, text, int(datetime.now().timestamp()))
            )
            await db.commit()
        return

    # ===== DECISION ENGINE =====
    action = await decide_action(chat_id, user_id, text, lower, history)

    if not action:
        return

    action_type, payload = action["action"]

    if action_type == "reply":
        await human_reply(message, payload)

    elif action_type == "message":
        await bot.send_message(chat_id, payload, parse_mode="HTML")

    elif action_type == "react":
        await message.react([ReactionTypeEmoji(emoji=payload)])


# ======================
# REMINDERS LOOP
# ======================

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


# ======================
# START
# ======================

async def main():
    await init_db()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())