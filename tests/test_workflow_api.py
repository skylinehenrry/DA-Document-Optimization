"""Exercise the local API with real parsers, storage, edits, and rendering.

- Verify draft creation, history, review, export, import, generation, and downloads.
- Confirm stale revisions, malformed inputs, missing artifacts, and unsafe requests
  return explicit errors without corrupting the saved workflow.
- Use temporary project and output folders so tests cannot alter the user's library.
- Replace optional model work with local behavior; no provider or sign-in is used.
"""

import atexit
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from backend.workflow_service import WorkflowService
from backend.workflow_store import WorkflowStore


_bootstrap = tempfile.TemporaryDirectory()
atexit.register(_bootstrap.cleanup)
with patch.dict(os.environ, {"DA_WORKFLOW_STORE": _bootstrap.name}):
    api = importlib.import_module("backend.app")


class WorkflowAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "scripts"
        self.project.mkdir()
        self.source = 'from pathlib import Path\n\ndef clean():\n    text = Path("raw.csv").read_text()\n    Path("clean.csv").write_text(text)\n'
        (self.project / "worker.py").write_text(self.source, encoding = "utf-8")
        (self.project / "main.py").write_text("import worker\nworker.clean()\n", encoding = "utf-8")
        # If source code were executed during analysis, this would leave proof.
        self.sentinel = self.root / "must-not-execute.txt"
        (self.project / "never_execute.py").write_text(f"from pathlib import Path\nPath({str(self.sentinel)!r}).write_text('unsafe')\n", encoding = "utf-8")
        self.output = self.root / "documents"
        self.service = WorkflowService(WorkflowStore(self.root / "store"))
        self.app = api.create_app(self.service)
        deny_provider = patch("backend.graph_enrichment.create_provider", side_effect = AssertionError("Tests must not call live models"))
        self.provider = deny_provider.start()
        self.addCleanup(deny_provider.stop)
        self.client = TestClient(self.app, base_url = "http://127.0.0.1")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def analyze(self):
        response = self.client.post("/api/drafts", json = {"script_folder": str(self.project),
            "da_document_folder": str(self.output), "working_directory": str(self.project)})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(self.sentinel.exists())
        result = response.json()
        if "graph" not in result:
            result = self.client.get(f"/api/drafts/{result['draft_id']}").json()
        return result

    def test_visual_import_and_generate_preserve_edited_topology_and_source_snapshot(self):
        draft = self.analyze()
        self.assertIsNone(draft["outputs"]["flowchart"])
        self.assertEqual(draft["review"]["proposed_edge_ids"], [])
        self.assertEqual(len(draft["graph"]["sources"]), 3)
        response = self.client.get(draft["outputs"]["draft_diagram"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        tree = ET.fromstring(response.text)
        parent_map = {child: parent for parent in tree.iter() for child in parent}
        cell = tree.find(".//mxCell[@edge='1']")
        wrapper = parent_map[cell]
        removed_id = wrapper.get("id")
        self.assertIn(removed_id, {edge["id"] for edge in draft["graph"]["edges"]})
        parent_map[wrapper].remove(wrapper)
        base = f"/api/drafts/{draft['draft_id']}"
        imported = self.client.post(base + "/import", json = {"expected_revision": 1, "xml": ET.tostring(tree, encoding = "unicode")})
        self.assertEqual(imported.status_code, 200, imported.text)
        reviewed = imported.json()
        self.assertEqual(reviewed["revision"], 2)
        self.assertNotIn(removed_id, {edge["id"] for edge in reviewed["graph"]["edges"]})
        (self.project / "worker.py").write_text("this is no longer valid python : : :", encoding = "utf-8")
        generated = self.client.post(base + "/generate", json = {"expected_revision": 2})
        self.assertEqual(generated.status_code, 200, generated.text)
        self.provider.assert_not_called()
        result = generated.json()
        download = self.client.get(result["outputs"]["flowchart_download"])
        self.assertEqual(download.status_code, 200, download.text)
        self.assertIn("attachment", download.headers["content-disposition"])
        html = self.client.get(result["outputs"]["flowchart"])
        self.assertEqual(html.status_code, 200)
        self.assertEqual(download.content, html.content)
        embedded = re.search(r'<script[^>]*id="graph-data"[^>]*>(.*?)</script>', html.text, re.S)
        rendered_graph = json.loads(embedded.group(1))
        self.assertEqual(rendered_graph, reviewed["graph"])
        self.assertEqual(self.service.store.snapshots(draft["draft_id"])["worker.py"], self.source)
        summaries = self.client.get(result["outputs"]["summaries"]).json()
        self.assertEqual(summaries["revision"], 2)
        self.assertEqual(len(summaries["summaries"]), 3)
        self.assertFalse(self.sentinel.exists())

    def test_stale_edits_imports_and_generation_cannot_overwrite_a_new_revision(self):
        draft = self.analyze()
        base = f"/api/drafts/{draft['draft_id']}"
        old_xml = self.client.get(draft["outputs"]["draft_diagram"]).text
        node_id = draft["graph"]["nodes"][0]["id"]
        edit = {"expected_revision": 1, "operations": [{"op": "update_node", "id": node_id, "label": "Reviewed label"}]}
        self.assertEqual(self.client.patch(base, json = edit).status_code, 200)
        self.assertEqual(self.client.patch(base, json = edit).status_code, 409)
        self.assertEqual(self.client.post(base + "/generate", json = {"expected_revision": 1}).status_code, 409)
        stale_import = self.client.post(base + "/import", json = {"expected_revision": 2, "xml": old_xml})
        self.assertEqual(stale_import.status_code, 422)
        self.assertEqual(self.client.get(base).json()["revision"], 2)
        history = self.client.get(base + "/history").json()
        self.assertEqual([item["revision"] for item in history], [1, 2])

    def test_same_project_reanalysis_creates_a_new_draft_and_keeps_old_artifacts(self):
        first = self.analyze()
        base = f"/api/drafts/{first['draft_id']}"
        generated = self.client.post(base + "/generate", json = {"expected_revision": 1}).json()
        first_html = generated["outputs"]["flowchart"]
        second = self.analyze()
        self.assertNotEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(first["graph"]["source_digest"], second["graph"]["source_digest"])
        self.assertEqual({node["id"] for node in first["graph"]["nodes"]}, {node["id"] for node in second["graph"]["nodes"]})
        self.assertIsNone(self.client.get(f"/api/drafts/{second['draft_id']}").json()["outputs"]["flowchart"])
        self.assertEqual(self.client.get(first_html).status_code, 200)

    def test_invalid_batch_is_atomic_and_errors_are_actionable(self):
        draft = self.analyze()
        base = f"/api/drafts/{draft['draft_id']}"
        edge_id = draft["graph"]["edges"][0]["id"]
        response = self.client.patch(base, json = {"expected_revision": 1, "operations": [
            {"op": "remove_edge", "id": edge_id},
            {"op": "add_edge", "edge": {"source": "missing", "target": "also_missing", "kind": "calls"}},
        ]})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Dangling", response.text)
        self.assertEqual(self.client.get(base).json()["graph"], draft["graph"])
        self.assertEqual(self.app.state.active_workflow_jobs, 0)

    def test_analysis_failure_is_visible_and_requires_acknowledgment(self):
        (self.project / "broken.py").write_text("def broken(:", encoding = "utf-8")
        draft = self.analyze()
        self.assertTrue(draft["review"]["has_analysis_errors"])
        base = f"/api/drafts/{draft['draft_id']}"
        self.assertEqual(self.client.post(base + "/generate", json = {"expected_revision": 1}).status_code, 409)
        accepted = self.client.post(base + "/generate", json = {"expected_revision": 1, "acknowledge_incomplete": True})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["generation"]["has_analysis_errors"])
        self.assertIn("broken.py", self.client.get(accepted.json()["outputs"]["flowchart"]).text)

    def test_old_global_routes_are_retired_instead_of_guessing_an_output(self):
        for route, method in (("/api/run", "post"), ("/api/output-status", "post"),
                              ("/api/logs", "get"), ("/api/heartbeat", "post"),
                              ("/api/output/workflow_flowchart.html", "get")):
            response = getattr(self.client, method)(route)
            self.assertEqual(response.status_code, 410, response.text)
            self.assertIn("saved draft", response.json()["detail"])
        self.provider.assert_not_called()

    def test_generation_inherits_the_saved_local_provider_when_not_overridden(self):
        from backend.workflow_service import GenerateRequest, saved_model_options
        settings = {"model": "Ollama", "language": "Japanese", "max_concurrency": 2}
        inherited = saved_model_options(GenerateRequest(expected_revision = 1), settings)
        self.assertEqual((inherited.model, inherited.language, inherited.max_concurrency), ("Ollama", "Japanese", 2))
        explicit = saved_model_options(GenerateRequest(expected_revision = 1, model = "OpenAI", language = "English"), settings)
        self.assertEqual((explicit.model, explicit.language, explicit.max_concurrency), ("OpenAI", "English", 2))

    def test_download_serves_the_verified_bytes_even_if_file_changes_after_read(self):
        draft = self.analyze()
        base = f"/api/drafts/{draft['draft_id']}"
        generated = self.client.post(base + "/generate", json = {"expected_revision": 1}).json()
        original = self.client.get(generated["outputs"]["flowchart_download"]).content
        real_read = Path.read_bytes

        def replaced_after_read(path):
            data = real_read(path)
            if path.name == "workflow_flowchart.html":
                path.write_text("Replaced after checksum input was captured", encoding = "utf-8")
            return data

        with patch.object(Path, "read_bytes", replaced_after_read):
            download = self.client.get(generated["outputs"]["flowchart_download"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, original)
        self.assertEqual(self.client.get(generated["outputs"]["flowchart_download"]).status_code, 422)

    def test_loopback_and_origin_guards_and_private_storage(self):
        frontend = self.client.get("/frontend/index.html")
        self.assertEqual(frontend.headers["x-frame-options"], "DENY")
        self.assertEqual(frontend.headers["content-security-policy"], "frame-ancestors 'none'")
        self.assertEqual(frontend.headers["referrer-policy"], "no-referrer")
        self.assertIn("<title>DA Document Generator</title>", frontend.text)
        for removed in ("Run Log", "Activity", "Appearance", "New analysis",
                        "Reads files. Never runs your scripts.", "Local first. Human reviewed."):
            self.assertNotIn(removed, frontend.text)
        self.assertEqual(self.client.get("/api/drafts", headers = {"host": "untrusted.example"}).status_code, 400)
        rejected = self.client.post("/api/drafts", headers = {"origin": "https://untrusted.example"}, json = {})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.get("/outputs/.workflow_store/workflows.sqlite3").status_code, 404)
        self.assertEqual(self.client.get("/api/drafts/no_such_draft").status_code, 404)
        draft = self.analyze()
        self.assertEqual(self.client.get(f"/api/drafts/{draft['draft_id']}/export/workflows.sqlite3").status_code, 404)

    def test_backend_import_requires_no_provider_packages_and_does_not_change_cwd(self):
        code = """
import importlib.abc, os, sys
class DenyProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('langchain', 'openai', 'ollama', 'azure')):
            raise ImportError('provider package unavailable')
sys.meta_path.insert(0, DenyProviders())
before = os.getcwd()
import backend.app
import backend.main
assert before == os.getcwd()
print('offline import works')
"""
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd = Path(__file__).resolve().parents[1],
                                env = {**os.environ, "DA_WORKFLOW_STORE": str(self.root / "offline-store")}, capture_output = True, text = True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("offline import works", result.stdout)


if __name__ == "__main__":
    unittest.main()
