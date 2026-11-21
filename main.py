
import os
import json
import uuid
import shutil
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
COARENA_API_KEY = os.getenv("COARENA_API_KEY", "")
COARENA_MODEL = os.getenv("COARENA_MODEL", "deepseek-chat")
COARENA_BASE_URL = os.getenv(
    "COARENA_BASE_URL",
    "https://coarena.ai/api/v1"
)

PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-this-secret")

WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/deepyam"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deepyam")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

if not COARENA_API_KEY:
    raise RuntimeError("Missing COARENA_API_KEY")

client = AsyncOpenAI(
    api_key=COARENA_API_KEY,
    base_url=COARENA_BASE_URL,
)

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --------------------------------------------------
# CONVERSATION STATE
# --------------------------------------------------
# RAM-based for this starter deployment.
# Render restart clears this state.

sessions: dict[int, dict[str, Any]] = {}

def get_session(user_id: int):
    if user_id not in sessions:
        sessions[user_id] = {
            "messages": [],
            "video": None,
            "platform": "tiktok",
        }
    return sessions[user_id]

# --------------------------------------------------
# FFMPEG HELPERS
# --------------------------------------------------

def ffmpeg_exists():
    return shutil.which("ffmpeg") is not None

def run_ffmpeg(args: list[str]):
    command = ["ffmpeg", "-y", "-hide_banner"] + args
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:])
    return result

def probe_video(path: str):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of", "json",
        path,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("Hindi mabasa ang video metadata.")
    return json.loads(result.stdout)

def output_path(user_id: int):
    return str(WORK_DIR / f"{user_id}_{uuid.uuid4().hex[:8]}_edited.mp4")

def require_video(user_id: int):
    video = get_session(user_id).get("video")
    if not video or not os.path.exists(video):
        raise RuntimeError(
            "Wala akong active video. Mag-send muna ng video sa chat."
        )
    return video

# --------------------------------------------------
# EDITING TOOLS
# --------------------------------------------------

def tool_video_info(user_id: int):
    path = require_video(user_id)
    return probe_video(path)

def tool_compress(user_id: int, quality: str = "high"):
    source = require_video(user_id)
    target = output_path(user_id)

    presets = {
        "high": "18",
        "balanced": "23",
        "small": "28",
    }
    crf = presets.get(quality.lower(), "20")

    run_ffmpeg([
        "-i", source,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", crf,
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        target,
    ])

    get_session(user_id)["video"] = target
    return {"output": target, "quality": quality}

def tool_tiktok(user_id: int):
    source = require_video(user_id)
    target = output_path(user_id)

    # Center-crop to vertical 9:16, then scale to 1080x1920.
    run_ffmpeg([
        "-i", source,
        "-vf",
        "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        target,
    ])

    get_session(user_id)["video"] = target
    return {"output": target, "format": "vertical 9:16"}

def tool_brightness(user_id: int, amount: float = 0.08):
    source = require_video(user_id)
    target = output_path(user_id)

    amount = max(-0.5, min(0.5, float(amount)))

    run_ffmpeg([
        "-i", source,
        "-vf", f"eq=brightness={amount}:contrast=1.03",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        target,
    ])

    get_session(user_id)["video"] = target
    return {"output": target, "brightness": amount}

def tool_trim(user_id: int, start: float, end: float):
    source = require_video(user_id)
    target = output_path(user_id)

    start = max(0, float(start))
    end = max(start + 0.1, float(end))
    duration = end - start

    run_ffmpeg([
        "-ss", str(start),
        "-i", source,
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-movflags", "+faststart",
        target,
    ])

    get_session(user_id)["video"] = target
    return {"output": target, "start": start, "end": end}

def tool_mute(user_id: int):
    source = require_video(user_id)
    target = output_path(user_id)

    run_ffmpeg([
        "-i", source,
        "-an",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-movflags", "+faststart",
        target,
    ])

    get_session(user_id)["video"] = target
    return {"output": target, "audio": "muted"}

def tool_add_text(user_id: int, text: str):
    source = require_video(user_id)
    target = output_path(user_id)

    # Escape characters for FFmpeg drawtext.
    safe = (
        str(text)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )

    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    run_ffmpeg([
        "-i", source,
        "-vf",
        f"drawtext=fontfile={font}:text='{safe}':"
        "fontcolor=white:fontsize=48:"
        "borderw=3:bordercolor=black:"
        "x=(w-text_w)/2:y=h*0.82",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        target,
    ])

    get_session(user_id)["video"] = target
    return {"output": target, "overlay": text}

# --------------------------------------------------
# AI TOOL DEFINITIONS
# --------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "video_info",
            "description": "Inspect the active uploaded video.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compress_video",
            "description": "Compress the active video. high preserves quality, balanced, small.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quality": {
                        "type": "string",
                        "enum": ["high", "balanced", "small"],
                    }
                },
                "required": ["quality"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_tiktok",
            "description": "Convert active video to vertical TikTok 9:16 format.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_brightness",
            "description": "Adjust brightness. Typical amount 0.05 to 0.20.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "minimum": -0.5,
                        "maximum": 0.5,
                    }
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trim_video",
            "description": "Trim video using start and end seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                },
                "required": ["start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mute_video",
            "description": "Remove audio from the active video.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_text_overlay",
            "description": "Add short text overlay to active video.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
]

SYSTEM_PROMPT = """
You are Deepyam, a friendly and capable AI video editor and social media assistant.

Understand natural Filipino/Tagalog, English, and mixed Taglish.
Never require slash commands. The user can speak naturally.

You can:
- Answer questions conversationally.
- Use video editing tools when the user requests an actual edit.
- Use video_info before editing if duration or dimensions matter.
- Make TikTok-ready videos.
- Create captions, hooks, titles, descriptions, hashtags, and keywords
  for YouTube, TikTok, or Facebook.
- Give exactly five hashtags when the user asks for top five.
- Explain that hashtag ranking is your relevance-based recommendation,
  not live trend data, unless a live trends source is connected.

If the user asks to edit a video, use tools rather than merely claiming
the edit is complete.
If no video is uploaded, ask the user to send one.
If a tool fails, tell the user honestly.
After successful editing, say that the edited video will be sent.

Keep replies natural, concise, and helpful.
Do not invent video contents that you have not inspected.
"""

# --------------------------------------------------
# AI CONVERSATION + TOOL LOOP
# --------------------------------------------------

async def ask_ai(user_id: int, user_text: str):
    session = get_session(user_id)

    platform = session.get("platform", "tiktok")
    active_video = session.get("video")

    context_note = (
        f"Selected platform: {platform}. "
        f"Active video available: "
        f"{bool(active_video and os.path.exists(active_video))}."
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + context_note}
    ]

    messages.extend(session["messages"][-16:])
    messages.append({"role": "user", "content": user_text})

    for _ in range(6):
        response = await client.chat.completions.create(
            model=COARENA_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.4,
        )

        message = response.choices[0].message

        if not message.tool_calls:
            answer = message.content or "Sige."
            session["messages"].append(
                {"role": "user", "content": user_text}
            )
            session["messages"].append(
                {"role": "assistant", "content": answer}
            )
            session["messages"] = session["messages"][-24:]
            return answer

        messages.append(message.model_dump(exclude_none=True))

        for call in message.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")

            try:
                result = await execute_tool(user_id, name, args)
                result_text = json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                log.exception("Tool failed: %s", name)
                result_text = json.dumps({
                    "error": str(exc)
                }, ensure_ascii=False)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result_text,
            })

    return "Natapos ang tool processing limit. Pakisabi kung ano ang susunod."

async def execute_tool(user_id: int, name: str, args: dict):
    if name == "video_info":
        return await asyncio.to_thread(tool_video_info, user_id)

    if name == "compress_video":
        return await asyncio.to_thread(
            tool_compress, user_id, args["quality"]
        )

    if name == "make_tiktok":
        return await asyncio.to_thread(tool_tiktok, user_id)

    if name == "adjust_brightness":
        return await asyncio.to_thread(
            tool_brightness, user_id, args["amount"]
        )

    if name == "trim_video":
        return await asyncio.to_thread(
            tool_trim, user_id, args["start"], args["end"]
        )

    if name == "mute_video":
        return await asyncio.to_thread(tool_mute, user_id)

    if name == "add_text_overlay":
        return await asyncio.to_thread(
            tool_add_text, user_id, args["text"]
        )

    raise ValueError(f"Unknown tool: {name}")



# --------------------------------------------------
# TELEGRAM HANDLERS
# --------------------------------------------------

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Hi! I'm Deepyam AI Video + Social Studio.\n\n"
        "Send a video and tell me naturally what you want.\n"
        "Example: 'Compress mo pero preserve quality.'\n"
        "Example: 'Gawin mong TikTok-ready.'\n"
        "Example: 'Give me a caption and top 5 hashtags.'\n\n"
        "You don't need slash commands to talk to me."
    )

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Send a video, then tell me what to do in normal Tagalog "
        "or English.\n\n"
        "I can currently:\n"
        "• Compress video\n"
        "• Convert to TikTok vertical format\n"
        "• Adjust brightness\n"
        "• Trim using seconds\n"
        "• Mute audio\n"
        "• Add text overlay\n"
        "• Generate captions, hashtags, and keywords\n\n"
        "Optional: /platform tiktok|youtube|facebook"
    )

async def platform_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    session = get_session(user_id)

    if not context.args:
        await update.message.reply_text(
            f"Current platform: {session['platform']}\n"
            "Use /platform tiktok, /platform youtube, "
            "or /platform facebook."
        )
        return

    platform = context.args[0].lower()
    allowed = {"tiktok", "youtube", "facebook"}

    if platform not in allowed:
        await update.message.reply_text(
            "Platform options: tiktok, youtube, facebook."
        )
        return

    session["platform"] = platform
    await update.message.reply_text(
        f"Platform set to {platform.title()}."
    )

async def video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    message = update.message

    media = message.video or message.document

    if not media:
        return

    if message.document:
        filename = (message.document.file_name or "").lower()
        if not filename.endswith((".mp4", ".mov", ".mkv", ".webm")):
            await message.reply_text(
                "Please send a supported video file."
            )
            return

    status = await message.reply_text(
        "Video received. Preparing it for editing..."
    )

    try:
        telegram_file = await context.bot.get_file(media.file_id)

        local_path = str(
            WORK_DIR / f"{user_id}_{uuid.uuid4().hex[:8]}_input.mp4"
        )

        await telegram_file.download_to_drive(local_path)

        # Validate file using ffprobe.
        await asyncio.to_thread(probe_video, local_path)

        get_session(user_id)["video"] = local_path

        await status.edit_text(
            "Video ready! Tell me what you'd like me to do."
        )

    except Exception as exc:
        log.exception("Video download/validation failed")
        await status.edit_text(
            "Hindi ko ma-load ang video. "
            "Try sending it as an MP4 video file.\n"
            f"Details: {str(exc)[:300]}"
        )

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    message = update.message

    if not message or not message.text:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    session = get_session(user_id)
    previous_video = session.get("video")

    status = await message.reply_text("Thinking...")

    try:
        answer = await ask_ai(user_id, user_text)

        await status.edit_text(answer[:4000])

        current_video = session.get("video")

        # If a tool produced a new file, send the actual video.
        if (
            current_video
            and os.path.exists(current_video)
            and current_video != previous_video
        ):
            await message.reply_text(
                "Here's your edited video:"
            )

            with open(current_video, "rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    supports_streaming=True,
                    caption="Edited by Deepyam AI",
                )

    except Exception as exc:
        log.exception("AI request failed")
        await status.edit_text(
            "Nagkaroon ng error sa AI request.\n"
            "Check your Coarena model/API settings.\n\n"
            f"Details: {str(exc)[:500]}"
        )

# --------------------------------------------------
# WEBHOOK
# --------------------------------------------------

telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("platform", platform_command))

telegram_app.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.ALL,
        video_handler,
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler,
    )
)

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

    if PUBLIC_URL:
        await telegram_app.bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram",
            secret_token=WEBHOOK_SECRET,
        )
        log.info("Telegram webhook configured.")
    else:
        log.warning(
            "RENDER_EXTERNAL_URL missing. Webhook not configured."
        )

@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.post("/telegram")
async def telegram_webhook(request: Request):
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if secret != WEBHOOK_SECRET:
        return {"ok": False, "error": "unauthorized"}

    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)

    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def health():
    return {
        "status": "Deepyam is running",
        "ffmpeg": ffmpeg_exists(),
    }


