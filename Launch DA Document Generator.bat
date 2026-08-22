@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "URL=http://127.0.0.1:8000/frontend/index.html"

cd /d "%PROJECT_DIR%"

echo Starting DA Document Generator...
echo Project folder: %PROJECT_DIR%
echo.

powershell -NoProfile -Command ^
  "$client = New-Object Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1', 8000); $client.Close(); exit 0 } catch { exit 1 }"

if %ERRORLEVEL% EQU 0 (
    echo The local server is already running.
    echo Opening %URL%
    start "" "%URL%"
    exit
)

echo Opening %URL%
echo Keep this window open while using the app.
echo Close this window or press Ctrl+C to stop the local server.
echo.

start "" "%URL%"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

echo.
echo Local server stopped.
exit
