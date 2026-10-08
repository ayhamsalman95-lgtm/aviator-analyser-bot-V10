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
  kill "$XVFB_PID" 2>/dev/null || true
  [[ -n "${VNC_PID:-}" ]] && kill "$VNC_PID" 2>/dev/null || true
  [[ -n "${WS_PID:-}" ]] && kill "$WS_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 2

echo "[NOVNC] Starting VNC server on 5900"
x11vnc -display "$DISPLAY" -rfbport 5900 -forever -shared -nopw -localhost >/tmp/aviator-x11vnc.log 2>&1 &
VNC_PID=$!

echo "[NOVNC] Starting noVNC on 6080"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/aviator-novnc.log 2>&1 &
WS_PID=$!

sleep 2

echo "[NOVNC] Starting Chrome"
google-chrome \
  --user-data-dir="$PROFILE" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-notifications \
  --window-size=1440,900 \
  "https://1xlite-130003.top/ar/" >/tmp/aviator-chrome.log 2>&1 &

echo
echo "[NOVNC] Ready."
echo "[NOVNC] Open forwarded port 6080 in the Codespace Ports panel."
echo "[NOVNC] Then open Chrome in noVNC and log in manually."
echo "[NOVNC] Do NOT paste your password into the terminal."
echo "[NOVNC] Keep this terminal running while using the browser."
echo

wait
