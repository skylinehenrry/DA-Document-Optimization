#!/bin/zsh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

URL="http://127.0.0.1:8000/frontend/index.html"
LAUNCHER_TTY="$(tty)"

close_launcher_window() {
  osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Terminal"
  repeat with terminalWindow in windows
    repeat with terminalTab in tabs of terminalWindow
      if tty of terminalTab is "$LAUNCHER_TTY" then
        close terminalWindow
        return
      end if
    end repeat
  end repeat
end tell
APPLESCRIPT
}

echo "Starting DA Document Generator..."
echo "Project folder: $PROJECT_DIR"
echo ""

if nc -z 127.0.0.1 8000 >/dev/null 2>&1; then
  echo "The local server is already running."
  echo "Opening $URL"
  open "$URL"
  close_launcher_window
  exit 0
fi

echo "Opening $URL"
echo "Keep this window open while using the app."
echo "Close this window or press Control-C to stop the local server."
echo ""

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

for attempt in {1..40}; do
  if curl -fsS "$URL" >/dev/null 2>&1; then
    open "$URL"
    wait "$SERVER_PID"
    close_launcher_window
    exit 0
  fi
  sleep 0.25
done

echo "The local server did not become ready in time."
echo "Please check whether another process is blocking port 8000."
close_launcher_window
exit 1
