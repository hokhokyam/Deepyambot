import os, json, tempfile, subprocess, asyncio, logging, shlex
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("video-agent")

BOT_TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
DEEPSEEK_API_KEY=os.environ["DEEPSEEK_API_KEY"]
PUBLIC_URL=os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
WEBHOOK_SECRET=os.environ.get("TELEGRAM_WEBHOOK_SECRET","change-this-secret")
client=OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
tg=Application.builder().token(BOT_TOKEN).build()
app=FastAPI()

SYSTEM_PROMPT="""You are a video-editing planner. Return ONLY JSON with:
{"action":"compress|convert|trim|caption|resize","start":"HH:MM:SS","end":"HH:MM:SS","caption":"text","width":720}
Choose action from the user's request. If unclear, compress. For trim, use start/end time mentioned;
otherwise start 00:00:00 and end 00:00:15. For caption, put short requested text in caption.
Never include shell commands, paths, filters, or executable code. Keep width between 320 and 1280.
"""

def plan_for(prompt):
    r=client.chat.completions.create(model="deepseek-flash", messages=[
        {"role":"system","content":SYSTEM_PROMPT},
        {"role":"user","content":prompt[:1500]}], response_format={"type":"json_object"})
    data=json.loads(r.choices[0].message.content)
    allowed={"compress","convert","trim","caption","resize"}
    if data.get("action") not in allowed: data["action"]="compress"
    data["width"]=max(320,min(1280,int(data.get("width",720))))
    return data

def timestamp(value, fallback):
    # Accept only HH:MM:SS or MM:SS numeric timestamps
    if not isinstance(value,str) or not all(x.isdigit() for x in value.split(":")):
        return fallback
    parts=value.split(":")
    if len(parts) not in (2,3) or any(int(x)>59 for x in parts[1:]):
        return fallback
    return value

def render_video(source, output, plan):
    action=plan["action"]
    cmd=["ffmpeg","-y","-i",str(source),"-map","0:v:0","-map","0:a?"]
    if action=="trim":
        start=timestamp(plan.get("start"),"00:00:00")
        end=timestamp(plan.get("end"),"00:00:15")
        cmd += ["-ss",start,"-to",end]
    if action=="resize":
        w=plan["width"]
        cmd += ["-vf",f"scale={w}:-2"]
    # Caption overlay uses a safely escaped filter string, not shell interpolation.
    if action=="caption":
        text=str(plan.get("caption","Edited with AI"))[:120]
        safe=text.replace("\\","\\\\").replace(":","\\:").replace("'","\\'").replace("%","\\%")
        cmd += ["-vf",f"drawtext=text='{safe}':fontcolor=white:fontsize=30:borderw=2:x=(w-text_w)/2:y=h-80"]
    cmd += ["-c:v","libx264","-preset","ultrafast","-crf","28","-c:a","aac","-movflags","+faststart",str(output)]
    if action=="convert": cmd[-1]=str(output.with_suffix(".mp4"))
    subprocess.run(cmd,check=True,timeout=600,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    return output.with_suffix(".mp4") if action=="convert" else output

async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a video with an instruction in its caption, e.g.\\n"
        "• compress this video\\n• trim from 00:00:03 to 00:00:12\\n"
        "• resize to 720px\\n• add caption: My travel vlog\\n"
        "I use DeepSeek to plan and FFmpeg to render. Please send short test clips first."
    )

async def help_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await start_cmd(update,context)

async def video_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    media=msg.video or (msg.document if msg.document and (msg.document.mime_type or "").startswith("video/") else None)
    if not media:
        await msg.reply_text("Please attach a video file (MP4 recommended) and put your editing instruction in its caption.")
        return
    status=await msg.reply_text("📥 Receiving video…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/"input.mp4"; out=Path(tmp)/"edited.mp4"
            tgfile=await context.bot.get_file(media.file_id)
            await tgfile.download_to_drive(src)
            prompt=msg.caption or "Compress this video for Telegram"
            await status.edit_text("🧠 DeepSeek is preparing the edit…")
            plan=await asyncio.to_thread(plan_for,prompt)
            await status.edit_text("🎬 FFmpeg is rendering…")
            result=await asyncio.to_thread(render_video,src,out,plan)
            await status.edit_text("📤 Sending edited video…")
            with result.open("rb") as f:
                await msg.reply_video(video=f,caption=f"Edited with DeepSeek + FFmpeg · {plan['action']}")
        await status.delete()
    except subprocess.TimeoutExpired:
        await status.edit_text("Render timed out. Try a shorter or smaller video.")
    except Exception:
        log.exception("Video processing failed")
        await status.edit_text("I couldn't process that clip. Try MP4 and a shorter video; check Render logs if it repeats.")

tg.add_handler(CommandHandler("start",start_cmd))
tg.add_handler(CommandHandler("help",help_cmd))
tg.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO,video_handler))

@app.on_event("startup")
async def startup():
    await tg.initialize()
    await tg.start()
    if PUBLIC_URL:
        await tg.bot.set_webhook(f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}")
    log.info("Telegram webhook configured")

@app.on_event("shutdown")
async def shutdown():
    await tg.stop()
    await tg.shutdown()

@app.get("/")
async def health():
    return {"status":"ok","service":"DeepSeek Telegram Video Agent"}

@app.post("/telegram/{secret}")
async def webhook(secret:str, request:Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    payload=await request.json()
    update=Update.de_json(payload,tg.bot)
    await tg.update_queue.put(update)
    return {"ok":True}
