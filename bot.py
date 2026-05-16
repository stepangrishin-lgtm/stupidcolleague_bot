import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque

import aiosqlite

from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.enums import ChatAction

# =========================================================
# LOAD
# =========================================================

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

with open("phrases.json", encoding="utf-8") as f:
    phrases = json.load(f)

bot = Bot(config["token"])
dp = Dispatcher()

TIMEZONE = ZoneInfo(config["timezone"])

# =========================================================
# RUNTIME MEMORY
# =========================================================

chat_history = {}          # chat_id -> deque
chat_modes = {}            # chat_id -> mood
last_random = {}           # anti spam
last_activity = {}         # cooldowns
delayed_queue = []         # delayed reactions

# =========================================================
# DB
# =========================================================

db = None

async def init_db():
    global db

    db = await aiosqlite.connect("database.db")

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
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id INTEGER PRIMARY KEY,
        messages_count INTEGER DEFAULT 0,
        promises_count INTEGER DEFAULT 0,
        favorite_score INTEGER DEFAULT 0
    )
    """)

    await db.commit()

# =========================================================
# USER MEMORY
# =========================================================

async def save_message(chat_id, user_id):
    await db.execute(
        "INSERT INTO messages VALUES (?, ?)",
        (chat_id, user_id)
    )

    await db.execute("""
    INSERT INTO user_profiles (user_id, messages_count)
    VALUES (?, 1)
    ON CONFLICT(user_id)
    DO UPDATE SET messages_count = messages_count + 1
    """, (user_id,))

    await db.commit()

async def add_promise(user_id):
    await db.execute("""
    INSERT INTO user_profiles (user_id, promises_count)
    VALUES (?, 1)
    ON CONFLICT(user_id)
    DO UPDATE SET promises_count = promises_count + 1
    """, (user_id,))

    await db.commit()

async def get_user_profile(user_id):
    cur = await db.execute("""
    SELECT messages_count, promises_count, favorite_score
    FROM user_profiles
    WHERE user_id = ?
    """, (user_id,))

    row = await cur.fetchone()

    if not row:
        return {
            "messages": 0,
            "promises": 0,
            "favorite": 0
        }

    return {
        "messages": row[0],
        "promises": row[1],
        "favorite": row[2]
    }

# =========================================================
# UTILS
# =========================================================

def detect_wave(history, keywords, threshold=3):
    return sum(any(k in msg for k in keywords) for msg in history) >= threshold

def detect_mode(history):

    if detect_wave(history, ["принято", "ок", "согласен"]):
        return "work"

    if detect_wave(history, ["ахах", "лол", "😂", "🤣"]):
        return "fun"

    if detect_wave(history, ["срочно", "дедлайн", "горим"]):
        return "panic"

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

        if dt < now:
            dt += timedelta(days=1)

        return dt

    if "завтра" in text:
        return now + timedelta(days=1)

    return None

def is_promise(text):
    text = text.lower()

    return any(x in text for x in [
        "сделаю",
        "посмотрю",
        "возьму",
        "попробую",
        "позже"
    ])

async def human_reply(message: Message, text: str):

    await message.bot.send_chat_action(
        message.chat.id,
        ChatAction.TYPING
    )

    await asyncio.sleep(random.uniform(1, 3))

    # иногда многоточие
    if random.random() < 0.08:
        await message.reply("...")
        await asyncio.sleep(random.uniform(0.5, 1.2))

    await message.reply(text)

async def get_target_user(chat_id):

    cur = await db.execute("""
    SELECT user_id, COUNT(*) as cnt
    FROM messages
    WHERE chat_id = ?
    GROUP BY user_id
    ORDER BY cnt DESC
    """, (chat_id,))

    rows = await cur.fetchall()

    if not rows:
        return None

    weighted = []

    for user_id, cnt in rows:
        weighted.extend([user_id] * min(cnt, 10))

    return random.choice(weighted)

# =========================================================
# COOLDOWN ENGINE
# =========================================================

def can_do_activity(chat_id, cooldown=90):

    now = datetime.now().timestamp()

    last = last_activity.get(chat_id, 0)

    if now - last < cooldown:
        return False

    last_activity[chat_id] = now
    return True

# =========================================================
# DECISION ENGINE V8
# =========================================================

async def decide_action(message, history):

    text = message.text or ""
    lower = text.lower()

    chat_id = message.chat.id
    user_id = message.from_user.id

    profile = await get_user_profile(user_id)

    candidates = []

    # =====================================================
    # HARD TRIGGERS
    # =====================================================

    for trigger, responses in phrases.get("triggers", {}).items():

        if trigger in lower:
            candidates.append((
                100,
                ("reply", random.choice(responses))
            ))

    # =====================================================
    # CONTEXT
    # =====================================================

    mode = detect_mode(history)
    chat_modes[chat_id] = mode

    # birthday
    if detect_wave(history, ["др", "с днем", "поздравляю"]):
        candidates.append((
            80,
            ("reply", random.choice(
                phrases.get("birthday", ["С днём рождения 🎉"])
            ))
        ))

    # work mode
    if mode == "work":

        if detect_wave(history, ["принято", "ок", "понял"]):
            candidates.append((
                60,
                ("reply", random.choice(["принято", "ок"]))
            ))

    # fun mode
    if mode == "fun":

        if detect_wave(history, ["ахах", "лол", "😂"]):
            candidates.append((
                40,
                ("reply", random.choice(["😂", "ну да", "смешно"]))
            ))

    # =====================================================
    # USER MEMORY
    # =====================================================

    if profile["promises"] >= 3:
        if random.random() < 0.05:
            candidates.append((
                55,
                ("reply", "ты уже много чего обещал 🙂")
            ))

    if profile["messages"] >= 100:
        if random.random() < 0.03:
            candidates.append((
                45,
                ("reply", "ты сегодня unusually активный")
            ))

    # =====================================================
    # REACTIONS
    # =====================================================

    if any(x in lower for x in phrases.get("agree_words", [])):
        candidates.append((
            70,
            ("react", "👍")
        ))

    # =====================================================
    # RANDOM NPC
    # =====================================================

    now = datetime.now(TIMEZONE)

    if 9 <= now.hour < 18:

        if last_random.get(chat_id) != now.date():

            if random.random() < 0.01:

                candidates.append((
                    10,
                    ("message", random.choice(
                        phrases.get("random", ["..."])
                    ))
                ))

    # =====================================================
    # NOTHING
    # =====================================================

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)

    top_score = candidates[0][0]

    top = [x for x in candidates if x[0] == top_score]

    return random.choice(top)[1]

# =========================================================
# HANDLER
# =========================================================

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

        # =================================================
        # HISTORY
        # =================================================

        history = chat_history.setdefault(
            chat_id,
            deque(maxlen=20)
        )

        history.append(lower)

        # =================================================
        # REMINDERS
        # =================================================

        if dt := parse_time(lower):

            await db.execute("""
            INSERT INTO reminders
            VALUES (NULL, ?, ?, ?, ?, ?)
            """, (
                chat_id,
                user_id,
                message.message_id,
                text,
                int(dt.timestamp())
            ))

            await db.commit()

            await human_reply(message, "Принято")
            return

        # =================================================
        # PROMISES
        # =================================================

        if is_promise(lower):
            await add_promise(user_id)

        # =================================================
        # DECISION
        # =================================================

        action = await decide_action(message, history)

        if not action:
            return

        if not can_do_activity(chat_id):
            return

        action_type, payload = action

        # delayed reactions
        if random.random() < 0.25:
            delayed_queue.append((
                datetime.now().timestamp() + random.randint(20, 90),
                message,
                action
            ))
            return

        if action_type == "reply":
            await human_reply(message, payload)

        elif action_type == "message":

            await bot.send_message(chat_id, payload)

            last_random[chat_id] = datetime.now(
                TIMEZONE
            ).date()

        elif action_type == "react":

            await message.react([
                ReactionTypeEmoji(emoji=payload)
            ])

    except Exception as e:
        print("HANDLE ERROR:", e)

# =========================================================
# REMINDER LOOP
# =========================================================

async def reminder_loop():

    while True:

        try:

            now = int(datetime.now(TIMEZONE).timestamp())

            cur = await db.execute("""
            SELECT id, chat_id, user_id,
                   message_id, text
            FROM reminders
            WHERE remind_at <= ?
            """, (now,))

            rows = await cur.fetchall()

            for row in rows:

                reminder_id, chat_id, user_id, \
                message_id, text = row

                await bot.send_message(
                    chat_id,
                    random.choice(
                        phrases.get(
                            "reminder_phrases",
                            ["напоминание"]
                        )
                    ) +
                    f"\n<a href='tg://user?id={user_id}'>коллега</a>",
                    parse_mode="HTML",
                    reply_to_message_id=message_id,
                    allow_sending_without_reply=True
                )

                await db.execute(
                    "DELETE FROM reminders WHERE id = ?",
                    (reminder_id,)
                )

            await db.commit()

        except Exception as e:
            print("REMINDER ERROR:", e)

        await asyncio.sleep(60)

# =========================================================
# DELAYED ACTIONS
# =========================================================

async def delayed_loop():

    while True:

        try:

            now = datetime.now().timestamp()

            ready = [
                x for x in delayed_queue
                if x[0] <= now
            ]

            for item in ready:

                _, message, action = item

                action_type, payload = action

                try:

                    if action_type == "reply":
                        await human_reply(message, payload)

                    elif action_type == "message":
                        await bot.send_message(
                            message.chat.id,
                            payload
                        )

                    elif action_type == "react":
                        await message.react([
                            ReactionTypeEmoji(
                                emoji=payload
                            )
                        ])

                except:
                    pass

                delayed_queue.remove(item)

        except Exception as e:
            print("DELAYED ERROR:", e)

        await asyncio.sleep(5)

# =========================================================
# RANDOM NPC LOOP
# =========================================================

async def npc_loop():

    while True:

        try:

            await asyncio.sleep(
                random.randint(3600, 7200)
            )

            cur = await db.execute("""
            SELECT DISTINCT chat_id
            FROM messages
            """)

            chats = [
                row[0]
                for row in await cur.fetchall()
            ]

            for chat_id in chats:

                if not can_do_activity(chat_id, 300):
                    continue

                # random npc message
                if random.random() < 0.08:

                    await bot.send_message(
                        chat_id,
                        random.choice(
                            phrases.get(
                                "random",
                                ["..."]
                            )
                        )
                    )

                # trolling
                if random.random() < 0.15:

                    user_id = await get_target_user(chat_id)

                    if user_id:

                        text = random.choice(
                            phrases.get(
                                "trolling",
                                ["{user}, работаем?"]
                            )
                        ).replace(
                            "{user}",
                            f"<a href='tg://user?id={user_id}'>коллега</a>"
                        )

                        await bot.send_message(
                            chat_id,
                            text,
                            parse_mode="HTML"
                        )

        except Exception as e:
            print("NPC ERROR:", e)

# =========================================================
# BROADCAST
# =========================================================

@dp.message()
async def broadcast(message: Message):

    try:

        if not message.text:
            return

        if not message.text.startswith("/broadcast"):
            return

        if message.from_user.id != config["admin_id"]:
            return

        text = message.text.replace(
            "/broadcast",
            "",
            1
        ).strip()

        cur = await db.execute("""
        SELECT DISTINCT chat_id
        FROM messages
        """)

        chats = [
            row[0]
            for row in await cur.fetchall()
        ]

        sent = 0

        for chat_id in chats:

            try:

                await bot.send_message(
                    chat_id,
                    text
                )

                sent += 1

                await asyncio.sleep(0.2)

            except:
                pass

        await message.reply(
            f"Отправлено в {sent} чатов"
        )

    except Exception as e:
        print("BROADCAST ERROR:", e)

# =========================================================
# START
# =========================================================

async def main():

    print("BOT STARTING...")

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await init_db()

    print("DB READY")

    await asyncio.gather(
        dp.start_polling(bot),
        reminder_loop(),
        delayed_loop(),
        npc_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
