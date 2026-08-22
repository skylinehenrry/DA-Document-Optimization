from pathlib import Path
import asyncio
import os
import platform
import signal
import subprocess
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BACKEND_DIR = Path(__file__).parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
OUTPUTS_DIR = BACKEND_DIR / "outputs"
HEARTBEAT_TIMEOUT_SECONDS = 30
HEARTBEAT_CHECK_SECONDS = 5

last_heartbeat_at: float | None = None
frontend_has_connected = False


app = FastAPI(title="DA Document Generator")
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")


class BrowseFolderRequest(BaseModel):
    target: str
    current_path: str | None = None


class RunAnalysisRequest(BaseModel):
    script_folder: str
    da_document_folder: str
    model: str
    max_concurrency: int


@app.on_event("startup")
async def start_heartbeat_monitor() -> None:
    asyncio.create_task(shutdown_when_frontend_disconnects())


async def shutdown_when_frontend_disconnects() -> None:
    """
    Watch for frontend heartbeat activity and stop the local server when it ends.
    - Waits until the frontend has connected at least once
    - Checks whether the latest heartbeat is older than the timeout
    - Stops the backend process so the launcher window can close naturally
    """
    while True:
        await asyncio.sleep(HEARTBEAT_CHECK_SECONDS)

        if not frontend_has_connected or last_heartbeat_at is None:
            continue

        seconds_since_heartbeat = time.monotonic() - last_heartbeat_at

        if seconds_since_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
            os.kill(os.getpid(), signal.SIGTERM)


def select_folder(initial_dir: Path) -> str | None:
    """
    Open the operating system's native folder picker and return a folder path.
    - Uses AppleScript on macOS so the Python/Tkinter app does not pop up
    - Uses PowerShell's folder picker on Windows
    - Falls back to no selection on unsupported systems
    """
    system_name = platform.system()

    if system_name == "Darwin":
        script = (
            'set selectedFolder to choose folder with prompt "Select Folder" '
            f'default location POSIX file "{initial_dir}"\n'
            "return POSIX path of selectedFolder"
        )

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            return result.stdout.strip() or None

        return None

    if system_name == "Windows":
        script = rf"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Select Folder"
$dialog.SelectedPath = "{initial_dir}"
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
    $dialog.SelectedPath
}}
"""

        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            return result.stdout.strip() or None

    return None


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/frontend/index.html")


@app.post("/api/browse-folder")
async def browse_folder(request: BrowseFolderRequest) -> dict[str, str | None]:
    """
    Tool for selecting a local folder from the frontend.
    - Opens a native folder picker on the local machine
    - Returns the selected folder path as text
    - Lets the frontend fill the matching path input
    """
    initial_dir = request.current_path or str(PROJECT_DIR)

    if not Path(initial_dir).exists():
        initial_dir = str(PROJECT_DIR)

    selected_path = select_folder(Path(initial_dir))

    return {"path": selected_path or None}


@app.post("/api/heartbeat")
async def heartbeat() -> dict[str, str]:
    """
    Lightweight signal from the browser that the frontend is still open.
    - The frontend sends this every few seconds
    - The backend uses it to decide when to shut down automatically
    - No project data or user input is sent through this endpoint
    """
    global frontend_has_connected, last_heartbeat_at

    frontend_has_connected = True
    last_heartbeat_at = time.monotonic()

    return {"status": "ok"}


@app.post("/api/run")
def run_analysis(request: RunAnalysisRequest) -> None:
    """
    Placeholder endpoint for the DA Document workflow runner.
    - The frontend is already wired to call this endpoint
    - The next backend step is to connect it to the async analysis pipeline
    - Keeping this explicit avoids silently pretending the workflow has run
    """
    raise HTTPException(
        status_code=501,
        detail="Analysis runner is not connected yet.",
    )
