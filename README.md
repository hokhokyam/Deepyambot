# Coarena Telegram AI + FFmpeg bot

Upload the video first. The bot temporarily downloads it, then wait for a separate instruction:
- `compress` / `paliitin ang video`
- `convert to MP4`

The bot executes FFmpeg and sends the processed file back. Text chat goes to Coarena AI.
Only compression and MP4 conversion are implemented in this build. Do not assume arbitrary edit requests work yet.

## Render environment variables
- `TELEGRAM_BOT_TOKEN`: Telegram BotFather token
- `COARENA_API_KEY`: Coarena API key (secret; never put in GitHub)
- `COARENA_MODEL`: exact model ID currently labeled Free in Coarena
- `WEBHOOK_SECRET`: optional webhook secret

Render supplies `RENDER_EXTERNAL_URL` and `PORT`.

Coarena OpenAI-compatible API base URL: `https://coarena.ai/api/v1`

Chat memory and last uploaded video are in RAM / temporary disk. Render restarts or sleeps can clear them. Use `/reset` to clear chat and video.

Deploy all files to the root of your GitHub repository. Set Render service to Docker, Root Directory blank.
