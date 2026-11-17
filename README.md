# YouTube Agent — Telegram comment approval bot

Cloud-first Flask app for Render. **No SQLite, no Supabase SDK, no local server, and no local database.**

## What it does
- Telegram webhook (not long-polling).
- Owner-only `/comments` command scans recent uploads and top-level comments.
- Gemini generates a suggested creator reply.
- Each suggestion is sent to Telegram with **Approve & reply** and **Skip** buttons.
- A YouTube reply is published only after the owner presses Approve.
- OAuth refresh token is supplied through Render environment variables.
- Health endpoint: `/healthz`.

## Important storage design
This build deliberately uses **in-memory state only**. Pending approval cards are lost when Render restarts or sleeps. Run `/comments` again to rebuild them. Do not treat this as durable storage. No SQLite or Supabase is used.

## Render
Create a Free Web Service from this repository:
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
- Health check: `/healthz`
- Set `RENDER_EXTERNAL_URL` to the exact deployed HTTPS URL.

## Environment variables
Set all values from `.env.example` in Render. Generate long random values for `TELEGRAM_WEBHOOK_SECRET` and `MANAGER_API_TOKEN`. Never commit secrets.

1. Create a Telegram bot with BotFather; set `TELEGRAM_BOT_TOKEN`.
2. Set your numeric Telegram user ID as `TELEGRAM_OWNER_ID`.
3. Set the group ID as `TELEGRAM_GROUP_ID` (optional; blank allows owner private chat).
4. In Google Cloud create OAuth Client ID type **Web application**. Add this exact Authorized redirect URI: `https://YOUR-RENDER-HOST/oauth/callback`.
5. Set `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`.
6. Open `https://YOUR-RENDER-HOST/oauth/start`, authorize the channel, copy the displayed refresh token into Render as `YOUTUBE_REFRESH_TOKEN`, then redeploy. Never share the token.
7. Set `GEMINI_API_KEY` for AI-generated drafts. Without it, the app uses a generic fallback draft.
8. After deployment, configure webhook by POSTing to `/admin/set-webhook` with header `X-Manager-Token: YOUR_MANAGER_API_TOKEN`.

## Telegram usage
- `/start` or `/help`
- `/status`
- `/comments` — fetch recent comments and post approval cards.
- Press **Approve & reply** to publish; press **Skip** to discard.

## Safety
- Auto-reply is approval-gated by design. There is no unattended public reply mode.
- Only the configured owner can run comment scanning or approve replies.
- Start with a private test video and verify the connected channel before using it on a public channel.
- This version scans up to 10 latest uploaded videos and up to 50 top-level comments per video per request.
