#!/bin/bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
mkdir -p /app/data/chrome_profile /tmp/aviator

Xvfb "${DISPLAY}" -screen 0 1440x900x24 -ac +extension RANDR > /tmp/aviator/xvfb.log 2>&1 &
XVFB_PID=$!

fluxbox > /tmp/aviator/fluxbox.log 2>&1 &
FLUXBOX_PID=$!

x11vnc -display "${DISPLAY}" -forever -shared -localhost -nopw -rfbport 5900 > /tmp/aviator/x11vnc.log 2>&1 &
X11VNC_PID=$!

python -m browser_collector.server > /tmp/aviator/browser-server.log 2>&1 &
SERVER_PID=$!

python -m bot > /tmp/aviator/bot.log 2>&1 &
BOT_PID=$!

python -m tools.railway_collector > /tmp/aviator/collector.log 2>&1 &
COLLECTOR_PID=$!

cleanup() {
  kill "$COLLECTOR_PID" "$BOT_PID" "$SERVER_PID" "$X11VNC_PID" "$FLUXBOX_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while kill -0 "$SERVER_PID" 2>/dev/null    && kill -0 "$BOT_PID" 2>/dev/null    && kill -0 "$COLLECTOR_PID" 2>/dev/null; do
  sleep 5
done

echo "Aviator Railway runtime: a critical process exited." >&2
exit 1
