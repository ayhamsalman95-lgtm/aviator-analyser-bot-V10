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

echo "[RUNTIME] critical child state:" >&2
for name in SERVER BOT COLLECTOR; do
  case "$name" in
    SERVER) pid="$SERVER_PID"; log="/tmp/aviator/browser-server.log" ;;
    BOT) pid="$BOT_PID"; log="/tmp/aviator/bot.log" ;;
    COLLECTOR) pid="$COLLECTOR_PID"; log="/tmp/aviator/collector.log" ;;
  esac
  if kill -0 "$pid" 2>/dev/null; then
    echo "[RUNTIME] $name still running (pid=$pid)" >&2
  else
    echo "[RUNTIME] $name process exited (pid=$pid)" >&2
    echo "[RUNTIME] --- $name log ---" >&2
    cat "$log" >&2 || true
    echo "[RUNTIME] --- end $name log ---" >&2
  fi
done

echo "[RUNTIME] --- X11VNC log ---" >&2
cat /tmp/aviator/x11vnc.log >&2 || true
echo "[RUNTIME] --- end X11VNC log ---" >&2

echo "Aviator Railway runtime: a critical process exited." >&2
exit 1
