import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReactionTypeEmoji
import aiosqlite

# ===== ЗАГРУЗКА КОНФИГА =====

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

with open("phrases.json", "r", encoding="utf-8") as f:
    phrases = json.load(f)

TOKEN = config["token"]
TIMEZONE = ZoneInfo(config["timezone"])

bot = Bot(TOKEN)
dp = Dispatcher()

# ===== БАЗА ДАННЫХ =====

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

async def get_random_user(chat_id):
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM messages WHERE chat_id = ?",
            (chat_id,)
        )
        users = await cursor.fetchall()
        return random.choice(users)[0] if users else None

# ===== ЛОГИКА =====

@dp.message()
async def handle_message(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    await save_message(chat_id, user_id)

    text = (message.text or "").lower()

    # 👍 Реакция на согласие
    if any(word in text for word in phrases["agree_words"]):
        await message.react([ReactionTypeEmoji(emoji="👍")])

    # 🤡 случайные реакции
    if random.random() < config["reaction_chance"]:
        emoji = random.choice(["🤡", "💩"])
        await message.react([ReactionTypeEmoji(emoji=emoji)])

    # 💬 триггеры
    for trigger, responses in phrases["triggers"].items():
        if trigger in text:
            await message.reply(random.choice(responses))
            return

# ===== РАНДОМ СООБЩЕНИЯ =====

async def random_messages_loop():
    while True:
        await asyncio.sleep(random.randint(3600, 7200))  # раз в 1-2 часа

        chats = set()

        async with aiosqlite.connect("database.db") as db:
            cursor = await db.execute("SELECT DISTINCT chat_id FROM messages")
            rows = await cursor.fetchall()
            chats = [row[0] for row in rows]

        for chat_id in chats:
            if random.random() < 0.3:
                await bot.send_message(chat_id, random.choice(phrases["random"]))

            # троллинг
            if random.random() < 0.3:
                user_id = await get_random_user(chat_id)
                if user_id:
                    text = random.choice(phrases["trolling"]).replace(
                        "{user}", f"<a href='tg://user?id={user_id}'>коллега</a>"
                    )
                    await bot.send_message(chat_id, text, parse_mode="HTML")

# ===== УТРО / ВЕЧЕР =====

async def scheduler_loop():
    while True:
        now = datetime.now(TIMEZONE)

        if now.weekday() < 5:  # будни
            # 09:00
            if now.hour == 9 and now.minute == 0:
                await broadcast(phrases["morning"])

            # 18:00
            if now.hour == 18 and now.minute == 0:
                await broadcast(phrases["evening"])

        await asyncio.sleep(60)

async def broadcast(messages):
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute("SELECT DISTINCT chat_id FROM messages")
        chats = await cursor.fetchall()

    for chat in chats:
        if random.random() < 0.5:
            await bot.send_message(chat[0], random.choice(messages))

# ===== ЗАПУСК =====

async def main():
    await init_db()

    asyncio.create_task(random_messages_loop())
    asyncio.create_task(scheduler_loop())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())