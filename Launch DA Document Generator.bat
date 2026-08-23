@echo off
REM Windows launcher for the DA Document Generator.
REM
REM How to use this file:
REM - Double-click this .bat file from File Explorer
REM - It opens a Command Prompt window and starts the local Python backend
REM - It opens the browser UI at http://127.0.0.1:8000/frontend/index.html
REM - The browser is the screen; the Python backend is the engine
REM - The backend stops after the browser heartbeat disappears
REM - The Command Prompt closes after the backend stops
REM
REM Important idea:
REM - 127.0.0.1 means "this computer only"
REM - Nothing is hosted publicly by this launcher
REM - The app is only reachable from the local machine unless changed later

setlocal

REM %~dp0 means "the folder where this BAT file is located".
REM This makes the launcher work even if the user double-clicks it from File Explorer.
set "PROJECT_DIR=%~dp0"

REM The frontend page served by the local Python backend.
set "URL=http://127.0.0.1:8000/frontend/index.html"

REM Move Command Prompt into the project folder before running Python.
cd /d "%PROJECT_DIR%"

echo Starting DA Document Generator...
echo Project folder: %PROJECT_DIR%
echo.

REM Check whether port 8000 is already active.
REM If this succeeds, the backend server is already running,
REM so the launcher should open the browser but avoid starting a duplicate server.
powershell -NoProfile -Command ^
  "$client = New-Object Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1', 8000); $client.Close(); exit 0 } catch { exit 1 }"

if %ERRORLEVEL% EQU 0 (
    REM Reuse an existing local server instead of starting a duplicate one.
    echo The local server is already running.
    echo Opening %URL%
    start "" "%URL%"
    exit
)

echo Opening %URL%
echo Keep this window open while using the app.
echo Close this window or press Ctrl+C to stop the local server.
echo.

REM Open the browser UI.
REM The Python server starts immediately after this command.
start "" "%URL%"

REM Start uvicorn, which runs the FastAPI backend defined in backend/app.py.
REM This command stays in the foreground while the app is running.
REM When the frontend heartbeat stops, backend/app.py shuts uvicorn down.
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

echo.
echo Local server stopped.

REM Close the launcher console after the backend stops.
exit
