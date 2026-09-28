#!/bin/bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:100}"
DISPLAY_NUM="${DISPLAY#:}"
mkdir -p /app/data/chrome_profile /tmp/aviator

# The Railway container owns this display. Remove stale Xvfb artifacts left
# by an earlier crashed process before starting a fresh X server.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

echo "[RUNTIME] starting Xvfb on ${DISPLAY}" >&2
Xvfb "${DISPLAY}" -screen 0 1440x900x24 -ac +extension RANDR > /tmp/aviator/xvfb.log 2>&1 &
XVFB_PID=$!

# Confirm the Xvfb process itself is alive; a stale socket must not count as success.
for i in $(seq 1 50); do
  if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
    echo "[RUNTIME] Xvfb exited during startup" >&2
    cat /tmp/aviator/xvfb.log >&2 || true
    exit 1
  fi
  if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    break
  fi
  sleep 0.1
done

if ! kill -0 "${XVFB_PID}" 2>/dev/null || [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
  echo "[RUNTIME] Xvfb did not create a working display ${DISPLAY}" >&2
  cat /tmp/aviator/xvfb.log >&2 || true
  exit 1
fi

# A previous container crash can leave Chromium singleton lock files on the
# persistent volume. Remove them before opening the persistent profile.
if ! pgrep -x chromium >/dev/null 2>&1; then
  rm -f /app/data/chrome_profile/SingletonLock         /app/data/chrome_profile/SingletonCookie         /app/data/chrome_profile/SingletonSocket
fi

echo "[RUNTIME] starting fluxbox" >&2
fluxbox > /tmp/aviator/fluxbox.log 2>&1 &
FLUXBOX_PID=$!

echo "[RUNTIME] starting x11vnc" >&2
x11vnc -display "${DISPLAY}" -forever -shared -localhost -nopw -rfbport 5900 > /tmp/aviator/x11vnc.log 2>&1 &
X11VNC_PID=$!

echo "[RUNTIME] starting browser server" >&2
python -m browser_collector.server > /tmp/aviator/browser-server.log 2>&1 &
SERVER_PID=$!

# Keep the service alive through transient Telegram authentication/network
# failures. Updating TELEGRAM_BOT_TOKEN then becomes effective automatically.
(
  while true; do
    echo "[RUNTIME] starting Telegram bot child" >&2
    python -m bot > /tmp/aviator/bot.log 2>&1 || true
    echo "[RUNTIME] Telegram bot child exited; retrying" >&2
    sleep 10
  done
) &
BOT_PID=$!

# Keep the collector retrying if Chromium/network startup is transiently unavailable.
(
  while true; do
    echo "[RUNTIME] starting Railway collector child" >&2
    python -m tools.railway_collector > /tmp/aviator/collector.log 2>&1 || true
    echo "[RUNTIME] Railway collector child exited; retrying" >&2
    sleep 10
  done
) &
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

# The browser server and X server are the critical runtime components.
while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$XVFB_PID" 2>/dev/null; do
  sleep 5
done

echo "[RUNTIME] critical server/Xvfb process exited" >&2
echo "[RUNTIME] --- browser-server log ---" >&2
cat /tmp/aviator/browser-server.log >&2 || true
echo "[RUNTIME] --- Xvfb log ---" >&2
cat /tmp/aviator/xvfb.log >&2 || true
exit 1
