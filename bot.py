import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque

from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.enums import ChatAction
import aiosqlite

# ===== LOAD =====

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

with open("phrases.json", encoding="utf-8") as f:
    phrases = json.load(f)

bot = Bot(config["token"])
dp = Dispatcher()

TIMEZONE = ZoneInfo(config["timezone"])

chat_history = {}
last_morning = {}
last_evening = {}

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

# ===== UTILS =====

def parse_time(text):
    now = datetime.now(TIMEZONE)

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

def detect_wave(history, keywords, threshold=3):
    count = sum(any(k in msg for k in keywords) for msg in history)
    return count >= threshold

async def human_reply(message: Message, text: str):
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await asyncio.sleep(random.uniform(1, 2.5))
        await message.reply(text)
    except:
        pass

async def get_target_user(chat_id):
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute("""
            SELECT user_id, COUNT(*) as cnt
            FROM messages
            WHERE chat_id = ?
            GROUP BY user_id
        """, (chat_id,))
        users = await cur.fetchall()

    if not users:
        return None

    return random.choice(users)[0]

# ===== HANDLER =====

@dp.message()
async def handle(message: Message):
    try:
        if not message.text:
            return

        if message.text.startswith("/"):
            return

        chat_id = message.chat.id
        user_id = message.from_user.id
        text = message.text
        lower = text.lower()

        await save_message(chat_id, user_id)

        history = chat_history.setdefault(chat_id, deque(maxlen=15))
        history.append(lower)

        # 👍 реакции
        if any(w in lower for w in phrases["agree_words"]):
            await message.react([ReactionTypeEmoji(emoji="👍")])

        # 🤡 случайные реакции
        if random.random() < config["reaction_chance"]:
            await message.react([ReactionTypeEmoji(emoji=random.choice(["🤡", "💩"]))])

        # ===== КОНТЕКСТ =====

        if len(history) > 5:

            if detect_wave(history, ["с днем", "поздравляю", "др"]):
                if random.random() < 0.3:
                    await human_reply(message, random.choice(phrases["birthday"]))
                    return

            if detect_wave(history, ["принято", "ок", "понял"]):
                if random.random() < 0.3:
                    await human_reply(message, random.choice(["принято", "ок"]))
                    return

        # ===== REMINDER =====

        if dt := parse_time(lower):
            async with aiosqlite.connect("database.db") as db:
                await db.execute(
                    "INSERT INTO reminders VALUES (NULL, ?, ?, ?, ?, ?)",
                    (chat_id, user_id, message.message_id, text, int(dt.timestamp()))
                )
                await db.commit()

            await human_reply(message, "Принято")
            return

    except Exception as e:
        print("HANDLE ERROR:", e)

# ===== LOOPS =====

async def reminder_loop():
    while True:
        try:
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
                        random.choice(phrases["reminder_phrases"]) +
                        f"\n<a href='tg://user?id={user_id}'>коллега</a>",
                        parse_mode="HTML",
                        reply_to_message_id=message_id,
                        allow_sending_without_reply=True
                    )

                    await db.execute("DELETE FROM reminders WHERE id=?", (r[0],))

                await db.commit()

        except Exception as e:
            print("REMINDER ERROR:", e)

        await asyncio.sleep(60)

async def random_loop():
    while True:
        try:
            await asyncio.sleep(random.randint(3600, 7200))

            async with aiosqlite.connect("database.db") as db:
                cur = await db.execute("SELECT DISTINCT chat_id FROM messages")
                chats = [x[0] for x in await cur.fetchall()]

            for chat in chats:

                if random.random() < 0.2:
                    await bot.send_message(chat, random.choice(phrases["random"]))

                if random.random() < 0.3:
                    user_id = await get_target_user(chat)
                    if user_id:
                        text = random.choice(phrases["trolling"]).replace(
                            "{user}",
                            f"<a href='tg://user?id={user_id}'>коллега</a>"
                        )
                        await bot.send_message(chat, text, parse_mode="HTML")

        except Exception as e:
            print("RANDOM ERROR:", e)

async def scheduler():
    while True:
        try:
            now = datetime.now(TIMEZONE)

            if now.weekday() < 5:
                async with aiosqlite.connect("database.db") as db:
                    cur = await db.execute("SELECT DISTINCT chat_id FROM messages")
                    chats = [row[0] for row in await cur.fetchall()]

                for chat_id in chats:

                    if 9 <= now.hour < 10:
                        if last_morning.get(chat_id) != now.date():
                            if random.random() < 0.5:
                                await bot.send_message(chat_id, random.choice(phrases["morning"]))
                            last_morning[chat_id] = now.date()

                    if 18 <= now.hour < 19:
                        if last_evening.get(chat_id) != now.date():
                            if random.random() < 0.5:
                                await bot.send_message(chat_id, random.choice(phrases["evening"]))
                            last_evening[chat_id] = now.date()

        except Exception as e:
            print("SCHEDULER ERROR:", e)

        await asyncio.sleep(60)

# ===== BROADCAST =====

@dp.message()
async def broadcast_command(message: Message):
    try:
        if not message.text:
            return

        if not message.text.startswith("/broadcast"):
            return

        if message.from_user.id != config["admin_id"]:
            return

        text = message.text.replace("/broadcast", "", 1).strip()

        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute("SELECT DISTINCT chat_id FROM messages")
            chats = [row[0] for row in await cur.fetchall()]

        for chat_id in chats:
            try:
                await bot.send_message(chat_id, text)
                await asyncio.sleep(0.2)
            except:
                pass

        await message.reply(f"Отправлено в {len(chats)} чатов")

    except Exception as e:
        print("BROADCAST ERROR:", e)

# ===== START =====
async def heartbeat():
    while True:
        print("alive")
        await asyncio.sleep(30)

async def main():
    print("BOT STARTING...")

    await bot.delete_webhook(drop_pending_updates=True)
    await init_db()

    await asyncio.gather(
        dp.start_polling(bot),
        reminder_loop(),
        random_loop(),
        scheduler(),
        heartbeat()
    )

if __name__ == "__main__":
    asyncio.run(main())