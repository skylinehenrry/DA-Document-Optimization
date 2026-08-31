@echo off
setlocal EnableExtensions DisableDelayedExpansion

REM Open DA Document Generator on Windows.
REM - Uses the project's virtual environment when available.
REM - Otherwise tries the Windows Python launcher, then python on PATH.
REM - launch.py verifies backend readiness before opening the browser.
REM - The backend is detached, so closing this window does not interrupt jobs.
REM - Startup errors remain visible so installation problems can be corrected.
REM - Absolute quoted paths work from another drive or a UNC share without cd.
REM - Disable inherited delayed expansion so ! in a folder name stays literal.

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0launch.py" %*
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        python "%~dp0launch.py" %*
    ) else (
        py -3 "%~dp0launch.py" %*
    )
)
set "DA_EXIT_CODE=%ERRORLEVEL%"
if not "%DA_EXIT_CODE%"=="0" pause
exit /b %DA_EXIT_CODE%
