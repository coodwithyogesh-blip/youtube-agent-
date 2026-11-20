#!/bin/sh
set -eu
: "${TELEGRAM_API_ID:?Set TELEGRAM_API_ID from my.telegram.org}"
: "${TELEGRAM_API_HASH:?Set TELEGRAM_API_HASH from my.telegram.org}"
: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN in Render}"
mkdir -p "${TELEGRAM_LOCAL_DATA:-/tmp/telegram-api}"
BIN="${TELEGRAM_API_BINARY:-./telegram-bot-api}"
if [ ! -x "$BIN" ]; then
  BIN="$(command -v telegram-bot-api || true)"
fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
  echo "telegram-bot-api binary missing. Check Render Build Command." >&2
  exit 1
fi
"$BIN" --local --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" --http-port=8081 --http-ip-address=127.0.0.1 --dir="${TELEGRAM_LOCAL_DATA:-/tmp/telegram-api}" &
API_PID=$!
export TELEGRAM_API_BASE="http://127.0.0.1:8081/bot"
export TELEGRAM_FILE_BASE="http://127.0.0.1:8081/file/bot"
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
gunicorn app:app --workers 1 --threads 8 --timeout 240 &
APP_PID=$!
wait "$APP_PID"
