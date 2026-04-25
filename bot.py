import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

last_messages = {}

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

def unique(chat_id, options):
    last = last_messages.get(chat_id)
    opts = [o for o in options if o != last] or options
    res = random.choice(opts)
    last_messages[chat_id] = res
    return res

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

def is_promise(text):
    return any(x in text.lower() for x in [
        "я сделаю", "я посмотрю", "я возьму", "сделаем позже", "попробую"
    ])

async def human_reply(message: Message, text: str):
    if random.random() < 0.15:
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(random.uniform(1, 3))

    # иногда "..." перед ответом
    if random.random() < 0.03:
        await message.reply("...")
        await asyncio.sleep(random.uniform(0.5, 1.2))

    await message.reply(text)

async def get_target_user(chat_id):
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute("""
            SELECT user_id, COUNT(*) as cnt
            FROM messages
            WHERE chat_id = ?
            GROUP BY user_id
            ORDER BY cnt DESC
        """, (chat_id,))
        users = await cur.fetchall()

    if not users:
        return None

    weighted = []
    for user_id, cnt in users:
        weighted.extend([user_id] * min(cnt, 10))

    return random.choice(weighted)

# ===== HANDLER =====

@dp.message()
async def handle(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text or ""
    lower = text.lower()

    await save_message(chat_id, user_id)

    # 👍
    if any(w in lower for w in phrases["agree_words"]):
        await message.react([ReactionTypeEmoji(emoji="👍")])

    # 🤡
    if random.random() < config["reaction_chance"]:
        await message.react([ReactionTypeEmoji(emoji=random.choice(["🤡", "💩"]))])

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

    # ===== AGREEMENT DETECT =====
    if any(x in lower for x in ["итого", "давай так", "решили"]):
        await human_reply(message, "Я правильно понял, что мы опять всё решили и ничего не сделали?")
        return

    # ===== CONTEXT =====
    if text.isupper() and len(text) > 5:
        await human_reply(message, unique(chat_id, phrases["caps"]))
        return

    if "?" in text and random.random() < 0.03:
        await human_reply(message, unique(chat_id, phrases["questions"]))
        return

    if len(text.split()) <= 2 and random.random() < 0.01:
        await human_reply(message, unique(chat_id, phrases["short"]))
        return

    # ===== TRIGGERS =====
    for t, arr in phrases["triggers"].items():
        if t in lower:
            await human_reply(message, unique(chat_id, arr))
            return

# ===== LOOPS =====

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
                _, chat_id, user_id, text = r
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

        await asyncio.sleep(60)

async def promise_loop():
    while True:
        await asyncio.sleep(7200)

        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute("SELECT id, chat_id, user_id, text FROM promises")
            rows = await cur.fetchall()

            for r in rows:
                if random.random() < 0.2:
                    await bot.send_message(
                    r[1],
                    random.choice([
                        f"<a href='tg://user?id={r[2]}'>коллега</a>, ты же говорил: \"{r[3]}\" 🙂",
                        f"Я, конечно, не напоминаю… но <a href='tg://user?id={r[2]}'>ты</a> обещал: \"{r[3]}\"",
                        f"Неловко получается… <a href='tg://user?id={r[2]}'>коллега</a>, где результат по: \"{r[3]}\"?"
                    ]),
    parse_mode="HTML"
)
                    await db.execute("DELETE FROM promises WHERE id=?", (r[0],))

            await db.commit()

async def random_loop():
    personality = config.get("personality", "neutral")

    while True:
        await asyncio.sleep(random.randint(3600, 7200))

        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute("SELECT DISTINCT chat_id FROM messages")
            chats = [x[0] for x in await cur.fetchall()]

        for chat in chats:

            # обычные сообщения
            if random.random() < 0.02:
                await bot.send_chat_action(chat, ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.0, 2.5))

                await bot.send_message(chat, random.choice(phrases["random"]))

            # === ТРОЛЛИНГ ===

            troll_chance = 0.05
            if personality == "toxic":
                troll_chance = 0.2
            elif personality == "manager":
                troll_chance = 0.01

            if random.random() < troll_chance:
                user_id = await get_target_user(chat)

                if user_id:
                    text = random.choice(phrases["trolling"]).replace(
                        "{user}",
                        f"<a href='tg://user?id={user_id}'>коллега</a>"
                    )

                    await bot.send_chat_action(chat, ChatAction.TYPING)
                    await asyncio.sleep(random.uniform(1.5, 3.5))

                    await bot.send_message(chat, text, parse_mode="HTML")

# ===== START =====

async def main():
    await init_db()

    asyncio.create_task(reminder_loop())
    asyncio.create_task(promise_loop())
    asyncio.create_task(random_loop())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())