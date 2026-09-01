"""Launcher safety checks without starting a real server or opening a browser.

- An occupied port cannot redirect the launcher into another application.
- A stop request must target the exact known managed instance.
- Local shutdown refuses active work instead of falling back to process killing.
- The launcher reports missing dependencies without installing anything itself.
"""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import launch


class LauncherTests(unittest.TestCase):
    def health(self, **overrides):
        return {"app_id": "da-workflow", "project_root": str(launch.PROJECT_DIR),
                "store_root": str(Path(os.environ.get("DA_WORKFLOW_STORE", launch.PROJECT_DIR / "backend" / ".workflow_store")).expanduser().resolve()),
                "instance_id": "the-checked-instance", "shutdown_available": True, **overrides}

    def test_occupied_port_cannot_reopen_another_app_or_another_project(self):
        with self.assertRaisesRegex(launch.LaunchError, "another application"):
            launch.check_identity(self.health(app_id = "unrelated"))
        with self.assertRaisesRegex(launch.LaunchError, "another copy"):
            launch.check_identity(self.health(project_root = str(launch.PROJECT_DIR / "elsewhere")))
        with self.assertRaisesRegex(launch.LaunchError, "older version"):
            launch.check_identity(self.health(instance_id = None))
        with self.assertRaisesRegex(launch.LaunchError, "different saved-work directory"):
            launch.check_identity(self.health(store_root = str(launch.PROJECT_DIR / "different-store")))

    def test_stop_uses_verified_instance_and_never_stops_an_unmanaged_server(self):
        with patch("launch.local_json", return_value = {"status": "stopping"}) as request:
            with patch("sys.stdout", new_callable = io.StringIO):
                launch.stop_server("http://127.0.0.1:8765", self.health())
            request.assert_called_once_with("http://127.0.0.1:8765/api/shutdown",
                                            payload = {"instance_id": "the-checked-instance"})
        with patch("launch.local_json") as request:
            with self.assertRaisesRegex(launch.LaunchError, "manually"):
                launch.stop_server("http://127.0.0.1:8765", self.health(shutdown_available = False))
            request.assert_not_called()

    def test_busy_backend_error_is_preserved_and_no_process_is_killed(self):
        error = HTTPError("http://127.0.0.1:8765/api/shutdown", 409, "Conflict", {},
                          io.BytesIO(json.dumps({"detail": "A job is still running; wait until it finishes."}).encode()))
        with patch("launch.local_json", side_effect = error), patch("launch.subprocess.Popen") as process:
            with self.assertRaisesRegex(launch.LaunchError, "job is still running"):
                launch.stop_server("http://127.0.0.1:8765", self.health())
            process.assert_not_called()

    def test_missing_dependencies_do_not_install_or_launch_anything(self):
        with patch("launch.importlib.util.find_spec", return_value = None), patch("launch.subprocess.Popen") as process:
            with self.assertRaisesRegex(launch.LaunchError, "requirements.txt"):
                launch.launch_server(8765, "http://127.0.0.1:8765")
            process.assert_not_called()

    def test_network_permission_failure_is_not_reported_as_a_stopped_server(self):
        with patch("launch.socket.create_connection", side_effect = PermissionError("denied")):
            with self.assertRaisesRegex(launch.LaunchError, "network access was denied"):
                launch.existing_server("http://127.0.0.1:8765", 8765)

    def test_existing_server_is_reused_without_starting_another_process(self):
        with patch("launch.existing_server", return_value = self.health()), patch("launch.launch_server") as start:
            with patch("launch.webbrowser.open") as browser, patch("sys.stdout", new_callable = io.StringIO):
                self.assertEqual(launch.main(["--port", "8765", "--no-browser"]), 0)
            start.assert_not_called()
            browser.assert_not_called()

    def test_legacy_console_encoding_does_not_hide_unicode_startup_errors(self):
        stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding = "cp1252", errors = "strict")
        stderr = io.TextIOWrapper(stderr_bytes, encoding = "cp1252", errors = "strict")
        try:
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                    patch("launch.existing_server", side_effect = launch.LaunchError("Cannot open project: \u540d\u524d")):
                code = launch.main(["--no-browser"])
            stdout.flush()
            stderr.flush()
            self.assertEqual(code, 1)
            self.assertIn("Cannot open project", stderr_bytes.getvalue().decode("cp1252"))
            self.assertIn(r"\u540d", stderr_bytes.getvalue().decode("cp1252"))
        finally:
            stdout.close()
            stderr.close()

    def test_windows_detachment_and_quoted_paths_use_the_selected_interpreter(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "Project with spaces & ! symbols"
            project.mkdir()
            store = Path(folder) / "Saved work"
            interpreter = str(project / ".venv" / "Scripts" / "python.exe")
            process = SimpleNamespace(pid = 45678, poll = lambda: None, terminate = Mock(), wait = Mock(), kill = Mock())
            health = {"app_id": "da-workflow", "project_root": str(project.resolve()),
                      "store_root": str(store.resolve()), "instance_id": "started-instance", "pid": process.pid, "status": "ok"}
            with patch.dict(os.environ, {"DA_WORKFLOW_STORE": str(store)}), \
                    patch("launch.PROJECT_DIR", project.resolve()), patch("launch.sys.executable", interpreter), \
                    patch("launch.importlib.util.find_spec", return_value = object()), \
                    patch("launch.detached_process_options", return_value = launch.detached_process_options("nt")), \
                    patch("launch.local_json", return_value = health), patch("launch.subprocess.Popen", return_value = process) as start:
                self.assertEqual(launch.launch_server(8765, "http://127.0.0.1:8765"), health)
            arguments, options = start.call_args
            self.assertEqual(arguments[0][0], interpreter)
            self.assertIsInstance(arguments[0], list)
            self.assertEqual(options["cwd"], project.resolve())
            self.assertEqual(options["creationflags"], 0x00000008 | 0x00000200)
            self.assertNotIn("start_new_session", options)
            self.assertFalse(options.get("shell", False))
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.STDOUT)
            self.assertTrue(options["close_fds"])
            self.assertEqual(options["env"]["PYTHONUTF8"], "1")
            self.assertEqual(options["env"]["PYTHONIOENCODING"], "utf-8:backslashreplace")
            self.assertEqual(options["env"]["DA_WORKFLOW_STORE"], str(store.resolve()))
            process.terminate.assert_not_called()

    def test_unc_private_store_is_rejected_without_relocating_saved_work(self):
        for path in (r"\\server\share\app\backend\.workflow_store", r"\\?\UNC\server\share\private-store",
                     "//server/share/private-store"):
            with self.subTest(path = path), self.assertRaisesRegex(launch.LaunchError, "DA_WORKFLOW_STORE"):
                launch.require_local_store(path, "nt")
        for path in (r"C:\Users\User\AppData\Local\DAFlowchartStudio\store", r"\\?\C:\Local store\data"):
            with self.subTest(path = path):
                launch.require_local_store(path, "nt")

    def test_launch_refuses_an_unsafe_store_before_creating_files_or_processes(self):
        with patch("launch.importlib.util.find_spec", return_value = object()), \
                patch("launch.require_local_store", side_effect = launch.LaunchError("Use a local private store")), \
                patch("launch.Path.mkdir") as mkdir, patch("launch.subprocess.Popen") as start:
            with self.assertRaisesRegex(launch.LaunchError, "local private store"):
                launch.launch_server(8765, "http://127.0.0.1:8765")
            mkdir.assert_not_called()
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
