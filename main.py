import os, re, json, logging, asyncio, tempfile, shutil, subprocess
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("deepyam")
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
COARENA_KEY=os.getenv("COARENA_API_KEY","")
MODEL=os.getenv("COARENA_MODEL","")
WEBHOOK_URL=os.getenv("WEBHOOK_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))
WORK=Path(os.getenv("WORK_DIR","/tmp/deepyam")); WORK.mkdir(parents=True,exist_ok=True)
app=FastAPI()
tg: Optional[Application]=None
client=AsyncOpenAI(api_key=COARENA_KEY,base_url="https://coarena.ai/api/v1") if COARENA_KEY else None
sessions={} # process-local; resets on redeploy/restart

PLATFORMS=("youtube","tiktok","facebook")
PLATFORM_GUIDE={
"youtube":"YouTube: provide searchable but compelling title, Shorts/full-video distinction if known, description, 5 relevant hashtags, ranked keywords, opening hook, CTA, thumbnail text, and practical SEO advice. Do not claim keyword search volume.",
"tiktok":"TikTok: punchy native caption, immediate hook, 5 relevant hashtags, ranked keywords, CTA, suggested on-screen text, retention/editing tip. Avoid hashtag stuffing.",
"facebook":"Facebook: conversational shareable caption, clear context, 5 relevant hashtags, ranked keywords, CTA/comment prompt, suggested opening line and audience angle."
}
EDIT_PROMPT="""You are Deepyam, a practical Telegram video editor assistant. Return ONLY valid JSON:
{"reply":"brief user-facing explanation","edit":{"operations":[...],"summary":"..."}}
Supported operations: {"type":"trim","start":"00:00:02","end":"00:00:12"}, {"type":"aspect","ratio":"9:16"|"16:9"|"1:1"}, {"type":"resize","width":1080,"height":1920}, {"type":"preset","name":"cinematic|vintage|warm|cool|dramatic|dreamy|bw"}, {"type":"effect","name":"blur|sharpen|vignette|mirror|negative|fade-in|fade-out|zoom-in|slow-motion"}, {"type":"text","text":"...","position":"top|center|bottom","color":"white|yellow|red|cyan","font":"sans|bold|serif|mono"}, {"type":"mute"}, {"type":"compress"}, {"type":"sfx","name":"beep|whoosh|impact","at":"00:00:03"}.
Do not invent unsupported operations. If the user asks only for advice/social copy, return operations=[] and answer in reply. Ask for clarification if essential. Preserve source unless operation requested."""
SOCIAL_PROMPT="""You are Deepyam Social Media Content Intelligence. Generate honest platform-native content from the user's topic, description, and edit summary. Return ONLY JSON:
{"platform":"...","title":"...","hook":"...","caption":"...","description":"...","hashtags":[{"tag":"#...","rank":1,"reason":"short relevance rationale"} x exactly 5],"keywords":[{"keyword":"...","rank":1}],"cta":"...","thumbnail_text":"...","audience":"...","recommendations":["..."],"trend_status":"AI relevance estimate only; no live trend/search-volume data connected"}.
Rank hashtags and keywords by estimated relevance, NOT live popularity. Do not claim guaranteed reach, viral results, or real-time trending. Adapt to platform instructions."""
def call_ffmpeg(args):
    p=subprocess.run(["ffmpeg","-hide_banner","-y",*args],capture_output=True,text=True,timeout=900)
    if p.returncode: raise RuntimeError((p.stderr or "FFmpeg failed")[-1800:])
def ffmpeg_edit(src, ops):
    out=WORK/f"out_{os.urandom(5).hex()}.mp4"
    inp=str(src); vf=[]; af=[]; extra=[]; has_audio=True
    # detect audio stream
    probe=subprocess.run(["ffprobe","-v","error","-select_streams","a","-show_entries","stream=index","-of","csv=p=0",inp],capture_output=True,text=True)
    has_audio=bool(probe.stdout.strip())
    for op in ops:
        typ=op.get("type")
        if typ=="trim":
            extra += ["-ss",str(op.get("start","0")),"-to",str(op.get("end",""))]
        elif typ=="aspect":
            ratio=op.get("ratio")
            dims={"9:16":"1080:1920","16:9":"1920:1080","1:1":"1080:1080"}
            if ratio not in dims: raise ValueError("Aspect must be 9:16, 16:9, or 1:1")
            w,h=dims[ratio].split(":"); vf.append(f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}")
        elif typ=="resize":
            w=max(144,min(3840,int(op.get("width",1280)))); h=max(144,min(3840,int(op.get("height",720))))
            vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        elif typ=="preset":
            name=op.get("name")
            filters={"cinematic":"eq=contrast=1.12:saturation=0.85","vintage":"colorchannelmixer=.9:0:0:0:0:.8:0:0:0:0:.65:0","warm":"colorbalance=rs=.08:gs=.02:bs=-.06","cool":"colorbalance=rs=-.04:gs=.01:bs=.08","dramatic":"eq=contrast=1.3:saturation=1.15","dreamy":"gblur=sigma=1.2","bw":"hue=s=0"}
            if name not in filters: raise ValueError("Unknown preset")
            vf.append(filters[name])
        elif typ=="effect":
            name=op.get("name")
            filters={"blur":"gblur=sigma=3","sharpen":"unsharp=5:5:1.0","vignette":"vignette","mirror":"hflip","negative":"negate","fade-in":"fade=t=in:st=0:d=1","fade-out":"fade=t=out:st=4:d=1","zoom-in":"scale=iw*1.08:ih*1.08,crop=iw/1.08:ih/1.08","slow-motion":"setpts=1.5*PTS"}
            if name not in filters: raise ValueError("Unknown effect")
            vf.append(filters[name])
            if name=="slow-motion" and has_audio: af.append("atempo=0.667")
        elif typ=="text":
            txt=str(op.get("text",""))[:180].replace("\\","\\\\").replace(":","\\:").replace("'","\\'").replace("%","\\%")
            pos={"top":"(w-text_w)/2:40","center":"(w-text_w)/2:(h-text_h)/2","bottom":"(w-text_w)/2:h-text_h-50"}.get(op.get("position","bottom"))
            color={"white":"white","yellow":"yellow","red":"red","cyan":"cyan"}.get(op.get("color","white"),"white")
            fonts={"sans":"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf","bold":"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","serif":"/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf","mono":"/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"}
            font=fonts.get(op.get("font","bold"),fonts["bold"])
            vf.append(f"drawtext=fontfile={font}:text='{txt}':fontcolor={color}:fontsize=48:borderw=3:bordercolor=black:x={pos.split(':')[0]}:y={pos.split(':')[1]}")
        elif typ=="mute": extra += ["-an"]; has_audio=False
        elif typ=="compress": extra += ["-crf","27","-preset","veryfast"]
        elif typ=="sfx":
            # Generated tone/noise effect is mixed with original audio; basic tone only.
            name=op.get("name"); freq={"beep":880,"whoosh":240,"impact":90}.get(name)
            if not freq: raise ValueError("Unknown SFX")
            at=float(op.get("at",0) or 0)
            # safely add a simple synthesized tone via lavfi input
            extra += ["-f","lavfi","-i",f"sine=frequency={freq}:duration=0.35"]
            # mixing is composed after regular filters below
            extra += ["-filter_complex",f"[1:a]adelay={int(at*1000)}|{int(at*1000)}[sfx];[0:a][sfx]amix=inputs=2:duration=first[aout]","-map","0:v","-map","[aout]"]
        else: raise ValueError(f"Unsupported operation: {typ}")
    args=["-i",inp]
    if vf: args += ["-vf",",".join(vf)]
    if not any(x=="-filter_complex" for x in extra):
        if not has_audio: args += ["-an"]
        else: args += ["-c:a","aac"]
    args += extra+["-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart",str(out)]
    call_ffmpeg(args)
    return out
def extract_json(text):
    text=text.strip()
    text=re.sub(r"^```(?:json)?|```$","",text,flags=re.I|re.M).strip()
    try:return json.loads(text)
    except: 
        m=re.search(r"\{.*\}",text,re.S)
        if m:return json.loads(m.group())
        raise ValueError("AI did not return valid JSON.")
async def ai_json(system,user):
    if not client: raise RuntimeError("Set COARENA_API_KEY in Render environment.")
    kwargs={"model":MODEL or "","messages":[{"role":"system","content":system},{"role":"user","content":user}],"temperature":0.65}
    if not MODEL: kwargs.pop("model")
    res=await client.chat.completions.create(**kwargs)
    return extract_json(res.choices[0].message.content or "")
def state(chat):
    return sessions.setdefault(chat,{"history":[],"video":None,"edit_summary":"No edit yet."})
async def start(update,context):
    await update.message.reply_text("Hi! I'm Deepyam AI Video + Social Studio.\nSend a video and describe edits naturally, or use /social youtube|tiktok|facebook [topic].\nCommands: /help /social /platform")
async def help_cmd(update,context):
    await update.message.reply_text("""Send a video, then say what to edit (e.g. “make it vertical, cinematic, add title text”).
Social: /social tiktok topic; /social youtube topic; /social facebook topic
If video is already uploaded, say “create TikTok caption and top 5 hashtags”.
Platform: /platform youtube|tiktok|facebook
Note: hashtags/keywords are AI relevance-ranked, not live trend rankings.""")
async def platform_cmd(update,context):
    if not context.args or context.args[0].lower() not in PLATFORMS:
        await update.message.reply_text("Choose: youtube, tiktok, or facebook");return
    state(update.effective_chat.id)["platform"]=context.args[0].lower()
    await update.message.reply_text(f"Platform set to {context.args[0].title()}.")
async def social(update,context,platform=None,topic=None):
    s=state(update.effective_chat.id); platform=platform or s.get("platform")
    if platform not in PLATFORMS:
        await update.message.reply_text("Choose platform: /social youtube [topic], /social tiktok [topic], or /social facebook [topic]");return
    topic=(topic or "Create a social package for my latest video").strip()
    brief=f"Platform: {platform}\nUser topic/instruction: {topic}\nVideo/edit context: {s.get('edit_summary','No uploaded video context')}"
    try:
        data=await ai_json(SOCIAL_PROMPT+"\n"+PLATFORM_GUIDE[platform],brief)
        s["platform"]=platform
        hashtags=data.get("hashtags",[])[:5]; keywords=data.get("keywords",[])[:10]
        lines=[f"📱 {platform.upper()} CONTENT PACKAGE",f"Title: {data.get('title','')}",f"Hook: {data.get('hook','')}",f"\nCaption:\n{data.get('caption','')}",f"\nDescription:\n{data.get('description','')}", "\nTop 5 hashtags (AI relevance rank):"]
        lines += [f"{i+1}. {x.get('tag','')} — {x.get('reason','')}" for i,x in enumerate(hashtags)]
        lines += ["\nRanked keywords:"]+[f"{i+1}. {x.get('keyword','')}" for i,x in enumerate(keywords)]
        lines += [f"\nCTA: {data.get('cta','')}",f"Thumbnail/on-screen text: {data.get('thumbnail_text','')}",f"Audience: {data.get('audience','')}", "\nSuggestions:"]+["• "+str(x) for x in data.get("recommendations",[])[:6]]
        lines += ["\nTrend note: AI relevance estimate only; live trend/search-volume data is not connected."]
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as e:
        log.exception("social generation failed")
        await update.message.reply_text("Social package failed: "+str(e)[:500])
async def social_cmd(update,context):
    args=context.args
    if not args: await social(update,context);return
    p=args[0].lower()
    if p not in PLATFORMS: await update.message.reply_text("Use /social youtube|tiktok|facebook [topic]");return
    await social(update,context,p," ".join(args[1:]))
async def on_text(update,context):
    chat=update.effective_chat.id;s=state(chat);msg=update.message.text
    m=re.search(r"\b(youtube|tiktok|facebook)\b",msg.lower())
    if any(k in msg.lower() for k in ["hashtag","caption","keyword","social media","seo","content package","content for"]):
        await social(update,context,m.group(1) if m else None,msg);return
    if not s.get("video"):
        await update.message.reply_text("Send/upload a video first for editing, or use /social platform [topic] to create social content.");return
    await update.message.reply_text("Analyzing your edit request…")
    try:
        data=await ai_json(EDIT_PROMPT,msg)
        edit=data.get("edit",{});ops=edit.get("operations",[])
        if not isinstance(ops,list):ops=[]
        s["edit_summary"]=edit.get("summary",msg)
        if not ops:
            await update.message.reply_text(data.get("reply","Tell me what edit you'd like, or ask for a social content package."));return
        path=await asyncio.to_thread(ffmpeg_edit,s["video"],ops)
        await update.message.reply_video(video=path,caption=(data.get("reply","Edited video")+"\n\nEdit summary: "+s["edit_summary"])[:1000])
        s["video"]=path
    except Exception as e:
        log.exception("edit failed")
        await update.message.reply_text("I couldn't complete that edit: "+str(e)[:700])
async def on_video(update,context):
    m=update.message.video or update.message.document
    if update.message.document and not (m.mime_type or "").startswith("video/"):
        await update.message.reply_text("Please upload a video file (MP4 preferred).");return
    if m.file_size and m.file_size>45*1024*1024:
        await update.message.reply_text("This deployment's download limit is about 45 MB. Please send a smaller/compressed video.");return
    await update.message.reply_text("Downloading your video…")
    f=await context.bot.get_file(m.file_id)
    path=WORK/f"input_{os.urandom(5).hex()}.mp4"
    await f.download_to_drive(path)
    state(update.effective_chat.id)["video"]=str(path)
    state(update.effective_chat.id)["edit_summary"]="Uploaded video; no edits applied yet."
    await update.message.reply_text("Video received. Tell me what to do, e.g. “make 9:16, add cinematic look, put title at bottom” or “make a TikTok content package”.")
async def init_bot():
    global tg
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    tg=Application.builder().token(TOKEN).build()
    tg.add_handler(CommandHandler("start",start));tg.add_handler(CommandHandler("help",help_cmd))
    tg.add_handler(CommandHandler("platform",platform_cmd));tg.add_handler(CommandHandler("social",social_cmd))
    tg.add_handler(MessageHandler(filters.VIDEO|filters.Document.VIDEO,on_video))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,on_text))
    await tg.initialize();await tg.start()
    if WEBHOOK_URL:
        await tg.bot.set_webhook(WEBHOOK_URL+"/telegram")
@app.on_event("startup")
async def startup(): await init_bot()
@app.on_event("shutdown")
async def shutdown():
    if tg: await tg.stop();await tg.shutdown()
@app.post("/telegram")
async def webhook(request:Request):
    if not tg: raise HTTPException(503,"Bot not initialized")
    update=Update.de_json(await request.json(),tg.bot)
    await tg.process_update(update)
    return {"ok":True}
@app.get("/")
async def health(): return {"status":"Deepyam v5 social-video bot running"}
