"""Start, reopen, or stop this computer's DA Document Generator.

- Uses only the standard library, so missing application packages can be explained.
- Starts one detached local server; closing the launcher does not interrupt work.
- Opens each launcher invocation with a new browser-session identifier so previous
  run history and draft navigation do not reappear in the new workspace.
- Checks application identity and project location before reusing a listening port.
- Opens the browser only after the correct server reports that it is ready.
- Keeps startup errors in the private store instead of losing a command window.
- Provides ``--status`` and ``--stop`` without guessing or killing a process ID.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4
import webbrowser


PROJECT_DIR = Path(__file__).resolve().parent
APP_ID = "da-workflow"


class LaunchError(RuntimeError):
    """An actionable startup problem that should remain visible to the user."""


class NoRedirect(HTTPRedirectHandler):
    """Keep requests on their explicitly selected local URL.

    - An unrelated server cannot redirect this client to another service.
    - Local requests also bypass system proxy settings through ``ProxyHandler``.
    """

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def configure_console() -> None:
    """Keep startup/status messages usable in legacy Windows consoles.

    - Preserve the terminal's selected encoding so its normal text still displays.
    - Escape characters that encoding cannot represent instead of crashing while
      printing a Unicode folder name or a backend error.
    - Leave test streams and other non-reconfigurable outputs untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors = "backslashreplace")
            except (OSError, ValueError):
                pass


def detached_process_options(platform_name: str | None = None) -> dict:
    """Select the native process options that survive a closed command window.

    - Windows starts a detached process in a new process group with explicit log
      handles; it does not inherit the launcher's console or stdin.
    - POSIX starts a new session so terminal hangup does not stop the backend.
    - Keep this selection separate from path handling for cross-platform tests.
    """
    if (platform_name or os.name) == "nt":
        # - These are the documented Win32 flag values; getattr also allows the
        #   Windows branch to be verified on a non-Windows development machine.
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": detached | process_group}
    return {"start_new_session": True}


def require_local_store(directory: str | Path, platform_name: str | None = None) -> None:
    """Refuse an unsafe Windows network location for the private SQLite store.

    - Source projects and output folders may be on a network share.
    - SQLite WAL and the worker lock need the private store on this computer's
      local disk; a share is not a supported place to keep recovery state.
    - Extended-length local paths remain valid; extended UNC paths are shares.
    - Never relocate, delete or overwrite an existing store automatically.
    """
    if (platform_name or os.name) != "nt":
        return
    drive = PureWindowsPath(str(directory)).drive.replace("\\", "/")
    network = (drive[4:].lower().startswith("unc/") if drive.startswith("//?/")
               else drive.startswith("//") and not drive.startswith("//./"))
    if network:
        raise LaunchError(
            "The private workflow store is on a network share. Recovery requires a local disk. "
            'Set DA_WORKFLOW_STORE to a local folder, for example with '
            'set "DA_WORKFLOW_STORE=%LOCALAPPDATA%\\DAFlowchartStudio\\store", then relaunch. '
            "The app and source projects may remain on the share. Existing saved work has not been moved or changed."
        )


def local_json(url: str, *, payload: dict | None = None, timeout: float = 2) -> dict:
    """Read one bounded JSON response from the local application.

    - Sends POST only when an explicit request body is supplied.
    - Limits response size so a foreign service cannot produce unlimited output.
    - Leaves HTTP status errors available for useful explanations.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data = data, headers = {"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout = timeout) as response:
        content = response.read(65_537)
    if len(content) > 65_536:
        raise LaunchError("The local server returned an unexpectedly large response.")
    decoded = json.loads(content)
    if not isinstance(decoded, dict):
        raise LaunchError("The local server did not return application status.")
    return decoded


def check_identity(health: dict) -> None:
    """Refuse to control another application or another copy of this project.

    - A listening port alone does not establish that this application is running.
    - Project identity avoids reopening a stale copy with a different private store.
    - The instance identifier is passed back for an explicit shutdown request.
    """
    if health.get("app_id") != APP_ID:
        raise LaunchError("This port is used by another application. Choose another port with --port.")
    server_project = health.get("project_root")
    if not server_project or Path(server_project).resolve() != PROJECT_DIR:
        raise LaunchError("This port is used by another copy of DA Document Generator. Stop that copy or choose --port.")
    if not health.get("instance_id"):
        raise LaunchError("The running server is an older version. Stop it once, then reopen this launcher.")
    expected_store = Path(os.environ.get("DA_WORKFLOW_STORE", PROJECT_DIR / "backend" / ".workflow_store")).expanduser().resolve()
    server_store = health.get("store_root")
    if not server_store or Path(server_store).resolve() != expected_store:
        raise LaunchError("The running app uses a different saved-work directory. Use its original settings or choose --port.")


def existing_server(base_url: str, port: int) -> dict | None:
    """Distinguish a stopped backend from an occupied or incompatible port.

    - A closed port is a normal first launch.
    - A server with an invalid health response is never silently reused.
    - Socket checks are limited to loopback and do not scan other ports or hosts.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout = 1):
            pass
    except PermissionError as error:
        raise LaunchError("Local network access was denied. Allow this application to reach its loopback server, then retry.") from error
    except (ConnectionRefusedError, TimeoutError, OSError):
        return None
    try:
        health = local_json(base_url + "/api/health")
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise LaunchError(
            "The port is occupied, but the updated application is not ready. "
            "If the old app is still running, stop its command window once and try again."
        ) from error
    check_identity(health)
    return health


def stop_server(base_url: str, health: dict) -> None:
    """Ask the server to stop only after it confirms that its work is saved.

    - Targets the exact instance returned by the health check.
    - Lets the backend reject shutdown when jobs are queued or running.
    - Never force-kills an active analysis or language-model request.
    """
    if not health.get("shutdown_available"):
        raise LaunchError("This server was started manually. Stop it in its original command window.")
    try:
        local_json(base_url + "/api/shutdown", payload = {"instance_id": health["instance_id"]})
    except HTTPError as error:
        try:
            message = json.loads(error.read(65_536)).get("detail", "The backend could not stop safely.")
        except (ValueError, AttributeError):
            message = "The backend could not stop safely. Check for active jobs in the app."
        finally:
            error.close()
        raise LaunchError(str(message)) from error
    print("Shutdown requested. Saved drafts and finished flowcharts remain available next time.")


def launch_server(port: int, base_url: str) -> dict:
    """Start a detached server and wait for a verified readiness response.

    - Uses the exact Python environment selected by the platform launcher.
    - Writes logs beneath the private workflow store, never the public frontend.
    - Detaches from the terminal on both macOS/Linux and Windows.
    - Terminates only the newly created process if startup fails or times out.
    - Does not install packages, download models, or send source files anywhere.
    """
    required = ("fastapi", "uvicorn", "pydantic", "sqlglot", "defusedxml")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise LaunchError(
            "Application packages are missing: " + ", ".join(missing) + ".\n"
            f'Install them using this Python environment: "{sys.executable}" -m pip install -r "{PROJECT_DIR / "requirements.txt"}"'
        )

    private_dir = Path(os.environ.get("DA_WORKFLOW_STORE", PROJECT_DIR / "backend" / ".workflow_store")).expanduser().resolve()
    require_local_store(private_dir)
    private_dir.mkdir(parents = True, exist_ok = True)
    log_path = private_dir / "server.log"
    if log_path.is_symlink():
        raise LaunchError("The server log must be a regular file inside the private store.")
    # Keep recent startup history without allowing one log to grow forever.
    # - Logs over 5 MB are preserved as server.previous.log before a new startup.
    # - Logs contain application events and errors, not source snapshots.
    if log_path.exists() and log_path.stat().st_size > 5_000_000:
        log_path.replace(private_dir / "server.previous.log")
    environment = dict(os.environ)
    environment.update(
        DA_WORKFLOW_STORE = str(private_dir),
        DA_WORKFLOW_MANAGED = "1",
        PYTHONUNBUFFERED = "1",
        PYTHONDONTWRITEBYTECODE = "1",
        PYTHONUTF8 = "1",
        PYTHONIOENCODING = "utf-8:backslashreplace",
    )
    process_options = detached_process_options()
    with log_path.open("ab") as log_file:
        try:
            log_path.chmod(0o600)
        except OSError:
            pass
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--timeout-graceful-shutdown", "10"],
            cwd = PROJECT_DIR,
            env = environment,
            stdin = subprocess.DEVNULL,
            stdout = log_file,
            stderr = subprocess.STDOUT,
            close_fds = True,
            **process_options,
        )
    deadline = time.monotonic() + 40
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LaunchError(f"The backend could not start. Read its startup log: {log_path}")
            try:
                health = local_json(base_url + "/api/health", timeout = 1)
                check_identity(health)
                if health.get("status") == "ok":
                    if health.get("pid") != process.pid:
                        raise LaunchError("Another launcher started the app at the same time. Reopen this launcher to use it.")
                    return health
            except (URLError, OSError, ValueError):
                pass
            time.sleep(0.2)
        raise LaunchError(f"The backend did not become ready. Read its startup log: {log_path}")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout = 5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout = 5)
        raise


def main(arguments: list[str] | None = None) -> int:
    """Provide the same behavior from a double-click or command line.

    - Default: start if needed, then open the app in the user's browser.
    - ``--no-browser``: start without opening another tab.
    - ``--status``: report whether the correct backend is available.
    - ``--stop``: stop an idle managed backend while retaining all saved work.
    """
    configure_console()
    parser = argparse.ArgumentParser(description = "Open DA Document Generator on this computer.")
    parser.add_argument("--port", type = int, default = 8000)
    parser.add_argument("--no-browser", action = "store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action = "store_true")
    actions.add_argument("--stop", action = "store_true")
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        health = existing_server(base_url, args.port)
        if args.status:
            print("Running at " + base_url if health else "The local backend is stopped.")
            return 0
        if args.stop:
            if health:
                stop_server(base_url, health)
            else:
                print("The local backend is already stopped.")
            return 0
        if health is None:
            print("Starting DA Document Generator...", flush = True)
            health = launch_server(args.port, base_url)
        else:
            require_local_store(health["store_root"])
        print(f"Ready: {base_url}")
        print("You can close this window. The backend keeps running and saves work independently.")
        print("Use Settings > Stop server in the app, or run this launcher with --stop, when finished.")
        if not args.no_browser:
            session_id = uuid4()
            # - Open the document directly so a framework redirect cannot discard
            #   the launcher session query string before the browser reads it.
            webbrowser.open(f"{base_url}/frontend/index.html?session={session_id}")
        return 0
    except (LaunchError, OSError, ValueError) as error:
        print(str(error), file = sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
