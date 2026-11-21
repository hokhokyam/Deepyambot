# DeepSeek + Telegram Video Editing Agent (Render)

A starter bot: send a video to Telegram with an instruction in its caption. DeepSeek converts the instruction to a restricted JSON edit plan; FFmpeg executes supported edits.

## Supported first version
- compress (default)
- convert to MP4
- trim using timestamps
- resize width
- overlay a short text caption

## Deploy
1. Create a Telegram bot with **@BotFather** and copy its token.
2. Create a DeepSeek API key in the DeepSeek platform. API usage may cost money; Render hosting being free does not make the AI API free.
3. Push this repository to your own GitHub repository.
4. In Render, choose **New → Web Service**, connect the GitHub repo, choose the Docker runtime and Free plan.
5. Add environment variables:
   - `TELEGRAM_BOT_TOKEN` = BotFather token
   - `DEEPSEEK_API_KEY` = DeepSeek API key
   - `TELEGRAM_WEBHOOK_SECRET` = long random secret
6. Deploy. Copy the Render service URL if needed; Render sets `RENDER_EXTERNAL_URL` automatically.
7. Open the bot in Telegram and send `/start`.

## Example caption on a video
`Trim from 00:00:02 to 00:00:10 and compress`

## Important free-tier notes
- This uses a webhook, not long-running polling, because Render Free web services sleep after inactivity and can restart.
- Free Render filesystem is ephemeral. This implementation processes videos in a temporary directory and sends the output back; it does not keep a video library.
- Free services have limited CPU/RAM and can be unsuitable for long/high-resolution clips. Start with short 480p/720p videos.
- Telegram and hosting impose upload/processing limits; very large clips may fail.
- The bot token and DeepSeek key must be Render environment variables, never committed to GitHub.
- FFmpeg is included in the Docker image.

## Editing behavior
DeepSeek is not generating video frames. It interprets the user's request; FFmpeg performs the actual media operation. Review and extend `render_video()` before enabling additional edit types.
