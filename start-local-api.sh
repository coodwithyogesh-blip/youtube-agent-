#!/bin/sh
set -eu
: "${TELEGRAM_API_ID:?Set TELEGRAM_API_ID from my.telegram.org}"
: "${TELEGRAM_API_HASH:?Set TELEGRAM_API_HASH from my.telegram.org}"
: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN in Render}"
mkdir -p "${TELEGRAM_LOCAL_DATA:-/tmp/telegram-api}"
BIN="${TELEGRAM_API_BINARY:-./telegram-bot-api}"
if [ ! -x "$BIN" ]; then BIN="$(command -v telegram-bot-api || true)"; fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
  echo "telegram-bot-api binary missing. Check Render Build Command." >&2; exit 1
fi
"$BIN" --local --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" --http-port=8081 --http-ip-address=127.0.0.1 --dir="${TELEGRAM_LOCAL_DATA:-/tmp/telegram-api}" &
API_PID=$!
export TELEGRAM_API_BASE="http://127.0.0.1:8081/bot"
export TELEGRAM_FILE_BASE="http://127.0.0.1:8081/file/bot"
export UPLOAD_LOCK_FILE="${UPLOAD_LOCK_FILE:-/tmp/youtube-agent-upload.lock}"
APP_PID=""
cleanup() {
  if [ -n "${APP_PID:-}" ]; then kill "$APP_PID" 2>/dev/null || true; fi
  kill "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python - <<'PY'
import socket,time,sys
for _ in range(90):
    try:
        with socket.create_connection(("127.0.0.1",8081),timeout=1): sys.exit(0)
    except OSError: time.sleep(1)
sys.exit("Telegram Local API server did not start")
PY
start_app() {
  gunicorn app:app --workers 1 --threads 8 --timeout 240 &
  APP_PID=$!
}
start_app

# Webhook auto-setup/checker runs independently; no periodic webhook-reset calls.
if [ -n "${RENDER_EXTERNAL_URL:-}" ] && [ -n "${MANAGER_API_TOKEN:-}" ]; then
  python - <<'PY' &
import os,time,urllib.request,json
base=os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
token=os.environ.get("MANAGER_API_TOKEN","")
headers={"X-Manager-Token":token}
def request(path,method="GET"):
 req=urllib.request.Request(base+path,method=method,headers=headers)
 with urllib.request.urlopen(req,timeout=15) as response:return json.loads(response.read().decode())
ready=False
for _ in range(90):
 try:
  with urllib.request.urlopen(base+"/healthz",timeout=8) as response:
   if response.status==200:ready=True;break
 except Exception:pass
 time.sleep(2)
if ready:
 while True:
  try:
   status=request("/admin/webhook-status")
   if not status.get("webhook",{}).get("url_configured"):
    print("Webhook missing; repairing...",flush=True)
    print("Webhook auto-repair result:",request("/admin/set-webhook","POST"),flush=True)
   else:print("Webhook check: configured",flush=True)
  except Exception as exc:print("Webhook auto-check/recovery error:",type(exc).__name__,flush=True)
  time.sleep(3600)
else:print("Webhook auto-setup: readiness timed out",flush=True)
PY
else
  echo "Webhook auto-setup skipped: RENDER_EXTERNAL_URL or MANAGER_API_TOKEN missing."
fi

# No periodic Gunicorn restarts: keep long-running upload threads alive.
# Render may still restart instances for platform events/deploys.
echo "Gunicorn periodic restart disabled."
wait "$APP_PID"
