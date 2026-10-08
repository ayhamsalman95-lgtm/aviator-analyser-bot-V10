#!/usr/bin/env bash
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

PROFILE="${PWD}/chrome_profile"
mkdir -p "$PROFILE"

echo "[NOVNC] Starting virtual display on $DISPLAY"
Xvfb "$DISPLAY" -screen 0 1440x900x24 -ac +extension GLX +render -noreset >/tmp/aviator-xvfb.log 2>&1 &
XVFB_PID=$!

cleanup() {
  echo "[NOVNC] Cleaning up services..." >&2
  kill "$XVFB_PID" 2>/dev/null || true
  [[ -n "${VNC_PID:-}" ]] && kill "$VNC_PID" 2>/dev/null || true
  [[ -n "${WS_PID:-}" ]] && kill "$WS_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

sleep 2

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  echo "[NOVNC] ERROR: Xvfb failed to start."
  cat /tmp/aviator-xvfb.log || true
  exit 1
fi

echo "[NOVNC] Starting VNC server on 5900"
x11vnc -display "$DISPLAY" -rfbport 5900 -forever -shared -nopw -localhost >/tmp/aviator-x11vnc.log 2>&1 &
VNC_PID=$!

echo "[NOVNC] Starting noVNC on 6080"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/aviator-novnc.log 2>&1 &
WS_PID=$!

sleep 2

if ! kill -0 "$VNC_PID" 2>/dev/null; then
  echo "[NOVNC] ERROR: x11vnc failed to start."
  cat /tmp/aviator-x11vnc.log || true
  exit 1
fi

if ! kill -0 "$WS_PID" 2>/dev/null; then
  echo "[NOVNC] ERROR: websockify failed to start."
  cat /tmp/aviator-novnc.log || true
  exit 1
fi

echo "[NOVNC] Starting Chrome"
google-chrome \
  --user-data-dir="$PROFILE" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-notifications \
  --window-size=1440,900 \
  "https://1xlite-130003.top/ar/" >/tmp/aviator-chrome.log 2>&1 &
CHROME_PID=$!

sleep 3

if ! kill -0 "$CHROME_PID" 2>/dev/null; then
  echo "[NOVNC] ERROR: Chrome exited during startup."
  tail -50 /tmp/aviator-chrome.log || true
  exit 1
fi

echo
echo "[NOVNC] Ready."
echo "[NOVNC] Xvfb PID=$XVFB_PID"
echo "[NOVNC] x11vnc PID=$VNC_PID"
echo "[NOVNC] noVNC PID=$WS_PID"
echo "[NOVNC] Chrome PID=$CHROME_PID"
echo "[NOVNC] Open forwarded port 6080 in the Codespace Ports panel."
echo "[NOVNC] Then open /vnc.html in the forwarded 6080 URL."
echo "[NOVNC] Do NOT use port 5900 in the browser."
echo "[NOVNC] Do NOT paste your password into the terminal."
echo "[NOVNC] Keep this terminal running while using the browser."
echo

while true; do
  sleep 10

  if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[NOVNC] ERROR: Xvfb stopped unexpectedly."
    exit 1
  fi

  if ! kill -0 "$VNC_PID" 2>/dev/null; then
    echo "[NOVNC] ERROR: x11vnc stopped unexpectedly."
    tail -50 /tmp/aviator-x11vnc.log || true
    exit 1
  fi

  if ! kill -0 "$WS_PID" 2>/dev/null; then
    echo "[NOVNC] ERROR: websockify stopped unexpectedly."
    tail -50 /tmp/aviator-novnc.log || true
    exit 1
  fi

  if ! kill -0 "$CHROME_PID" 2>/dev/null; then
    echo "[NOVNC] WARNING: Chrome stopped. Restarting Chrome..."
    google-chrome \
      --user-data-dir="$PROFILE" \
      --no-sandbox \
      --disable-dev-shm-usage \
      --disable-notifications \
      --window-size=1440,900 \
      "https://1xlite-130003.top/ar/" >/tmp/aviator-chrome.log 2>&1 &
    CHROME_PID=$!
  fi
done
