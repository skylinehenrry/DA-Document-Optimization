"""
Check project naming and filesystem behavior on macOS and Windows.
- Portable tests exercise Windows path rules, legacy console encodings and CRLF.
- Native Windows tests run the actual batch wrapper with a local test environment.
- All source folders, stores and launcher fixtures live in temporary directories.
- These tests do not execute analyzed source programs, contact providers or open UI.
"""

import asyncio
import io
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

from backend.main import cli, parser
from backend.project_identity import flowchart_attachment, flowchart_filename, project_title
from backend.static_analysis import analyze_project
from backend.workflow_service import _write_directory
from backend.workflow_store import WorkflowStore


ROOT = Path(__file__).resolve().parents[1]


class PlatformCompatibilityTests(unittest.TestCase):
    def test_windows_folder_picker_uses_explicit_utf8_without_interpolating_paths(self):
        from backend.app import select_folder
        chosen = "C:\\分析プロジェクト\\Monthly & Revenue"
        with patch("backend.app.platform.system", return_value = "Windows"), patch("backend.app.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout = chosen + "\r\n", stderr = "")
            self.assertEqual(select_folder(Path(chosen)), chosen)
        command = run.call_args.args[0]
        self.assertNotIn(chosen, command[-1])
        self.assertIn("[Console]::OutputEncoding", command[-1])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["env"]["DA_FOLDER_PICKER_INITIAL"], chosen)

    def test_project_download_names_are_safe_on_windows_and_in_http_headers(self):
        for title in ('CON', 'con.report', 'LPT1', 'COM\u00b9', '../../AUX', 'Bad <>:"/\\|?* name. ',
                      'Report\r\nX-Injected: value', '\U0001f4c8' * 1000):
            with self.subTest(title = title[:50]):
                filename = flowchart_filename(title)
                self.assertTrue(filename.endswith(".html"))
                self.assertLessEqual(len(filename.encode("utf-8")), 200)
                self.assertNotRegex(filename, r'[\x00-\x1f\x7f<>:"/\\|?*]')
                self.assertNotIn(filename.split(".", 1)[0].upper(), {"CON", "PRN", "AUX", "NUL", "LPT1", "COM\u00b9"})
                header = flowchart_attachment(title)
                header.encode("ascii")
                self.assertNotIn("\r", header)
                self.assertNotIn("\n", header)
        self.assertEqual(flowchart_filename(" Revenue Operations "), "Revenue Operations.html")
        self.assertEqual(flowchart_filename("Revenue.html"), "Revenue.html")
        self.assertIn("filename*=UTF-8''", flowchart_attachment("\u6536\u5165\u5206\u6790"))

    def test_managed_shutdown_runs_the_process_signal_handler_and_lifespan_cleanup(self):
        code = """
import asyncio, signal
from backend.app import ShutdownRequest, create_app

async def main():
    received = asyncio.Event()
    original = signal.signal(signal.SIGTERM, lambda number, frame: received.set())
    try:
        application = create_app()
        async with application.router.lifespan_context(application):
            endpoint = next(route.endpoint for route in application.routes if getattr(route, 'path', None) == '/api/shutdown')
            response = await endpoint(ShutdownRequest(instance_id=application.state.instance_id))
            assert response['status'] == 'stopping'
            await asyncio.wait_for(received.wait(), 3)
        assert application.state.workflow_jobs.lock.file is None
        print('graceful shutdown completed')
    finally:
        signal.signal(signal.SIGTERM, original)

asyncio.run(main())
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd = ROOT,
                                    env = {**os.environ, "DA_WORKFLOW_STORE": directory, "DA_WORKFLOW_MANAGED": "1"},
                                    capture_output = True, text = True, encoding = "utf-8", timeout = 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("graceful shutdown completed", result.stdout)

    def test_folder_name_defaults_cover_windows_posix_and_unc_path_styles(self):
        cases = [
            ("/Users/user/Monthly Revenue/", "Monthly Revenue"),
            (r"C:\Users\Analyst\Monthly Revenue" + "\\", "Monthly Revenue"),
            ("C:/Users/Analyst/Monthly Revenue/", "Monthly Revenue"),
            (r"\\server\Finance Share\Monthly Revenue" + "\\", "Monthly Revenue"),
            (r"\\server\Finance Share" + "\\", "Finance Share"),
            (PureWindowsPath("C:/"), "C drive"),
        ]
        for source, expected in cases:
            with self.subTest(source = source):
                self.assertEqual(project_title(source), expected)
                self.assertEqual(project_title(source, "  \t  "), expected)
                self.assertEqual(project_title(source, "  Reviewed Finance  "), "Reviewed Finance")

    def test_direct_analysis_and_cli_delegate_the_name_to_the_selected_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "Monthly Revenue"
            project.mkdir()
            (project / "script.py").write_text("value = 1\n", encoding = "utf-8")
            graph, _ = analyze_project(project)
            self.assertEqual(graph.title, "Monthly Revenue")
            named, _ = analyze_project(project, title = "  Reviewed Revenue  ")
            self.assertEqual(named.title, "Reviewed Revenue")
            args = parser().parse_args(["analyze", str(project), str(Path(directory) / "reports")])
            self.assertIsNone(args.title)

    def test_selected_symlink_folder_name_is_preserved_for_display(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "Physical source"
            project.mkdir()
            (project / "script.py").write_text("value = 1\n", encoding = "utf-8")
            selected = Path(directory) / "Chosen project name"
            try:
                selected.symlink_to(project, target_is_directory = True)
            except OSError as error:
                self.skipTest(f"This OS account cannot create directory symlinks: {error}")
            graph, _ = analyze_project(selected)
            self.assertEqual(graph.title, selected.name)
            self.assertEqual(graph.project_root, project.resolve().as_posix())

    def test_cli_output_stays_valid_utf8_json_in_a_legacy_windows_text_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "Project \u540d\u524d \U0001f31f"
            project.mkdir()
            (project / "script.py").write_text("value = 1\n", encoding = "utf-8")
            store = Path(directory) / "store"
            data, errors = io.BytesIO(), io.BytesIO()
            stdout = io.TextIOWrapper(data, encoding = "cp1252", errors = "strict")
            stderr = io.TextIOWrapper(errors, encoding = "cp1252", errors = "strict")
            try:
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    code = asyncio.run(cli(["--store", str(store), "analyze", str(project), str(Path(directory) / "reports")]))
                stdout.flush()
                response = json.loads(data.getvalue().decode("utf-8"))
                self.assertEqual(code, 0)
                graph = WorkflowStore(store).load(response["draft_id"])
                self.assertEqual(graph.title, project.name)
            finally:
                stdout.close()
                stderr.close()

    def test_artifact_bytes_match_manifest_input_even_with_windows_newline_translation(self):
        real_write = Path.write_text
        real_fsync = os.fsync
        writable_flushes = []

        def windows_text_writer(path, content, *, encoding = None, errors = None, newline = None):
            # - Simulate Windows' default text translation on any test host.
            # - An explicit LF newline option must preserve the bytes hashed by
            #   generation manifests, including non-ASCII labels and summaries.
            if newline is None:
                content = content.replace("\n", "\r\n")
            return real_write(path, content, encoding = encoding, errors = errors, newline = "\n")

        def requires_write_access(descriptor):
            # - A zero-byte write proves the descriptor grants write access,
            #   as Windows needs for FlushFileBuffers/_commit.
            os.write(descriptor, b"")
            writable_flushes.append(descriptor)
            return real_fsync(descriptor)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "published"
            expected = {"workflow_flowchart.html": "<h1>\u540d\u524d</h1>\n<p>Reviewed connections</p>\n",
                        "summaries.json": '{\n  "label": "\U0001f31f"\n}\n'}
            with patch.object(Path, "write_text", windows_text_writer), patch("backend.workflow_service.os.fsync", requires_write_access):
                _write_directory(destination, expected)
            for name, content in expected.items():
                self.assertEqual((destination / name).read_bytes(), content.encode("utf-8"))
            self.assertEqual(len(writable_flushes), len(expected))


@unittest.skipUnless(os.name == "nt", "Requires the actual Windows command processor; not simulated on macOS")
class NativeWindowsLauncherTests(unittest.TestCase):
    def test_batch_wrapper_handles_spaces_punctuation_and_inherited_delayed_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "Project with spaces & ! symbols"
            project.mkdir()
            venv.EnvBuilder(with_pip = False).create(project / ".venv")
            wrapper = project / "Launch DA Document Generator.bat"
            shutil.copyfile(ROOT / wrapper.name, wrapper)
            (project / "launch.py").write_text(
                "import json, os, sys\nfrom pathlib import Path\n"
                "Path(os.environ['DA_TEST_LAUNCH_MARKER']).write_text(json.dumps({"
                "'interpreter': sys.executable, 'script': str(Path(__file__).resolve()), 'arguments': sys.argv[1:]"
                "}), encoding='utf-8')\n", encoding = "utf-8",
            )
            marker = Path(directory) / "launched.json"
            command_processor = os.environ.get("COMSPEC", "cmd.exe")
            # - The normal absolute invocation works from another directory.
            # - The relative invocation inherits delayed expansion but lets the
            #   wrapper protect the exclamation mark in its own expanded path.
            for delayed, command_path, cwd in (("off", str(wrapper), Path(directory)),
                                                ("on", wrapper.name, project)):
                with self.subTest(delayed_expansion = delayed):
                    command = f'"{command_processor}" /d /v:{delayed} /s /c ""{command_path}" --status"'
                    result = subprocess.run(command, cwd = cwd, env = {**os.environ, "DA_TEST_LAUNCH_MARKER": str(marker)},
                                            stdin = subprocess.DEVNULL, capture_output = True, timeout = 30)
                    self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors = "replace"))
                    recorded = json.loads(marker.read_text(encoding = "utf-8"))
                    self.assertEqual(Path(recorded["interpreter"]).parent, project / ".venv" / "Scripts")
                    self.assertEqual(Path(recorded["script"]), project / "launch.py")
                    self.assertEqual(recorded["arguments"], ["--status"])
                    marker.unlink()


if __name__ == "__main__":
    unittest.main()
