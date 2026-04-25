import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.enums import ChatAction
import aiosqlite

# ===== ЗАГРУЗКА =====

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

with open("phrases.json", "r", encoding="utf-8") as f:
    phrases = json.load(f)

TOKEN = config["token"]
TIMEZONE = ZoneInfo(config["timezone"])

bot = Bot(TOKEN)
dp = Dispatcher()

# ===== ПАМЯТЬ =====

last_messages = {}  # анти-повторы

# ===== БД =====

async def init_db():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            user_id INTEGER
        )
        """)
        await db.commit()

async def save_message(chat_id, user_id):
    async with aiosqlite.connect("database.db") as db:
        await db.execute(
            "INSERT INTO messages (chat_id, user_id) VALUES (?, ?)",
            (chat_id, user_id)
        )
        await db.commit()

async def get_active_user(chat_id):
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute(
            """SELECT user_id, COUNT(*) as cnt 
               FROM messages 
               WHERE chat_id = ? 
               GROUP BY user_id 
               ORDER BY cnt DESC""",
            (chat_id,)
        )
        users = await cursor.fetchall()

    if not users:
        return None

    weighted = []
    for user_id, cnt in users:
        weighted.extend([user_id] * min(cnt, 10))

    return random.choice(weighted)

# ===== УТИЛИТЫ =====

def get_unique(chat_id, options):
    last = last_messages.get(chat_id)
    choices = [o for o in options if o != last] or options
    result = random.choice(choices)
    last_messages[chat_id] = result
    return result


def make_typo(text):
    if len(text) < 8 or random.random() > 0.2:
        return text, None

    i = random.randint(0, len(text) - 2)
    typo = text[:i] + text[i+1] + text[i] + text[i+2:]
    return typo, text


async def human_reply(message: Message, text: str):
    # иногда игнор
    if random.random() < 0.15:
        return

    msg_len = len(message.text or "")

    if msg_len < 20:
        delay = random.uniform(0.8, 1.8)
    elif msg_len < 80:
        delay = random.uniform(1.5, 3.0)
    else:
        delay = random.uniform(2.5, 4.5)

    # typing
    await message.bot.send_chat_action(
        chat_id=message.chat.id,
        action=ChatAction.TYPING
    )
    await asyncio.sleep(delay)

    # иногда "..." перед ответом
    if random.random() < 0.05:
        await message.reply("...")
        await asyncio.sleep(random.uniform(0.5, 1.2))

    # опечатка
    typo, corrected = make_typo(text)

    if corrected:
        await message.reply(typo)
        await asyncio.sleep(random.uniform(0.4, 1.0))
        await message.reply(corrected)
    else:
        await message.reply(text)

# ===== ОСНОВНАЯ ЛОГИКА =====

@dp.message()
async def handle_message(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    await save_message(chat_id, user_id)

    text = (message.text or "")
    lower = text.lower()

    # 👍 согласие
    if any(word in lower for word in phrases["agree_words"]):
        await message.react([ReactionTypeEmoji(emoji="👍")])

    # 🤡 реакции
    if random.random() < config["reaction_chance"]:
        await message.react([
            ReactionTypeEmoji(emoji=random.choice(["🤡", "💩"]))
        ])

    # ===== КОНТЕКСТ =====

    # CAPS
    if text.isupper() and len(text) > 5:
        await human_reply(message, get_unique(chat_id, phrases["caps"]))
        return

    # вопрос
    if "?" in text and random.random() < 0.05:
        await human_reply(message, get_unique(chat_id, phrases["questions"]))
        return

    # короткое
    if len(text.split()) <= 2 and random.random() < 0.03:
        await human_reply(message, get_unique(chat_id, phrases["short"]))
        return

    # триггеры
    for trigger, responses in phrases["triggers"].items():
        if trigger in lower:
            await human_reply(message, get_unique(chat_id, responses))
            return

# ===== ФОНОВЫЕ АКТИВНОСТИ =====

async def random_loop():
    while True:
        await asyncio.sleep(random.randint(3600, 7200))

        async with aiosqlite.connect("database.db") as db:
            cursor = await db.execute("SELECT DISTINCT chat_id FROM messages")
            chats = [row[0] for row in await cursor.fetchall()]

        for chat_id in chats:
            # обычные вбросы
            if random.random() < 0.3:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await bot.send_message(chat_id, random.choice(phrases["random"]))

            # троллинг
            if random.random() < 0.3:
                user_id = await get_active_user(chat_id)
                if user_id:
                    text = random.choice(phrases["trolling"]).replace(
                        "{user}", f"<a href='tg://user?id={user_id}'>коллега</a>"
                    )

                    await bot.send_chat_action(chat_id, ChatAction.TYPING)
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                    await bot.send_message(chat_id, text, parse_mode="HTML")

# ===== УТРО / ВЕЧЕР =====

async def scheduler():
    while True:
        now = datetime.now(TIMEZONE)

        if now.weekday() < 5:
            if now.hour == 9 and now.minute == 0:
                await broadcast(phrases["morning"])
            if now.hour == 18 and now.minute == 0:
                await broadcast(phrases["evening"])

        await asyncio.sleep(60)

async def broadcast(messages):
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute("SELECT DISTINCT chat_id FROM messages")
        chats = [row[0] for row in await cursor.fetchall()]

    for chat_id in chats:
        if random.random() < 0.5:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(random.uniform(1.0, 2.5))
            await bot.send_message(chat_id, random.choice(messages))

# ===== ЗАПУСК =====

async def main():
    await init_db()

    asyncio.create_task(random_loop())
    asyncio.create_task(scheduler())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())