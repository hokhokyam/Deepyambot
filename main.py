import os
import re
import logging
import asyncio
import tempfile
import shutil
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("coarena-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
COARENA_API_KEY = os.getenv("COARENA_API_KEY", "").strip()
COARENA_MODEL = os.getenv("COARENA_MODEL", "").strip()  # exact model marked Free in Coarena
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not COARENA_API_KEY:
    raise RuntimeError("Missing COARENA_API_KEY")
if not COARENA_MODEL:
    raise RuntimeError("Missing COARENA_MODEL; set exact Coarena Free model ID")

ai = AsyncOpenAI(
    api_key=COARENA_API_KEY,
    base_url="https://coarena.ai/api/v1",
    timeout=90,
    max_retries=2,
)

SYSTEM_PROMPT = (
    "You are a helpful Telegram assistant. Answer naturally in the user's language, "
    "usually Filipino/Tagalog or English. Be concise and honest. The bot can currently "
    "execute these video actions: compress and convert to MP4. If asked for another "
    "edit, explain it is not implemented yet. Never claim an edit was completed unless "
    "the bot actually ran FFmpeg and sent the result."
)
history: dict[int, list[dict[str, str]]] = {}
last_video: dict[int, str] = {}  # latest downloaded video path per chat
MAX_HISTORY = 12
telegram_app: Application | None = None


async def ai_reply(chat_id: int, text: str) -> str:
    messages = history.setdefault(chat_id, [])
    messages.append({"role": "user", "content": text})
    messages[:] = messages[-MAX_HISTORY:]
    try:
        response = await ai.chat.completions.create(
            model=COARENA_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            temperature=0.65,
            max_tokens=700,
        )
        answer = (response.choices[0].message.content or "").strip()
        if not answer:
            answer = "Walang text response ang model. Subukan ulit."
        messages.append({"role": "assistant", "content": answer})
        messages[:] = messages[-MAX_HISTORY:]
        return answer
    except Exception as e:
        log.exception("Coarena API call failed")
        messages.pop()
        return (
            "Hindi makasagot ang Coarena ngayon. I-check ang API key, exact Free model ID, "
            "at daily limit. Error type: " + type(e).__name__
        )


def parse_video_action(text: str) -> str | None:
    t = (text or "").lower()
    if any(x in t for x in ["compress", "compression", "paliitin", "liitan", "reduce video size", "smaller file"]):
        return "compress"
    if any(x in t for x in ["convert mp4", "to mp4", "gawing mp4", "mp4 format"]):
        return "mp4"
    return None


async def run_video(chat_id: int, message, action: str):
    source = last_video.get(chat_id)
    if not source or not os.path.isfile(source):
        await message.reply_text("Wala akong naka-save na video para sa chat na ito. I-send muna ang video, pagkatapos sabihin ang 'compress' o 'convert to MP4'.")
        return

    status = await message.reply_text("Sige, pinoproseso ko na ang video gamit ang FFmpeg…")
    with tempfile.TemporaryDirectory(prefix="edit_") as tmp:
        output = Path(tmp) / ("compressed.mp4" if action == "compress" else "converted.mp4")
        if action == "compress":
            command = [
                "ffmpeg", "-y", "-i", source,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                str(output)
            ]
        else:
            command = [
                "ffmpeg", "-y", "-i", source,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                str(output)
            ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, err = await proc.communicate()
            if proc.returncode or not output.exists():
                log.error("ffmpeg error: %s", err.decode(errors="ignore")[-3000:])
                await status.edit_text("Hindi natapos ang FFmpeg processing. Tingnan ang Render Logs.")
                return
            with output.open("rb") as f:
                await message.reply_document(
                    document=f,
                    filename=output.name,
                    caption="Tapos na ang actual video processing! 🎬"
                )
            await status.delete()
        except Exception:
            log.exception("Video action failed")
            await status.edit_text("Nag-error habang nag-e-edit. Tingnan ang Render Logs.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Hi! AI chat + basic video processor ako.\n\n"
            "1) I-send muna ang video.\n"
            "2) Pagkatapos, sabihin: 'compress ang video' o 'convert to MP4'.\n"
            "3) Ipapagana ko ang FFmpeg at ibabalik ang processed file.\n\n"
            "Commands: /help, /reset, /ai <message>"
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Video workflow:\n"
            "• Send video first (video file is kept temporarily)\n"
            "• Then message: compress / paliitin ang video\n"
            "• Or: convert to MP4\n"
            "Supported now: compression and MP4 conversion only.\n\n"
            "Plain text = conversational AI. /ai <text> = AI chat or supported edit command.\n"
            "/reset clears AI context and the remembered video."
        )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid is not None:
        history.pop(cid, None)
        old = last_video.pop(cid, None)
        if old:
            try:
                os.remove(old)
            except OSError:
                pass
    if update.effective_message:
        await update.effective_message.reply_text("Na-reset ang chat context at naka-save na video.")


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.effective_message.reply_text("Gamit: /ai <tanong o video edit instruction>")
        return
    action = parse_video_action(prompt)
    if action:
        await run_video(update.effective_chat.id, update.effective_message, action)
    else:
        pending = await update.effective_message.reply_text("...")
        result = await ai_reply(update.effective_chat.id, prompt)
        await pending.edit_text(result)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.text:
        return
    text = message.text.strip()
    action = parse_video_action(text)
    if action:
        await run_video(chat.id, message, action)
        return
    pending = await message.reply_text("...")
    result = await ai_reply(chat.id, text)
    try:
        await pending.edit_text(result)
    except Exception:
        await message.reply_text(result)


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    media = message.video
    if not media and message.document and (message.document.mime_type or "").startswith("video/"):
        media = message.document
    if not media:
        return
    # Remove prior downloaded file for this chat, if any.
    old = last_video.get(chat.id)
    if old:
        try:
            os.remove(old)
        except OSError:
            pass
    # Store video under /tmp; Render's ephemeral filesystem may clear on restart/sleep.
    suffix = ".mp4"
    path = tempfile.mktemp(prefix=f"telegram_{chat.id}_", suffix=suffix)
    try:
        file_obj = await context.bot.get_file(media.file_id)
        await file_obj.download_to_drive(custom_path=path)
        last_video[chat.id] = path
        await message.reply_text(
            "Video received and temporarily saved. 🎞️\n"
            "Now send a separate message: 'compress ang video' or 'convert to MP4'.\n"
            "Kapag sinabi mo ang instruction, actual FFmpeg processing ang tatakbo."
        )
    except Exception:
        log.exception("Video download failed")
        await message.reply_text("Hindi ko ma-download ang video. Check Telegram/Render logs.")


def build_bot() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app


@asynccontextmanager
async def lifespan(api: FastAPI):
    global telegram_app
    telegram_app = build_bot()
    await telegram_app.initialize()
    await telegram_app.start()
    if RENDER_EXTERNAL_URL:
        await telegram_app.bot.set_webhook(
            url=f"{RENDER_EXTERNAL_URL}/telegram",
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message"],
        )
        log.info("Telegram webhook set")
    else:
        log.warning("RENDER_EXTERNAL_URL missing; webhook not set")
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(title="Coarena Telegram Video Bot", lifespan=lifespan)


@app.get("/")
async def health():
    return {"ok": True, "service": "coarena-telegram-video-bot"}


@app.post("/telegram")
async def webhook(request: Request):
    if telegram_app is None:
        raise HTTPException(503, "Bot is starting")
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid webhook secret")
    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
