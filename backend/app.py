"""
Local web app for the DA Document Generator.

This is the "engine" behind the HTML user interface.
The user sees frontend/index.html in the browser, but the browser cannot run
Python, inspect local folders freely, or execute the document generation flow.
This FastAPI server receives requests from the browser and performs those
local-machine tasks on behalf of the UI.

This module provides the lightweight backend used by the browser UI:
- Serves the frontend files from the frontend folder
- Serves generated outputs from backend/outputs
- Provides folder browsing through native operating system dialogs
- Receives frontend heartbeat pings and shuts down when the UI is closed
- Exposes a placeholder run endpoint for the analysis workflow
"""

from pathlib import Path
import asyncio
from datetime import datetime
import os
import platform
import signal
import subprocess
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from .main import run_da_document_workflow
except ImportError:
    from main import run_da_document_workflow


BACKEND_DIR = Path(__file__).parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
OUTPUTS_DIR = BACKEND_DIR / "outputs"

# Heartbeat tuning.
#
# The frontend sends a heartbeat every few seconds while the browser page is open.
# If no heartbeat arrives for HEARTBEAT_TIMEOUT_SECONDS, the backend assumes the
# user closed the frontend and safely shuts itself down.
HEARTBEAT_TIMEOUT_SECONDS = 30
HEARTBEAT_CHECK_SECONDS = 5

# Heartbeat state is process-local because this app is intended for one local user.
last_heartbeat_at: float | None = None
frontend_has_connected = False
latest_output_dir: Path | None = None
run_logs: list[str] = []


app = FastAPI(title="DA Document Generator")

# The backend owns generated outputs, while the UI stays in the separate frontend folder.
#
# /frontend/... lets the browser load index.html, styles.css, and app.js.
# /outputs/... lets the browser open generated JSON, HTML, and document files.
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")


class BrowseFolderRequest(BaseModel):
    """
    Request body for folder browsing.
    - target identifies which frontend input requested the folder
    - current_path lets the dialog open near the user's existing path
    """

    target: str
    current_path: str | None = None


class RunAnalysisRequest(BaseModel):
    """
    Request body for running the DA Document workflow.
    - script_folder is the folder containing scripts to analyze
    - da_document_folder is where final DA outputs should be saved
    - model controls the selected LLM provider
    - max_concurrency controls parallel LLM requests
    """

    script_folder: str
    da_document_folder: str
    model: str
    max_concurrency: int


class OutputStatusRequest(BaseModel):
    """
    Request body for checking generated output readiness.
    - da_document_folder is the user-selected DA Document folder
    - The backend checks the outputs folder inside that selected folder
    """

    da_document_folder: str


class RunAnalysisResponse(BaseModel):
    """
    Response body returned to the frontend after a successful run.
    - output_folder is the selected DA Document folder
    - outputs contains browser links for generated files
    - message gives the frontend a short success status
    """

    output_folder: str
    outputs: dict[str, str | None]
    message: str


class OutputStatusResponse(BaseModel):
    """
    Response body for output card readiness.
    - output_folder is the folder where generated files should exist
    - outputs contains clickable links only for files that are ready
    - missing contains output names that are not available yet
    """

    output_folder: str
    outputs: dict[str, str | None]
    missing: list[str]


class RunLogResponse(BaseModel):
    """
    Response body for frontend log polling.
    - logs contains timestamped messages from the current or latest run
    - The frontend renders these messages in the Run Log panel
    """

    logs: list[str]


OUTPUT_FILE_MAP = {
    "da_document": "DA_Document.docx",
    "flowchart": "workflow_flowchart.html",
    "profiles": "profiles.json",
    "network": "workflow_network.json",
    "summaries": "summaries.json",
    "flowchart_spec": "flowchart_spec.json",
}


def build_output_status(output_dir: Path) -> OutputStatusResponse:
    """
    Check which generated outputs are ready for the frontend cards.
    - Looks inside the actual output directory used by the workflow
    - Returns a browser-safe /api/output link only when the file exists
    - Keeps missing files visible but disabled in the UI
    """
    outputs: dict[str, str | None] = {}
    missing: list[str] = []

    for output_key, file_name in OUTPUT_FILE_MAP.items():
        if (output_dir / file_name).is_file():
            outputs[output_key] = f"/api/output/{file_name}"
        else:
            outputs[output_key] = None
            missing.append(output_key)

    return OutputStatusResponse(
        output_folder = str(output_dir),
        outputs = outputs,
        missing = missing,
    )


def add_run_log(message: str) -> None:
    """
    Append one timestamped message to the in-memory run log.
    - Keeps logging simple for the local single-user app
    - Gives the frontend a readable progress trail
    - Mirrors the kind of progress previously visible only in Terminal
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    run_logs.append(f"{timestamp} - {message}")


@app.on_event("startup")
async def start_heartbeat_monitor() -> None:
    """
    Start the background monitor used for automatic local server shutdown.
    - Runs once when FastAPI starts
    - Leaves the server alive until a browser page has connected
    - Delegates shutdown timing to shutdown_when_frontend_disconnects
    """
    # FastAPI calls this automatically when uvicorn starts the app.
    # The monitor runs in the background while normal web requests continue.
    asyncio.create_task(shutdown_when_frontend_disconnects())


async def shutdown_when_frontend_disconnects() -> None:
    """
    Watch for frontend heartbeat activity and stop the local server when it ends.
    - Waits until the frontend has connected at least once
    - Checks whether the latest heartbeat is older than the timeout
    - Stops the backend process so the launcher window can close naturally
    """
    while True:
        # Sleep between checks so this monitor stays lightweight.
        await asyncio.sleep(HEARTBEAT_CHECK_SECONDS)

        # Do not auto-shutdown until a browser has connected at least once.
        # This prevents the server from closing during startup or isolated tests.
        if not frontend_has_connected or last_heartbeat_at is None:
            continue

        seconds_since_heartbeat = time.monotonic() - last_heartbeat_at

        if seconds_since_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
            # SIGTERM lets uvicorn perform its normal shutdown sequence.
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
        # AppleScript opens the normal macOS folder picker without showing Tkinter.
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
        # PowerShell gives Windows users the standard folder browser dialog.
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
    """
    Browser entry point.
    - Redirects to the frontend index page
    - Keeps frontend asset links relative and file-friendly
    - Lets users open the app from the short localhost URL
    """
    return RedirectResponse(url="/frontend/index.html")


@app.post("/api/browse-folder")
async def browse_folder(request: BrowseFolderRequest) -> dict[str, str | None]:
    """
    Tool for selecting a local folder from the frontend.
    - Opens a native folder picker on the local machine
    - Returns the selected folder path as text
    - Lets the frontend fill the matching path input
    """
    # Start near the current value shown in the frontend input when possible.
    # If that path is blank or invalid, use the project folder as a safe default.
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

    # time.monotonic is used for elapsed-time checks because it is not affected
    # by system clock changes.
    frontend_has_connected = True
    last_heartbeat_at = time.monotonic()

    return {"status": "ok"}


@app.post("/api/run")
async def run_analysis(request: RunAnalysisRequest) -> RunAnalysisResponse:
    """
    Run the DA Document workflow from frontend parameters.
    - Uses Script Folder as the source folder to analyze
    - Uses DA Document Folder as the output root
    - Generated files are saved into an outputs folder inside that root
    - Returns stable local-server links to the generated outputs
    """
    global latest_output_dir

    run_logs.clear()
    add_run_log("Analysis requested from frontend.")

    try:
        output_paths = await run_da_document_workflow(
            script_folder = request.script_folder,
            da_document_folder = request.da_document_folder,
            model = request.model,
            max_concurrency = request.max_concurrency,
            logger = add_run_log,
        )
    except FileNotFoundError as error:
        add_run_log(f"Analysis failed: {error}")
        raise HTTPException(status_code = 400, detail = str(error)) from error
    except ValueError as error:
        add_run_log(f"Analysis failed: {error}")
        raise HTTPException(status_code = 400, detail = str(error)) from error
    except Exception as error:
        add_run_log(f"Analysis failed unexpectedly: {error}")
        raise HTTPException(status_code = 500, detail = str(error)) from error

    latest_output_dir = output_paths["profiles"].parent

    output_status = build_output_status(latest_output_dir)

    return RunAnalysisResponse(
        output_folder = output_status.output_folder,
        outputs = output_status.outputs,
        message = "Analysis complete.",
    )


@app.post("/api/output-status")
async def get_output_status(request: OutputStatusRequest) -> OutputStatusResponse:
    """
    Check whether expected output files already exist for the selected folder.
    - Lets the frontend disable cards before files are generated
    - Uses <DA Document Folder>/outputs as the canonical output location
    - Updates latest_output_dir so stable /api/output links open from this folder
    """
    global latest_output_dir

    output_dir = Path(request.da_document_folder).expanduser() / "outputs"
    latest_output_dir = output_dir

    return build_output_status(output_dir)


@app.get("/api/logs")
async def get_logs() -> RunLogResponse:
    """
    Return current run log messages for the frontend Run Log panel.
    - The frontend polls this endpoint while analysis is running
    - Messages are kept in memory for the latest run
    - No script content or extracted data is returned here
    """
    return RunLogResponse(logs = run_logs)


@app.get("/api/output/{file_name}")
async def get_latest_output(file_name: str) -> FileResponse:
    """
    Serve a generated output file from the latest selected DA Document folder.
    - Keeps the frontend links stable across projects
    - Avoids exposing arbitrary local files
    - Only serves files inside the latest output folder
    """
    if latest_output_dir is None:
        raise HTTPException(status_code = 404, detail = "No analysis output is available yet.")

    output_dir = latest_output_dir.resolve()
    output_path = (output_dir / file_name).resolve()

    if output_dir not in output_path.parents and output_path != output_dir:
        raise HTTPException(status_code = 400, detail = "Invalid output path.")

    if not output_path.is_file():
        raise HTTPException(status_code = 404, detail = f"Output file not found: {file_name}")

    return FileResponse(output_path)
