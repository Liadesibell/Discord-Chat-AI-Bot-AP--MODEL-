import asyncio
import ctypes
import hashlib
import logging
import os
import re
import time
from pathlib import Path

import aiosqlite
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord_ai_bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
BOT_NAME = os.getenv("BOT_NAME", "Nemotron")
DATABASE_PATH = os.getenv("DATABASE_PATH", "memory.sqlite3")
INACTIVITY_DAYS = 3
MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_WORDS_PER_MESSAGE = 20
DISCORD_MESSAGE_LIMIT = 1900
_instance_mutex = None

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Set it in your .env file.")
if not NVIDIA_API_KEY:
    raise RuntimeError("NVIDIA_API_KEY is missing. Set it in your .env file.")

ai_client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=NVIDIA_API_KEY)


class ConversationStore:
    def __init__(self, path: str):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id TEXT PRIMARY KEY,
                last_activity REAL NOT NULL
            )
            """
        )
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            )
            """
        )
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user_id "
            "ON conversation_messages(user_id, id)"
        )
        await self.connection.commit()

    async def claim_message(self, message_id: str) -> bool:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, created_at) "
                "VALUES (?, ?)",
                (message_id, time.time()),
            )
            await self.connection.commit()
            return cursor.rowcount == 1

    async def get_context(self, user_id: str) -> list[dict[str, str]]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT role, content FROM conversation_messages
                WHERE user_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, MAX_CONTEXT_MESSAGES),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "role": row[0],
                    "content": " ".join(
                        row[1].split()[:MAX_CONTEXT_WORDS_PER_MESSAGE]
                    ),
                }
                for row in reversed(rows)
            ]

    async def add_message(self, user_id: str, role: str, content: str) -> None:
        assert self.connection is not None
        now = time.time()
        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO conversation_messages (user_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, role, content, now),
            )
            await self.connection.execute(
                """
                INSERT INTO user_activity (user_id, last_activity) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_activity = excluded.last_activity
                """,
                (user_id, now),
            )
            await self.connection.commit()

    async def delete_inactive(self) -> int:
        assert self.connection is not None
        cutoff = time.time() - INACTIVITY_DAYS * 24 * 60 * 60
        async with self.lock:
            cursor = await self.connection.execute(
                "SELECT COUNT(*) FROM user_activity WHERE last_activity < ?",
                (cutoff,),
            )
            expired_count = (await cursor.fetchone())[0]
            if expired_count:
                await self.connection.execute(
                    """
                    DELETE FROM conversation_messages
                    WHERE user_id IN (
                        SELECT user_id FROM user_activity WHERE last_activity < ?
                    )
                    """,
                    (cutoff,),
                )
                await self.connection.execute(
                    "DELETE FROM user_activity WHERE last_activity < ?",
                    (cutoff,),
                )
                await self.connection.commit()
        return expired_count

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None


intents = discord.Intents.default()
intents.message_content = True
store = ConversationStore(DATABASE_PATH)
name_pattern = re.compile(rf"(?<!\w){re.escape(BOT_NAME)}(?!\w)", re.IGNORECASE)


class AIBot(commands.Bot):
    async def setup_hook(self) -> None:
        await store.initialize()
        deleted_count = await store.delete_inactive()
        if deleted_count:
            logger.info("Deleted stored conversations for %s inactive users.", deleted_count)
        remove_inactive_users.start()


bot = AIBot(command_prefix="!", intents=intents)


def acquire_single_instance():
    if os.name != "nt":
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(
        None,
        False,
        f"Local\\DiscordAIChatBot-{hashlib.sha256(DISCORD_TOKEN.encode()).hexdigest()[:16]}",
    )
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return None
    return kernel32, handle


@tasks.loop(hours=1)
async def remove_inactive_users() -> None:
    deleted_count = await store.delete_inactive()
    if deleted_count:
        logger.info("Deleted stored conversations for %s inactive users.", deleted_count)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


def extract_message_content(message: discord.Message | object | None) -> str:
    if message is None:
        return ""

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif hasattr(part, "text") and isinstance(part.text, str):
                text_parts.append(part.text)
            elif isinstance(part, dict):
                text_value = part.get("text")
                if isinstance(text_value, str):
                    text_parts.append(text_value)
        return "".join(text_parts)
    return ""


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    await bot.process_commands(message)

    was_mentioned = bot.user is not None and bot.user in message.mentions
    was_named = name_pattern.search(message.content) is not None
    if not (was_mentioned or was_named):
        return

    if not await store.claim_message(str(message.id)):
        logger.info("Ignoring duplicate event for Discord message %s.", message.id)
        return

    user_id = str(message.author.id)
    await store.add_message(user_id, "user", message.content)
    context = await store.get_context(user_id)
    try:
        completion = await asyncio.to_thread(
            ai_client.chat.completions.create,
            model=NVIDIA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Türkçe ve çok kısa yanıt ver; normalde en fazla 2 cümle "
                        "kullan. Kullanıcı özellikle ayrıntı istemedikçe açıklama "
                        "ve liste yazma. Her mesaja yalnızca tek bir yanıt üret. "
                        "Kullanıcıya hitap ederken '413' yazma; bunun yerine "
                        "'hayatım' de."
                    ),
                },
                *context,
            ],
            temperature=0.7,
            top_p=0.9,
            max_tokens=120,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        answer = extract_message_content(completion.choices[0].message).strip()
        if not answer or not answer.strip():
            raise RuntimeError("The AI provider returned an empty response.")
        if len(answer) > DISCORD_MESSAGE_LIMIT:
            answer = answer[: DISCORD_MESSAGE_LIMIT - 3].rstrip() + "..."
    except Exception:
        logger.exception("AI request failed for Discord user %s.", user_id)
        await message.reply(
            "Şu anda yanıt oluşturamıyorum. Lütfen biraz sonra tekrar dene.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    await store.add_message(user_id, "assistant", answer)
    await message.reply(
        answer,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def main() -> None:
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        await store.close()


if __name__ == "__main__":
    mutex = acquire_single_instance()
    if os.name == "nt" and mutex is None:
        logger.error(
            "This bot is already running with the same Discord token. "
            "Close the other bot window before starting another instance."
        )
        raise SystemExit(1)
    try:
        asyncio.run(main())
    finally:
        if mutex is not None:
            kernel32, handle = mutex
            kernel32.CloseHandle(handle)
