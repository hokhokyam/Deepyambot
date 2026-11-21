# Coarena Telegram AI + Video Bot

## Files
- `main.py`: Telegram webhook, natural AI chat with short in-memory context, basic video compress/MP4 conversion
- `Dockerfile`: installs FFmpeg and Python dependencies
- `render.yaml`: Render Blueprint settings

## Required Render environment variables
1. `TELEGRAM_BOT_TOKEN` — token from BotFather
2. `COARENA_API_KEY` — your Coarena API key (never commit it)
3. `COARENA_MODEL` — exact model ID that Coarena marks **Free**
4. `WEBHOOK_SECRET` — Render generates this through Blueprint (optional if set manually)

Render automatically provides `RENDER_EXTERNAL_URL` and `PORT`.

## Coarena API
OpenAI-compatible base URL: `https://coarena.ai/api/v1`
The code uses the model selected in `COARENA_MODEL`. Do not guess the model name; choose a currently Free model in your Coarena model list. Free tiers have daily/rate limits and availability can change.

## Deploy
1. Upload these files to the root of your GitHub repository.
2. In Render, create or update a Docker web service connected to the repository. Root directory should be blank.
3. Add the environment variables above and deploy.
4. Visit the Render service URL; `/` should return `{"ok": true, ...}`.

## Bot commands
- `/start`, `/help`
- `/ai your prompt`
- `/reset` clears the chat context
- Plain text messages are sent to the AI
- Send a video with caption `compress` or `convert mp4`

## Important limitations
- Chat context is stored in RAM and resets after service restart/sleep; this is not persistent database memory.
- Render Free may sleep when inactive and wake slowly.
- Video files are processed temporarily and not retained.
- Only compression and MP4 conversion are implemented in this starter. It does not yet implement arbitrary AI-directed edits, trim, subtitles, or effects.
