# Deepyam v5 — AI Video + Social Studio

## Features
- Telegram bot receives MP4/video.
- AI planner (Coarena OpenAI-compatible API) interprets natural language editing requests.
- FFmpeg operations: trim, aspect ratios, resize, presets, effects, text overlay, mute, compression, basic synthesized SFX.
- Social content for YouTube, TikTok, Facebook: title, hook, caption, description, exactly up to 5 hashtags, ranked keywords, CTA, thumbnail text, audience and suggestions.
- Rankings are AI-estimated relevance, not live trending/search-volume data.

## Deploy to Render
1. Push this folder to a new GitHub repository.
2. Render → New Web Service → connect repository → Docker runtime.
3. Add Environment Variables: `TELEGRAM_BOT_TOKEN`, `COARENA_API_KEY`, `COARENA_MODEL`, `WEBHOOK_URL` (the public Render URL, no trailing slash).
4. Deploy. Open Render service URL; `/` should return status.
5. In Telegram, `/start`.

## Commands
- `/social youtube [topic]`
- `/social tiktok [topic]`
- `/social facebook [topic]`
- `/platform tiktok` sets default platform.
- Upload video and send edit instructions as natural language.

## Important limitations
- This is a starter implementation; test on a separate bot before production.
- Render Free may sleep/restart; local session state and uploaded files are temporary and reset on restart.
- Telegram file download limits and hosting RAM/CPU apply.
- AI planner does not visually understand the footage; provide a clear description/topic.
- Does not publish directly to social accounts or retrieve live trend metrics.
- Some FFmpeg operations/combination orders may need testing; save originals.
- Multi-clip transitions, speech-to-text subtitles, automatic face tracking and platform analytics are not implemented.
- Never commit API keys. Use environment variables only.
