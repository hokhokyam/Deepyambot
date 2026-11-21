
import os
import json
import tempfile
import subprocess
import logging
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI


# =====================================================
# CONFIGURATION
# =====================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video-agent")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get(
    "TELEGRAM_WEBHOOK_SECRET",
    "change-this-secret"
)

# DeepSeek API client
deepseek = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# Telegram bot
telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)

# FastAPI server for Render webhook
app = FastAPI()


# =====================================================
# DEEPSEEK AI
# =====================================================

SYSTEM_PROMPT = """
You are an AI video editing assistant.

Return ONLY valid JSON using this structure:

{
  "action": "compress",
  "start": "00:00:00",
  "end": "00:00:10",
  "caption": "My Video",
  "width": 720
}

Allowed actions:
- compress
- convert
- trim
- caption
- resize

Rules:
- Default action is compress.
- For trim, use the start and end times requested.
- For caption, use the text requested by the user.
- For resize, use the requested width.
- Never return shell commands.
- Never return executable code.
- Never return FFmpeg filters.
- Width must be between 320 and 1280.
"""


def get_ai_plan(user_instruction):
    response = deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": user_instruction[:1500]
            }
        ],
        response_format={"type": "json_object"}
    )

    result = json.loads(
        response.choices[0].message.content
    )

    allowed_actions = {
        "compress",
        "convert",
        "trim",
        "caption",
        "resize"
    }

    if result.get("action") not in allowed_actions:
        result["action"] = "compress"

    try:
        width = int(result.get("width", 720))
    except (ValueError, TypeError):
        width = 720

    result["width"] = max(320, min(1280, width))

    return result


# =====================================================
# VIDEO UTILITIES
# =====================================================

def parse_timestamp(value, fallback="00:00:00"):
    """
    Accept MM:SS or HH:MM:SS timestamps.
    """

    if not isinstance(value, str):
        return fallback

    parts = value.split(":")

    if len(parts) not in (2, 3):
        return fallback

    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return fallback

    if any(number < 0 for number in numbers):
        return fallback

    if any(number > 59 for number in numbers[1:]):
        return fallback

    return value


def timestamp_to_seconds(value):
    parts = [int(part) for part in value.split(":")]

    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds

    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def render_video(source, output, plan, temp_dir):
    action = plan.get("action", "compress")

    command = ["ffmpeg", "-y"]

    # Trim video
    if action == "trim":
        start = parse_timestamp(
            plan.get("start"),
            "00:00:00"
        )

        end = parse_timestamp(
            plan.get("end"),
            "00:00:15"
        )

        start_seconds = timestamp_to_seconds(start)
        end_seconds = timestamp_to_seconds(end)

        duration = end_seconds - start_seconds

        if duration <= 0:
            duration = 15

        command += [
            "-ss", start,
            "-i", str(source),
            "-t", str(duration)
        ]

    else:
        command += ["-i", str(source)]

    command += [
        "-map", "0:v:0",
        "-map", "0:a?"
    ]

    # Resize video
    if action == "resize":
        width = plan.get("width", 720)

        command += [
            "-vf",
            f"scale={width}:-2"
        ]

    # Add caption
    elif action == "caption":
        caption = str(
            plan.get("caption", "Edited with AI")
        )[:120]

        # Use a text file to avoid placing arbitrary
        # user text directly inside an FFmpeg filter.
        caption_file = Path(temp_dir) / "caption.txt"
        caption_file.write_text(
            caption,
            encoding="utf-8"
        )

        caption_path = str(caption_file).replace(
            "\\", "/"
        ).replace(":", r"\:")

        command += [
            "-vf",
            (
                "drawtext="
                f"textfile='{caption_path}':"
                "fontcolor=white:"
                "fontsize=30:"
                "borderw=2:"
                "x=(w-text_w)/2:"
                "y=h-80"
            )
        ]

    # Encode video
    command += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output)
    ]

    subprocess.run(
        command,
        check=True,
        timeout=600,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    return output


# =====================================================
# TELEGRAM COMMANDS
# =====================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    await message.reply_text(
        "Hello! I'm your AI Video Editing Bot. 🤖\n\n"
        "Send me a video with an editing instruction "
        "in its caption.\n\n"
        "Examples:\n"
        "• Compress this video\n"
        "• Trim from 00:00:03 to 00:00:12\n"
        "• Resize to 720px\n"
        "• Add caption: My travel vlog\n\n"
        "DeepSeek plans the edit and FFmpeg renders it."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await start_command(update, context)


# =====================================================
# NORMAL TEXT MESSAGES
# =====================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    if not message or not message.text:
        return

    user_text = message.text.lower().strip()

    if user_text in ["hi", "hello", "hey", "test"]:
        await message.reply_text(
            "Hello! 👋\n\n"
            "I'm online and ready.\n"
            "Send me a video with an editing instruction "
            "in the caption to start editing."
        )

    elif "help" in user_text:
        await help_command(update, context)

    else:
        await message.reply_text(
            "I received your message! ✅\n\n"
            "To edit a video, send a video file and "
            "write your instruction in its caption."
        )


# =====================================================
# VIDEO HANDLER
# =====================================================

async def video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    media = message.video

    if not media and message.document:
        mime_type = message.document.mime_type or ""

        if mime_type.startswith("video/"):
            media = message.document

    if not media:
        await message.reply_text(
            "Please send a video file."
        )
        return

    status = await message.reply_text(
        "📥 Receiving your video..."
    )

    try:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)

            input_file = temp_dir / "input.mp4"
            output_file = temp_dir / "edited.mp4"

            # Download video from Telegram
            telegram_file = await context.bot.get_file(
                media.file_id
            )

            await telegram_file.download_to_drive(
                input_file
            )

            instruction = (
                message.caption
                or "Compress this video"
            )

            await status.edit_text(
                "🧠 DeepSeek is planning your edit..."
            )

            # Ask DeepSeek
            plan = await asyncio.to_thread(
                get_ai_plan,
                instruction
            )

            await status.edit_text(
                "🎬 FFmpeg is editing your video..."
            )

            # Render with FFmpeg
            result = await asyncio.to_thread(
                render_video,
                input_file,
                output_file,
                plan,
                temp_dir
            )

            await status.edit_text(
                "📤 Sending your edited video..."
            )

            # Send edited video back
            with open(result, "rb") as video:
                await message.reply_video(
                    video=video,
                    caption=(
                        "✅ Video edited successfully!\n"
                        f"Action: {plan['action']}\n"
                        "Powered by DeepSeek + FFmpeg"
                    )
                )

            await status.delete()

    except subprocess.TimeoutExpired:
        await status.edit_text(
            "⏱️ Video processing timed out.\n"
            "Try a shorter or smaller video."
        )

    except Exception:
        logger.exception("Video processing error")

        await status.edit_text(
            "❌ I couldn't process your video.\n\n"
            "Try a short MP4 video.\n"
            "If the problem continues, check Render Logs."
        )


# =====================================================
# REGISTER TELEGRAM HANDLERS
# =====================================================

telegram_app.add_handler(
    CommandHandler("start", start_command)
)

telegram_app.add_handler(
    CommandHandler("help", help_command)
)

# Respond to ordinary text messages
telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    )
)

# Process video messages
telegram_app.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.VIDEO,
        video_handler
    )
)


# =====================================================
# FASTAPI / RENDER WEBHOOK
# =====================================================

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

    if RENDER_URL:
        webhook_url = (
            f"{RENDER_URL}/telegram/{WEBHOOK_SECRET}"
        )

        await telegram_app.bot.set_webhook(
            webhook_url
        )

        logger.info("Telegram webhook configured")

    logger.info("Bot started successfully")


@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.get("/")
async def health_check():
    return {
        "status": "online",
        "service": "DeepSeek Telegram Video Agent"
    }


@app.post("/telegram/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request
):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook secret"
        )

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.update_queue.put(update)

    return {"ok": True}
