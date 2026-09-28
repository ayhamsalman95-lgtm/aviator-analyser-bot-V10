#!/bin/bash
set -euo pipefail

mkdir -p /app/data /tmp/aviator

echo "[RUNTIME] starting browser API server" >&2
python -m browser_collector.server > /tmp/aviator/browser-server.log 2>&1 &
SERVER_PID=$!

echo "[RUNTIME] starting Telegram bot" >&2
python -m bot > /tmp/aviator/bot.log 2>&1 &
BOT_PID=$!

tail -F /tmp/aviator/browser-server.log 2>/dev/null &
SERVER_LOG_PID=$!
tail -F /tmp/aviator/bot.log 2>/dev/null &
BOT_LOG_PID=$!

cleanup() {
  kill "$BOT_LOG_PID" "$SERVER_LOG_PID" 2>/dev/null || true
  kill "$BOT_PID" "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$BOT_PID" 2>/dev/null; do
  sleep 5
done

echo "[RUNTIME] critical server/bot process exited" >&2

for name in SERVER BOT; do
  case "$name" in
    SERVER) pid="$SERVER_PID"; log="/tmp/aviator/browser-server.log" ;;
    BOT) pid="$BOT_PID"; log="/tmp/aviator/bot.log" ;;
  esac
  if kill -0 "$pid" 2>/dev/null; then
    echo "[RUNTIME] $name still running (pid=$pid)" >&2
  else
    echo "[RUNTIME] $name process exited (pid=$pid)" >&2
    cat "$log" >&2 || true
  fi
done

exit 1
