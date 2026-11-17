# YouTube Agent — cloud YouTube uploader and Telegram comment approvals

Cloud-first Flask app for Render. No SQLite, no Supabase, and no local Termux server.

## Current features
- Telegram webhook with owner-only `/comments` command.
- Gemini reply drafts; replies publish only after owner presses **Approve & reply**.
- Avoids comments already answered by the connected YouTube channel.
- Web dashboard at `/` with authenticated video upload.
- Optional custom thumbnail upload.
- Title, description, tags and visibility controls.
- Native YouTube scheduled publishing. Choose an India-local date/time; YouTube keeps the scheduled video private until its publish time.
- Health endpoint: `/healthz`.

## Video upload
Open the Render service URL, enter `MANAGER_API_TOKEN`, select a video, optionally select a thumbnail, and submit. The upload API is `POST /api/videos/upload` and requires the `X-Manager-Token` header.

The default request limit is 90 MB (`MAX_UPLOAD_MB`). Render's own request/body limits may be lower or vary by service, so this dashboard is intended for small/medium uploads. Large video files need a direct-to-cloud upload design rather than passing the whole file through the web request. A thumbnail must be 2 MB or smaller for YouTube.

Scheduling uses YouTube's native `status.publishAt`; no local scheduler or background worker is required. The scheduled publish time must be in the future. Times entered without an offset are interpreted as Asia/Kolkata.

## Important OAuth action
This version now requests both YouTube scopes:
- `youtube.force-ssl` (comment management)
- `youtube.upload` (video upload and thumbnail)

After deploying this version, reconnect Google OAuth at `/oauth/start` and replace `YOUTUBE_REFRESH_TOKEN` in Render with the newly issued token. An older token may not have upload permission. Add the exact redirect URI `https://YOUR-RENDER-HOST/oauth/callback` to your Google OAuth client.

## Render deployment
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
- Health check path: `/healthz`
- Set `RENDER_EXTERNAL_URL` to the exact deployed HTTPS URL.
- Set secrets only in Render Environment, never in GitHub.

## Environment
Copy values from `.env.example`. Required settings include Telegram bot token/webhook secret/owner ID, Google OAuth client ID and secret, YouTube refresh token, and `MANAGER_API_TOKEN`. `GEMINI_API_KEY` is optional for comment drafting.

## Telegram commands
- `/start` or `/help`
- `/status`
- `/comments` — create approval cards for recent comments.

## Storage and Render Free limitation
No SQLite or Supabase is used. Telegram pending approvals and OAuth state are held in memory and are lost when the service restarts or sleeps. Video uploads go directly to YouTube; uploaded video and thumbnail temp files are removed from the service after the request. This build does not retain an upload queue or local media library.

## Safety
- Replies are approval-gated; no unattended auto-reply.
- Dashboard upload is protected by `MANAGER_API_TOKEN`.
- Scheduled publishing is handled by YouTube after the upload succeeds.
- Test with a private video before making content public.
- Never share the YouTube refresh token or manager token.
