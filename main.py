import os
import re
import json
import logging
import asyncio
import tempfile
import subprocess
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
COARENA_MODEL = os.getenv("COARENA_MODEL", "").strip()  # Must be a model marked Free in Coarena
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
if not COARENA_API_KEY:
    raise RuntimeError("Missing COARENA_API_KEY environment variable")
if not COARENA_MODEL:
    raise RuntimeError("Missing COARENA_MODEL environment variable. Set it to a Coarena model marked Free.")

ai = AsyncOpenAI(
    api_key=COARENA_API_KEY,
    base_url="https://coarena.ai/api/v1",
    timeout=90.0,
    max_retries=2,
)

# In-memory chat context. It clears when the Render service restarts/sleeps.
# Keep only the last 12 messages per chat to limit token usage.
chat_history: dict[int, list[dict[str, str]]] = {}
MAX_HISTORY_MESSAGES = 12
SYSTEM_PROMPT = (
    "You are a helpful, natural conversational AI assistant inside a Telegram bot. "
    "Reply in the language the user uses (often Tagalog/Filipino or English). "
    "Be friendly, clear, honest, and concise. Do not claim to have edited a video "
    "unless the bot actually processed and returned a file."
)

telegram_app: Application | None = None


async def ask_ai(chat_id: int, user_text: str) -> str:
    history = chat_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    # Limit history and always keep a valid sequence of recent turns.
    history[:] = history[-MAX_HISTORY_MESSAGES:]
    try:
        result = await ai.chat.completions.create(
            model=COARENA_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
            temperature=0.7,
            max_tokens=900,
        )
        answer = (result.choices[0].message.content or "").strip()
        if not answer:
            answer = "Walang text na ibinalik ng model. Subukan ulit."
        history.append({"role": "assistant", "content": answer})
        history[:] = history[-MAX_HISTORY_MESSAGES:]
        return answer
    except Exception as exc:
        log.exception("Coarena request failed")
        history.pop()  # Remove failed user request from memory
        return (
            "Hindi ako makakonekta sa Coarena ngayon. "
            "Pakitingnan ang COARENA_MODEL (dapat Free model), API key, at free daily limit. "
            f"Error: {type(exc).__name__}"
        )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Hi! Ako ang AI video/chat bot mo. 🤖\n\n"
            "Mag-message lang para makipag-usap sa AI.\n"
            "Mag-send ng video at caption na 'compress' para i-compress.\n"
            "Commands: /help, /reset, /ai <message>"
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Paano gamitin:\n"
            "• Normal text: AI chat\n"
            "• /ai tanong: diretsong AI prompt\n"
            "• /reset: burahin ang chat context sa memory\n"
            "• Mag-send ng video na may caption 'compress' para i-compress\n"
            "• Caption 'convert mp4' para gawing MP4\n\n"
            "Video editing sa version na ito: compress at convert MP4. "
            "Ang ibang advanced edit ay hindi pa naka-enable."
        )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid is not None:
        chat_history.pop(cid, None)
    if update.effective_message:
        await update.effective_message.reply_text("Na-reset ang conversational context. Simula ulit tayo!")


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.effective_message.reply_text("Gamit: /ai <tanong mo>")
        return
    msg = await update.effective_message.reply_text("Nag-iisip...")
    answer = await ask_ai(update.effective_chat.id, prompt)
    await msg.edit_text(answer)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_chat:
        return
    user_text = (update.effective_message.text or "").strip()
    if not user_text:
        return
    pending = await update.effective_message.reply_text("...")
    answer = await ask_ai(update.effective_chat.id, user_text)
    try:
        await pending.edit_text(answer)
    except Exception:
        await update.effective_message.reply_text(answer)


def safe_video_action(caption: str) -> str:
    """Basic supported actions selected from the user's caption."""
    c = (caption or "").lower()
    if any(word in c for word in ("compress", "compressed", "paliitin", "liitan", "reduce size")):
        return "compress"
    if any(word in c for word in ("convert mp4", "to mp4", "gawing mp4", "mp4 format")):
        return "mp4"
    return "unsupported"


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    media = message.video or (message.document if message.document and
                              (message.document.mime_type or "").startswith("video/") else None)
    if not media:
        return

    action = safe_video_action(message.caption or "")
    if action == "unsupported":
        await message.reply_text(
            "Nakuha ko ang video. Sa ngayon, caption na 'compress' o 'convert mp4' "
            "ang supported. Magdagdag ng caption at i-send ulit."
        )
        return

    status = await message.reply_text("Pinoproseso ang video…")
    with tempfile.TemporaryDirectory(prefix="tgvideo_") as tmp:
        inp = Path(tmp) / "input"
        out = Path(tmp) / ("compressed.mp4" if action == "compress" else "converted.mp4")
        try:
            tg_file = await context.bot.get_file(media.file_id)
            await tg_file.download_to_drive(custom_path=str(inp))
            if action == "compress":
                cmd = [
                    "ffmpeg", "-y", "-i", str(inp),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    str(out)
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-i", str(inp),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    str(out)
                ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not out.exists():
                log.error("ffmpeg failed: %s", stderr.decode(errors="ignore")[-2500:])
                await status.edit_text("Hindi naproseso ang video. Baka unsupported codec o masyadong malaki.")
                return
            with out.open("rb") as f:
                await message.reply_document(document=f, filename=out.name, caption="Tapos na! 🎬")
            await status.delete()
        except Exception:
            log.exception("Video processing failed")
            await status.edit_text("Nagkaproblema sa pag-download o pagproseso ng video.")


def build_telegram_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    global telegram_app
    telegram_app = build_telegram_app()
    await telegram_app.initialize()
    await telegram_app.start()

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/telegram"
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message"],
        )
        log.info("Telegram webhook configured: %s", webhook_url)
    else:
        log.warning("RENDER_EXTERNAL_URL not set; webhook was not configured.")
    yield
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="Coarena Telegram AI Bot", lifespan=lifespan)


@app.get("/")
async def health():
    return {"ok": True, "service": "coarena-telegram-bot"}


@app.post("/telegram")
async def telegram_webhook(request: Request):
    global telegram_app
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Bot is starting")
    if WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
