
import os
import re
import json
import uuid
import asyncio
import logging
import sqlite3
import shutil
import subprocess
import threading
import asyncio

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AI_KEY = os.environ["COARENA_API_KEY"]
AI_MODEL = os.getenv("COARENA_MODEL", "deepseek-chat")
AI_BASE_URL = os.getenv(
    "COARENA_BASE_URL",
    "https://coarena.ai/api/v1",
)

PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "replace-this-secret")

# Set WORK_DIR=/var/data/deepyam if you have a Render disk
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/deepyam"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(WORK_DIR / "deepyam.sqlite3")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deepyam")

client = AsyncOpenAI(
    api_key=AI_KEY,
    base_url=AI_BASE_URL,
)

app = FastAPI()
bot_app = Application.builder().token(BOT_TOKEN).build()

# Prevent simultaneous edits/uploads by the same Telegram user.
user_locks: dict[int, asyncio.Lock] = {}

def get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]

# =========================================================
# SQLITE STATE
# =========================================================

def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                active_video TEXT,
                platform TEXT DEFAULT 'tiktok',
                history TEXT DEFAULT '[]'
            )
        """)

def ensure_user(user_id: int):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
            (user_id,),
        )

def get_user(user_id: int) -> dict:
    ensure_user(user_id)
    with db() as con:
        row = con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return dict(row)

def set_video(user_id: int, path: str):
    ensure_user(user_id)
    with db() as con:
        con.execute(
            "UPDATE users SET active_video=? WHERE user_id=?",
            (path, user_id),
        )

def get_video(user_id: int) -> str | None:
    data = get_user(user_id)
    path = data.get("active_video")
    if path and os.path.isfile(path):
        return path
    return None

def set_platform(user_id: int, platform: str):
    ensure_user(user_id)
    with db() as con:
        con.execute(
            "UPDATE users SET platform=? WHERE user_id=?",
            (platform, user_id),
        )

def get_history(user_id: int) -> list:
    data = get_user(user_id)
    try:
        return json.loads(data.get("history") or "[]")
    except Exception:
        return []

def save_history(user_id: int, history: list):
    history = history[-24:]
    with db() as con:
        con.execute(
            "UPDATE users SET history=? WHERE user_id=?",
            (json.dumps(history, ensure_ascii=False), user_id),
        )

init_db()

# =========================================================
# FFMPEG
# =========================================================

def require_video(user_id: int) -> str:
    path = get_video(user_id)
    if not path:
        raise RuntimeError(
            "Wala akong mahanap na active video file. "
            "I-upload muli ang video at hintaying matapos ang upload."
        )
    return path

def ffprobe(path: str) -> dict:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is missing. Install FFmpeg.")

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError("Hindi mabasa ang video file.")

    return json.loads(result.stdout)

def ffmpeg(args: list[str]):
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner"] + args,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-1800:])

def new_output(user_id: int) -> str:
    return str(
        WORK_DIR / f"{user_id}_{uuid.uuid4().hex[:10]}_edit.mp4"
    )

def register_output(user_id: int, path: str) -> dict:
    if not os.path.isfile(path) or os.path.getsize(path) < 1000:
        raise RuntimeError("FFmpeg did not produce a valid output.")
    ffprobe(path)
    set_video(user_id, path)
    return {"output": path, "success": True}

def compress_video(user_id: int, quality: str = "small"):
    source = require_video(user_id)
    target = new_output(user_id)

    crf_map = {
        "high": 18,
        "balanced": 23,
        "small": 28,
    }
    crf = crf_map.get(quality.lower(), 28)

    ffmpeg([
        "-i", source,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        target,
    ])
    return register_output(user_id, target)

def make_tiktok(user_id: int):
    source = require_video(user_id)
    target = new_output(user_id)

    ffmpeg([
        "-i", source,
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-movflags", "+faststart",
        target,
    ])
    return register_output(user_id, target)

def brightness(user_id: int, amount: float = 0.10):
    source = require_video(user_id)
    target = new_output(user_id)
    amount = max(-0.5, min(0.5, float(amount)))

    ffmpeg([
        "-i", source,
        "-vf", f"eq=brightness={amount}:contrast=1.03",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        target,
    ])
    return register_output(user_id, target)

def mute_video(user_id: int):
    source = require_video(user_id)
    target = new_output(user_id)

    ffmpeg([
        "-i", source,
        "-an",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-movflags", "+faststart",
        target,
    ])
    return register_output(user_id, target)

def trim_video(user_id: int, start: float, end: float):
    source = require_video(user_id)
    target = new_output(user_id)

    start = max(0, float(start))
    end = float(end)
    if end <= start:
        raise ValueError("End time must be greater than start time.")

    ffmpeg([
        "-ss", str(start),
        "-i", source,
        "-t", str(end - start),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-movflags", "+faststart",
        target,
    ])
    return register_output(user_id, target)

# =========================================================
# NATURAL-LANGUAGE EDIT ROUTER
# =========================================================

def detect_edit(text: str):
    t = text.lower().strip()

    # Compression: "compress small", "make file smaller"
    if any(x in t for x in [
        "compress small", "compress it small",
        "make it smaller", "smaller file",
        "compress mo", "i-compress",
        "compress this", "compress video",
    ]):
        quality = "small"
        if "high quality" in t or "preserve quality" in t:
            quality = "high"
        elif "balanced" in t:
            quality = "balanced"
        return ("compress", {"quality": quality})

    if any(x in t for x in [
        "tiktok-ready", "tik tok ready",
        "make it tiktok", "pang-tiktok",
        "vertical 9:16",
    ]):
        return ("tiktok", {})

    if any(x in t for x in [
        "brighter", "brighten", "more bright",
        "mas maliwanag", "liwanagan",
    ]):
        return ("brightness", {"amount": 0.12})

    if any(x in t for x in [
        "mute video", "remove audio",
        "mute mo", "tanggalin audio",
    ]):
        return ("mute", {})

    # Example: trim from 5 to 20 seconds
    match = re.search(
        r"(?:trim|cut).*?(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?"
        r".*?(?:to|until|-)\s*(\d+(?:\.\d+)?)",
        t,
    )
    if match:
        return (
            "trim",
            {
                "start": float(match.group(1)),
                "end": float(match.group(2)),
            },
        )

    return None

async def run_edit(user_id: int, action: str, args: dict):
    if action == "compress":
        return await asyncio.to_thread(
            compress_video, user_id, args.get("quality", "small")
        )
    if action == "tiktok":
        return await asyncio.to_thread(make_tiktok, user_id)
    if action == "brightness":
        return await asyncio.to_thread(
            brightness, user_id, args.get("amount", 0.12)
        )
    if action == "mute":
        return await asyncio.to_thread(mute_video, user_id)
    if action == "trim":
        return await asyncio.to_thread(
            trim_video, user_id, args["start"], args["end"]
        )
    raise ValueError("Unknown edit action.")

# =========================================================
# AI CHAT
# =========================================================

SYSTEM = """
You are Deepyam, a helpful AI video editor and social media assistant.
Understand natural Tagalog, English, and Taglish.
Speak naturally and remember the conversation context.
The user's selected platform is included in the context.
Create captions, titles, descriptions, hooks, five hashtags,
and keywords when requested.
Hashtag suggestions are relevance-based, not live trend rankings.
Never claim that a video was edited unless a tool succeeded.
If user asks to edit but no video exists, ask them to upload it.
"""

async def ask_ai(user_id: int, text: str) -> str:
    user = get_user(user_id)
    history = get_history(user_id)
    video_exists = bool(get_video(user_id))

    messages = [
        {
            "role": "system",
            "content": (
                SYSTEM
                + f"\nPlatform: {user.get('platform', 'tiktok')}"
                + f"\nActive video exists: {video_exists}"
            ),
        }
    ]
    messages.extend(history[-16:])
    messages.append({"role": "user", "content": text})

    response = await client.chat.completions.create(
        model=AI_MODEL,
        messages=messages,
        temperature=0.5,
    )

    answer = response.choices[0].message.content or "Sige."

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": answer})
    save_history(user_id, history)

    return answer

# =========================================================
# TELEGRAM HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm Deepyam AI Video + Social Studio.\n\n"
        "Send a video, then tell me naturally what to do.\n"
        "Example: Compress small.\n"
        "Example: Make it TikTok-ready.\n"
        "Example: Give me a caption and top 5 hashtags."
    )

async def platform_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        data = get_user(user_id)
        await update.message.reply_text(
            f"Platform: {data.get('platform', 'tiktok')}"
        )
        return

    platform = context.args[0].lower()
    if platform not in ("tiktok", "youtube", "facebook"):
        await update.message.reply_text(
            "Choose tiktok, youtube, or facebook."
        )
        return

    set_platform(user_id, platform)
    await update.message.reply_text(f"Platform set to {platform.title()}.")

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    media = message.video or message.document

    if not media:
        return

    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith((".mp4", ".mov", ".mkv", ".webm")):
            await message.reply_text("Please send a video file.")
            return

    async with get_lock(user_id):
        status = await message.reply_text("Downloading video...")

        try:
            file = await context.bot.get_file(media.file_id)
            path = str(WORK_DIR / f"{user_id}_{uuid.uuid4().hex}_input.mp4")
            await file.download_to_drive(path)

            # Verify completed download and valid video.
            if not os.path.isfile(path) or os.path.getsize(path) < 1000:
                raise RuntimeError("Download appears incomplete.")

            metadata = await asyncio.to_thread(ffprobe, path)

            # Only set active video AFTER successful validation.
            set_video(user_id, path)

            duration = metadata.get("format", {}).get("duration", "?")
            await status.edit_text(
                f"Video ready! Duration: {duration} seconds.\n"
                "Tell me what you'd like me to do."
            )

        except Exception as exc:
            log.exception("Upload failed")
            await status.edit_text(
                f"Video upload/validation failed: {str(exc)[:400]}"
            )

async def send_edited_video(
    update: Update,
    path: str,
    status,
):
    if not os.path.isfile(path):
        await status.edit_text(
            "Editing finished, but output file is missing."
        )
        return

    size = os.path.getsize(path)
    if size > 49 * 1024 * 1024:
        await status.edit_text(
            "The edited file is too large for Telegram video upload. "
            "Try compressing it further."
        )
        return

    await status.edit_text("Editing complete. Sending your video...")

    with open(path, "rb") as f:
        await update.message.reply_video(
            video=f,
            supports_streaming=True,
            caption="Edited by Deepyam AI",
        )

    await status.edit_text("Done! Your edited video was sent.")

async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return

    # Support /ai natural text.
    if text.startswith("/ai"):
        text = text[3:].strip()
    if not text:
        await update.message.reply_text("Yes? Tell me what you need.")
        return

    async with get_lock(user_id):
        status = await update.message.reply_text("Thinking...")

        try:
            # Deterministic edit routing fixes tool-call dependence.
            edit = detect_edit(text)

            if edit:
                action, args = edit

                if not get_video(user_id):
                    await status.edit_text(
                        "Wala akong active video file sa system. "
                        "I-upload muli ang video at hintaying lumabas "
                        "'Video ready' bago mag-request ng edit."
                    )
                    return

                await status.edit_text("Editing your video with FFmpeg...")

                result = await run_edit(user_id, action, args)
                output = result["output"]

                await send_edited_video(update, output, status)
                return

            answer = await ask_ai(user_id, text)
            await status.edit_text(answer[:4000])

        except Exception as exc:
            log.exception("Message processing failed")
            await status.edit_text(
                "May error habang pinoproseso ang request:\n"
                f"{str(exc)[:700]}"
            )

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /ai hi routes into the same text pipeline
    await process_text(update, context)

# =========================================================
# WEBHOOK
# =========================================================

bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("platform", platform_cmd))
bot_app.add_handler(CommandHandler("ai", ai_command))

bot_app.add_handler(
    MessageHandler(filters.VIDEO | filters.Document.ALL, upload_video)
)
bot_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, process_text)
)

@app.on_event("startup")
async def on_startup():
    await bot_app.initialize()
    await bot_app.start()

    if PUBLIC_URL:
        await bot_app.bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram",
            secret_token=WEBHOOK_SECRET,
        )
        log.info("Webhook configured.")

@app.on_event("shutdown")
async def on_shutdown():
    await bot_app.stop()
    await bot_app.shutdown()

@app.post("/telegram")
async def webhook(request: Request):
    token = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if token != WEBHOOK_SECRET:
        return {"ok": False}

    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def health():
    return {
        "status": "Deepyam running",
        "active_database": DB_PATH,
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }
