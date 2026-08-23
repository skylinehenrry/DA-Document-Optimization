#!/bin/zsh

# macOS launcher for the DA Document Generator.
#
# How to use this file:
# - Double-click this .command file from Finder
# - It opens a Terminal window and starts the local Python backend
# - It opens the browser UI after the backend is ready
# - The browser is the screen; the Python backend is the engine
# - The backend stops after the browser heartbeat disappears
# - The launcher tries to close its own Terminal window after shutdown
#
# Important idea:
# - 127.0.0.1 means "this computer only"
# - Nothing is hosted publicly by this launcher
# - The app is only reachable from the local machine unless changed later

set -e

# Find the folder where this launcher lives.
# This lets the user double-click the launcher from anywhere, while the script
# still runs from the DA Document Optimization project folder.
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# The frontend page served by the local Python backend.
# We open the explicit frontend page to avoid relying on browser redirects.
URL="http://127.0.0.1:8000/frontend/index.html"

# Record the Terminal tab running this script.
# Later, AppleScript uses this value to close only this launcher window.
LAUNCHER_TTY="$(tty)"

close_launcher_window() {
  # Close this launcher's Terminal window after the script finishes.
  #
  # Why the delay:
  # - Terminal can be stubborn if a script tries to close its own window too early
  # - Waiting briefly lets the shell finish first
  # - &! detaches the close task so it can survive after this script exits
  (
    sleep 0.7
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
  ) >/dev/null 2>&1 &!
}

echo "Starting DA Document Generator..."
echo "Project folder: $PROJECT_DIR"
echo ""

if nc -z 127.0.0.1 8000 >/dev/null 2>&1; then
  # Check whether something is already listening on port 8000.
  #
  # If yes:
  # - The backend server is already running
  # - We do not start a duplicate server
  # - We simply open the browser UI and close this launcher window
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

# Start uvicorn, which runs the FastAPI backend defined in backend/app.py.
#
# The "&" means:
# - Start the server in the background from this shell
# - Save its process ID so we can stop it later if needed
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 &
SERVER_PID=$!

cleanup() {
  # Stop the backend if the launcher is interrupted before normal shutdown.
  #
  # This handles cases like:
  # - The user presses Control-C
  # - Terminal is closed while the backend is still starting
  # - The launcher exits before the heartbeat shutdown takes over
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}

# Run cleanup when the launcher exits or receives an interrupt signal.
trap cleanup EXIT INT TERM

for attempt in {1..40}; do
  # Wait until the backend is actually reachable before opening the browser.
  #
  # Without this wait:
  # - Safari may open before the server is ready
  # - The user may see a temporary loading error or spinning cursor
  if curl -fsS "$URL" >/dev/null 2>&1; then
    open "$URL"

    # Keep this launcher alive while uvicorn is alive.
    # Once the frontend heartbeat stops, backend/app.py terminates uvicorn,
    # this wait finishes, and the launcher can close its Terminal window.
    wait "$SERVER_PID"
    close_launcher_window
    exit 0
  fi
  sleep 0.25
done

echo "The local server did not become ready in time."
echo "Please check whether another process is blocking port 8000."
kill "$SERVER_PID" >/dev/null 2>&1 || true
close_launcher_window
exit 1
