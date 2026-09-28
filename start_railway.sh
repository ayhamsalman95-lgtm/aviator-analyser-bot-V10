#!/bin/bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
mkdir -p /app/data/chrome_profile /tmp/aviator

echo "[RUNTIME] starting Xvfb" >&2
Xvfb "${DISPLAY}" -screen 0 1440x900x24 -ac +extension RANDR > /tmp/aviator/xvfb.log 2>&1 &
XVFB_PID=$!

echo "[RUNTIME] starting fluxbox" >&2
fluxbox > /tmp/aviator/fluxbox.log 2>&1 &
FLUXBOX_PID=$!

echo "[RUNTIME] starting x11vnc" >&2
x11vnc -display "${DISPLAY}" -forever -shared -localhost -nopw -rfbport 5900 > /tmp/aviator/x11vnc.log 2>&1 &
X11VNC_PID=$!

echo "[RUNTIME] starting browser server" >&2
python -m browser_collector.server > /tmp/aviator/browser-server.log 2>&1 &
SERVER_PID=$!

echo "[RUNTIME] starting Telegram bot" >&2
python -m bot > /tmp/aviator/bot.log 2>&1 &
BOT_PID=$!

echo "[RUNTIME] starting Railway collector" >&2
python -m tools.railway_collector > /tmp/aviator/collector.log 2>&1 &
COLLECTOR_PID=$!

# Forward child logs to Railway stdout/stderr so startup failures are visible.
tail -F /tmp/aviator/browser-server.log 2>/dev/null &
SERVER_LOG_PID=$!
tail -F /tmp/aviator/bot.log 2>/dev/null &
BOT_LOG_PID=$!
tail -F /tmp/aviator/collector.log 2>/dev/null &
COLLECTOR_LOG_PID=$!

cleanup() {
  kill "$COLLECTOR_LOG_PID" "$BOT_LOG_PID" "$SERVER_LOG_PID" 2>/dev/null || true
  kill "$COLLECTOR_PID" "$BOT_PID" "$SERVER_PID" "$X11VNC_PID" "$FLUXBOX_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while kill -0 "$SERVER_PID" 2>/dev/null    && kill -0 "$BOT_PID" 2>/dev/null    && kill -0 "$COLLECTOR_PID" 2>/dev/null; do
  sleep 5
done

for name in SERVER BOT COLLECTOR; do
  case "$name" in
    SERVER) pid="$SERVER_PID" ;;
    BOT) pid="$BOT_PID" ;;
    COLLECTOR) pid="$COLLECTOR_PID" ;;
  esac
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[RUNTIME] $name process exited (pid=$pid)" >&2
  fi
done

echo "Aviator Railway runtime: a critical process exited." >&2
exit 1
