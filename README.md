# YouTube Agent — Telegram video publisher and automatic comment manager

Cloud-first Flask app for Render. No SQLite, no Supabase, and no Termux/local server.

## Telegram video workflow
1. Send the video to the connected Telegram bot.
2. Send your own thumbnail as a Telegram photo.
3. When both arrive, Gemini analyses the actual video and generates the title, description, SEO tags and hashtags.
4. The app uploads the video as **Public** and sets the thumbnail you supplied. AI does not create or replace the thumbnail.

No manual title, description, tags or hashtags are required. An optional Telegram caption is passed to Gemini as extra context. The Telegram Bot API download limit is 20 MB per file; send a smaller/compressed video if the bot cannot download it. Thumbnail must be 2 MB or smaller.

## Automatic YouTube comment replies
- `AUTO_REPLY_MODE=automatic` enables unattended AI replies.
- The service checks recent comments every 15 minutes by default (`COMMENT_POLL_SECONDS`).
- It skips comments already answered by the connected channel and filters obvious spam, links and some sensitive requests.
- Gemini writes a short reply and YouTube publishes it without an approval button.
- `/comments` manually triggers a scan; `/status` reports configuration.

This is polling, not instant push notification. On Render Free, the service can sleep when idle; while asleep, comment scans do not run. Use an always-on service for reliable continuous monitoring. Processed-comment state is memory-only and resets on restart, but the code also checks existing channel replies before posting.

## Web dashboard
Open the Render service URL, enter `MANAGER_API_TOKEN`, select a video and your thumbnail, and submit. The dashboard also generates metadata from the video using Gemini. API: `POST /api/videos/upload-ai`.

The default request limit is 90 MB (`MAX_UPLOAD_MB`). Render's own request/body limits may be lower or vary by service. Large uploads need a direct-to-cloud upload design rather than passing the whole file through the web request.

## Required environment
Set these in Render Environment:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_OWNER_ID`
- Optional `TELEGRAM_GROUP_ID` to restrict the bot to one group
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`
- `GEMINI_API_KEY` (required for AI video metadata and AI comment replies)
- `GEMINI_MODEL=gemini-3.8-flash` and `GEMINI_FALLBACK_MODEL=gemini-3.5-flash` (optional; bounded retries with exponential backoff are built in for transient 429/5xx/network failures)
- `MANAGER_API_TOKEN`, `RENDER_EXTERNAL_URL`
- `AUTO_REPLY_MODE=automatic`, `COMMENT_POLL_SECONDS=900`

## OAuth
This app requests `youtube.force-ssl` and `youtube.upload`. After deploying, authorize again at `/oauth/start` and replace `YOUTUBE_REFRESH_TOKEN` in Render with the new token. Add exact redirect URI `https://YOUR-RENDER-HOST/oauth/callback` in Google Cloud OAuth settings. Never share the refresh token.

## Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 1 --threads 8 --timeout 240`
- Health check: `/healthz`

## Google Sheets storage

The app includes `sheets_store.py` for a shared Google Spreadsheet with per-Telegram-user rows. It creates these tabs on first successful registration: Users, Accounts, Settings, Upload_History, Comments, and Sessions.

Configure these Render environment variables:
- `GOOGLE_SHEET_ID`: the long spreadsheet ID from the Google Sheets URL.
- `GOOGLE_SERVICE_ACCOUNT_JSON`: the complete service-account JSON key as one environment-variable value (never commit it to GitHub).

Setup:
1. Create a Google Cloud project and enable the Google Sheets API.
2. Create a service account and generate a JSON key.
3. Create a blank Google Sheet.
4. Share that Sheet with the service-account email address as Editor.
5. Add the two environment variables in Render and redeploy.
6. Open the Telegram bot privately and send `/start`. The user should then appear in the Users tab.

Important: Telegram users connect YouTube through the signed OAuth flow. Multiple channels can be stored for the same Telegram user; each channel refresh token is encrypted with TOKEN_ENCRYPTION_KEY. In Telegram, use Upload Video, select one of your own connected channels, then send the video and thumbnail. The upload service resolves the token by both Telegram user ID and selected channel ID, so one user cannot select another user's channel. The older web dashboard and automatic comment worker still use the legacy owner YOUTUBE_REFRESH_TOKEN; per-user comment moderation is a separate pending workflow. Instagram, Facebook and Snapchat are not yet connected. Passwords and raw browser cookies must never be stored in Sheets.

## Storage and limitations
No SQLite or Supabase. Media files are temporary and removed after upload. Telegram media, duplicate tracking and OAuth state are held in process memory and reset on restart. Test with a private/test channel first; this configuration publishes Telegram submissions publicly. Never share tokens.
