"""
Serve the local DA Workflow application and its saved operations.
- The browser displays the interface; Python reads project folders and renders
  reviewed graphs without executing any of the analyzed source programs.
- Browser actions are durable jobs. Closing a tab does not stop the backend or
  remove its progress, draft revisions or generated flowcharts.
- A restarted backend recovers committed results and identifies interrupted work.
  Interrupted model requests require an explicit retry because they may cost money.
- Only the frontend directory is public. Drafts, source snapshots and reports are
  served through ownership-checked API routes rather than a global output folder.
- Local host/origin checks stop unrelated websites from invoking local actions.
- The app factory keeps tests isolated from the real user's database and process.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .graph_models import StrictModel, utc_now
from .workflow_api import router as workflow_router
from .workflow_jobs import JobConflict, WorkflowJobs, router as job_router
from .workflow_service import ReviewRequired, WorkflowService, default_service
from .workflow_store import DraftNotFound, RevisionConflict


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
APP_ID = "da-workflow"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
log = logging.getLogger(__name__)


class BrowseFolderRequest(StrictModel):
    """
    Ask the operating system to choose a folder for one interface input.
    - target identifies the input; it is never interpolated into an OS command.
    - current_path is passed as data to the folder dialog, not executable script.
    - The user may also type or paste a path when native dialogs are unavailable.
    """

    target: str = Field(min_length=1, max_length=100)
    current_path: str | None = Field(default=None, max_length=32768)


class ShutdownRequest(StrictModel):
    instance_id: str = Field(min_length=1, max_length=100)


def select_folder(initial_dir: Path) -> str | None:
    """
    Return a folder chosen through the local machine's native dialog.
    - The caller runs this blocking operation in a thread so health/progress polls
      still work while the user is deciding which folder to select.
    - Paths are supplied as AppleScript arguments or environment data; quotes,
      apostrophes, dollar signs and backticks in paths cannot become commands.
    - Cancellation, a missing dialog utility or a five-minute timeout returns None.
    """
    system_name = platform.system()
    if system_name == "Darwin":
        script = """on run argv
    set selectedFolder to choose folder with prompt "Select Folder" default location POSIX file (item 1 of argv)
    return POSIX path of selectedFolder
end run"""
        command = ["osascript", "-e", script, str(initial_dir)]
        environment = None
    elif system_name == "Windows":
        script = """[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select Folder'
$dialog.SelectedPath = $env:DA_FOLDER_PICKER_INITIAL
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $dialog.SelectedPath
}
$dialog.Dispose()
"""
        command = ["powershell", "-NoProfile", "-STA", "-Command", script]
        environment = {**os.environ, "DA_FOLDER_PICKER_INITIAL": str(initial_dir)}
    else:
        return None
    try:
        # - PowerShell otherwise uses a legacy console code page on many systems.
        # - Encode/decode both native dialog results explicitly as UTF-8 so names
        #   containing Japanese text or accented characters remain exact.
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False,
                                env=environment, timeout=300)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def create_app(service: WorkflowService | None = None, *, shutdown_callback: Callable[[], None] | None = None) -> FastAPI:
    """
    Build an application with its own service, worker and process identity.
    - Production uses the existing DA_WORKFLOW_STORE path or the established
      backend/.workflow_store default, preserving previously saved work.
    - Tests supply a temporary service and optional harmless shutdown callback.
    - There is deliberately no browser heartbeat or disconnect-triggered shutdown.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.workflow_jobs = WorkflowJobs(application.state.workflow_service, application.state.instance_id)
        await application.state.workflow_jobs.start()
        try:
            yield
        finally:
            await application.state.workflow_jobs.stop()

    application = FastAPI(title="DA Workflow", version="2.0", lifespan=lifespan)
    application.state.workflow_service = service or default_service()
    application.state.instance_id = "instance_" + uuid4().hex
    application.state.started_at = utc_now()
    application.state.active_workflow_jobs = 0
    application.state.workflow_logger = lambda message: log.info("%s", message)
    application.state.stopping = False
    application.state.managed = os.environ.get("DA_WORKFLOW_MANAGED") == "1"
    application.state.shutdown_callback = shutdown_callback
    application.include_router(workflow_router)
    application.include_router(job_router)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @application.middleware("http")
    async def protect_local_actions(request: Request, call_next):
        """
        Restrict browser actions to this exact local application origin.
        - Command-line clients may omit Origin; cross-site browser posts may not.
        - Disable new mutations once a managed shutdown has been accepted.
        - Tell browsers not to cache API state or guess downloaded content types.
        """
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (origin is not None and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "Local workflow requests must come from this application's origin."},
                                    headers=SECURITY_HEADERS)
            if application.state.stopping:
                return JSONResponse(status_code=503, content={"detail": "The backend is stopping. Restart it before continuing."},
                                    headers=SECURITY_HEADERS)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/") or request.url.path.startswith("/frontend/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RevisionConflict)
    async def revision_conflict_handler(request, error: RevisionConflict):
        return JSONResponse(status_code=409, content={"detail": str(error), "expected_revision": error.expected,
                                                     "current_revision": error.actual})

    @application.exception_handler(ReviewRequired)
    @application.exception_handler(JobConflict)
    async def workflow_conflict_handler(request, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.exception_handler(DraftNotFound)
    @application.exception_handler(FileNotFoundError)
    async def missing_artifact_handler(request, error):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.exception_handler(ValueError)
    async def invalid_workflow_handler(request, error):
        detail = error.errors(include_context=False, include_input=False) if isinstance(error, ValidationError) else str(error)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})

    @application.get("/")
    async def index():
        return RedirectResponse(url="/frontend/index.html")

    @application.get("/api/health")
    async def health():
        """
        Identify this backend without waiting for analysis or reading the database.
        - Instance identity lets the UI recognize a restart and restore saved work.
        - Project/store identity lets the launcher avoid reusing an unrelated server.
        - Worker state also explains when another process owns this store's queue.
        """
        manager = getattr(application.state, "workflow_jobs", None)
        return {"status": "stopping" if application.state.stopping else "ok", "app_id": APP_ID,
                "version": "2.0", "instance_id": application.state.instance_id, "pid": os.getpid(),
                "project_root": str(PROJECT_DIR), "store_root": str(application.state.workflow_service.store.directory),
                "started_at": application.state.started_at,
                "worker_state": manager.worker_state if manager is not None else "starting",
                "current_job_id": manager.current_job_id if manager is not None else None,
                "managed": application.state.managed,
                "shutdown_available": application.state.managed or application.state.shutdown_callback is not None}

    @application.post("/api/browse-folder")
    async def browse_folder(body: BrowseFolderRequest):
        initial = Path(body.current_path).expanduser() if body.current_path else PROJECT_DIR
        if not initial.is_dir():
            initial = PROJECT_DIR
        selected = await asyncio.to_thread(select_folder, initial)
        return {"path": selected, "target": body.target}

    @application.post("/api/shutdown")
    async def shutdown(body: ShutdownRequest):
        """
        Stop only the exact idle backend identified by the launcher or browser.
        - An instance mismatch refuses stale requests after a restart.
        - Running or queued work must finish before a managed stop is allowed.
        - Ordinary imported/test apps never send process signals by default.
        """
        if body.instance_id != application.state.instance_id:
            raise HTTPException(status_code=409, detail="This is a different backend instance. Refresh its status before stopping it.")
        if not application.state.managed and application.state.shutdown_callback is None:
            raise HTTPException(status_code=409, detail="This backend was started manually. Stop it from its terminal after work finishes.")
        manager = application.state.workflow_jobs
        async with manager.submission_lock:
            manager.accepting = False
            try:
                if application.state.active_workflow_jobs or await asyncio.to_thread(manager.repository.busy):
                    raise HTTPException(status_code=409, detail="Work is still queued or running. Wait for it to finish before stopping the backend.")
                application.state.stopping = True
            except (OSError, sqlite3.Error) as error:
                log.warning("Could not check saved jobs before shutdown: %s", error)
                raise HTTPException(status_code=503, detail="Could not verify whether work is still running. The backend has been left running; check the storage location and try stopping it again.") from error
            finally:
                # - A cancelled request or a temporary storage error must not
                #   leave an otherwise healthy server rejecting every new job.
                # - Keep submissions disabled only after an idle stop is accepted.
                if not application.state.stopping:
                    manager.accepting = True

        def finish_shutdown():
            callback = application.state.shutdown_callback
            if callback is not None:
                callback()
            elif application.state.managed:
                # - Raise the signal in this process so uvicorn's installed
                #   handler runs its normal lifespan shutdown on Windows too.
                # - os.kill(..., SIGTERM) uses unconditional TerminateProcess
                #   on Windows and would skip that cleanup.
                # - Do not broadcast Ctrl+C or Ctrl+Break to a console group.
                signal.raise_signal(signal.SIGTERM)

        asyncio.get_running_loop().call_later(0.2, finish_shutdown)
        return {"status": "stopping", "instance_id": application.state.instance_id}

    @application.api_route("/api/run", methods=["POST"])
    @application.api_route("/api/logs", methods=["GET"])
    @application.api_route("/api/heartbeat", methods=["POST"])
    @application.api_route("/api/output-status", methods=["POST"])
    @application.api_route("/api/output/{file_name:path}", methods=["GET"])
    async def retired_route():
        """
        Explain obsolete links instead of guessing which project they refer to.
        - The old global run/log/output state could disappear or point elsewhere.
        - Existing draft and generation URLs continue to use their saved identity.
        """
        raise HTTPException(status_code=410, detail="This legacy route has been retired. Reload the app, open a saved draft, and use its job progress or generation-specific download link.")

    application.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
    return application


app = create_app()
