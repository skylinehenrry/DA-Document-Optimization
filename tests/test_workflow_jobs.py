"""
Exercise durable application behavior with real local storage and offline sources.
- Model packages are blocked so failures cannot accidentally trigger paid work.
- Simulated restarts cover both uncommitted work and the commit/status-update gap.
- Parallel app instances prove a second server cannot interrupt a live worker.
- API checks cover duplicate delivery, downloads, stale revisions and shutdown.
"""

import asyncio
import atexit
from contextlib import ExitStack
import importlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.graph_edits import EditRequest
from backend.workflow_jobs import JobRepository, JobRequest, WorkerLock, WorkflowJobs
from backend.workflow_service import AnalysisRequest, GenerateRequest, WorkflowService
from backend.workflow_store import WorkflowStore


_bootstrap = tempfile.TemporaryDirectory()
atexit.register(_bootstrap.cleanup)
with patch.dict(os.environ, {"DA_WORKFLOW_STORE": _bootstrap.name}):
    api = importlib.import_module("backend.app")


class JobFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.sentinel = self.root / "source-must-not-run"
        (self.project / "main.py").write_text(
            'from pathlib import Path\n'
            'content = Path("input.csv").read_text()\n'
            'Path("output.csv").write_text(content)\n'
            f'Path({str(self.sentinel)!r}).write_text("must not execute")\n', encoding = "utf-8",
        )
        self.output = self.root / "documents"
        self.service = WorkflowService(WorkflowStore(self.root / "store"))
        self.repository = JobRepository(self.service.store)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.provider = self.stack.enter_context(patch("backend.graph_enrichment.create_provider",
                                                      side_effect = AssertionError("No live model calls in app tests")))

    def payload(self):
        return {"script_folder": str(self.project), "da_document_folder": str(self.output),
                "working_directory": str(self.project), "model": "Ollama"}

    def request(self, kind = "analyze", draft_id = None, payload = None):
        return JobRequest(kind = kind, draft_id = draft_id, payload = self.payload() if payload is None else payload,
                          request_id = uuid4())

    def open_client(self, service = None, **options):
        application = api.create_app(service or self.service, **options)
        client = self.stack.enter_context(TestClient(application, base_url = "http://127.0.0.1"))
        return client

    def wait_job(self, client, job_id, state = None):
        deadline = time.monotonic() + 8
        result = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["state"] == state or (state is None and result["state"] in {"succeeded", "failed", "interrupted"}):
                return result
            time.sleep(0.015)
        self.fail(f"Job did not reach {state or 'a terminal state'}: {result}")

    def submit(self, client, request):
        response = client.post("/api/jobs", json = request.model_dump(mode = "json"))
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()


class WorkflowJobAPITests(JobFixture, unittest.TestCase):
    def test_job_history_is_filtered_by_launcher_session(self):
        client = self.open_client()
        first_session = uuid4()
        second_session = uuid4()
        first = self.request().model_copy(update = {"session_id": first_session})
        second = self.request().model_copy(update = {"session_id": second_session})
        first_job = self.submit(client, first)
        second_job = self.submit(client, second)

        first_rows = client.get(f"/api/jobs?summary=true&session_id={first_session}").json()
        second_rows = client.get(f"/api/jobs?summary=true&session_id={second_session}").json()
        self.assertEqual([row["id"] for row in first_rows], [first_job["id"]])
        self.assertEqual([row["id"] for row in second_rows], [second_job["id"]])

    def test_project_name_is_optional_trimmed_and_does_not_rename_saved_drafts(self):
        client = self.open_client()
        previous = None
        for supplied, expected in (({}, self.project.name), ({"title": None}, self.project.name),
                                   ({"title": "  \t\n "}, self.project.name),
                                   ({"title": "  Revenue Operations  "}, "Revenue Operations")):
            with self.subTest(title = supplied):
                payload = {**self.payload(), **supplied}
                request = self.request(payload = payload)
                completed = self.wait_job(client, self.submit(client, request)["id"])
                self.assertEqual(completed["state"], "succeeded", completed)
                result = completed["result"]
                self.assertEqual(result["title"], expected)
                self.assertEqual(result["graph"]["title"], expected)
                if previous is not None:
                    retained = client.get(f"/api/drafts/{previous['draft_id']}").json()
                    self.assertEqual(retained["graph"]["title"], previous["title"])
                previous = result
        generated = self.wait_job(client, self.submit(client, self.request("generate", previous["draft_id"],
                                                                           {"expected_revision": 1}))["id"])
        self.assertEqual(generated["state"], "succeeded", generated)
        download_url = generated["result"]["outputs"]["flowchart_download"]
        self.assertIn("workflow_flowchart.html?download=1", download_url)
        download = client.get(download_url)
        self.assertEqual(download.status_code, 200)
        self.assertIn('filename="Revenue Operations.html"', download.headers["content-disposition"])

    def test_duplicate_delivery_is_one_job_and_health_remains_responsive(self):
        started, release = threading.Event(), threading.Event()
        original = self.service.analyze

        async def delayed(*args, **kwargs):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original(*args, **kwargs)

        self.stack.enter_context(patch.object(self.service, "analyze", delayed))
        client = self.open_client()
        request = self.request()
        job = self.submit(client, request)
        self.assertTrue(started.wait(2))
        for _ in range(3):
            start = time.monotonic()
            health = client.get("/api/health")
            self.assertLess(time.monotonic() - start, 1)
            self.assertEqual(health.json()["app_id"], "da-workflow")
            self.assertEqual(health.json()["current_job_id"], job["id"])
        duplicate = self.submit(client, request)
        self.assertEqual(duplicate["id"], job["id"])
        changed = request.model_dump(mode = "json")
        changed["payload"]["title"] = "This is a different request"
        self.assertEqual(client.post("/api/jobs", json = changed).status_code, 409)
        self.assertEqual(len(client.get("/api/jobs").json()), 1)
        # No request is held open while the computation proceeds. Re-reading
        # saved jobs is also all a newly opened browser needs to recover progress.
        release.set()
        completed = self.wait_job(client, job["id"])
        self.assertEqual(completed["state"], "succeeded", completed)
        compact = client.get("/api/jobs?summary=true").json()[0]
        self.assertNotIn("graph", compact["result"])
        self.assertNotIn("review", compact["result"])
        self.assertFalse(compact["result_complete"])
        self.assertEqual(compact["result"]["outputs"], completed["result"]["outputs"])
        self.assertLessEqual(len(compact["logs"]), 8)
        self.assertIn("graph", client.get(f"/api/jobs/{job['id']}").json()["result"])
        self.assertEqual(len(client.get("/api/drafts").json()), 1)
        self.assertFalse(self.sentinel.exists())
        self.provider.assert_not_called()

    def test_jobs_are_serialized_and_conflicting_revisions_fail_visibly(self):
        client = self.open_client()
        analysis = self.wait_job(client, self.submit(client, self.request())["id"])
        draft = analysis["result"]
        node_id = draft["graph"]["nodes"][0]["id"]
        payload = {"expected_revision": 1, "operations": [{"op": "update_node", "id": node_id, "label": "Reviewed"}]}
        first = self.submit(client, self.request("edit", draft["draft_id"], payload))
        second = self.submit(client, self.request("edit", draft["draft_id"], payload))
        self.assertEqual(self.wait_job(client, first["id"])["state"], "succeeded")
        failed = self.wait_job(client, second["id"])
        self.assertEqual(failed["state"], "failed", failed)
        self.assertEqual(failed["error"]["code"], "revision_conflict")
        self.assertEqual(failed["error"]["current_revision"], 2)
        self.assertEqual(len(self.service.store.history(draft["draft_id"])), 2)

    def test_an_empty_project_has_an_actionable_error_without_a_blank_draft(self):
        (self.project / "main.py").unlink()
        client = self.open_client()
        result = self.wait_job(client, self.submit(client, self.request())["id"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("No supported source files", result["error"]["message"])
        self.assertEqual(client.get("/api/drafts").json(), [])

    def test_uncommitted_job_is_interrupted_after_restart_and_only_runs_on_explicit_retry(self):
        original = self.repository.enqueue(self.request())
        self.repository.claim("process-that-exited")
        client = self.open_client()
        recovered = self.wait_job(client, original["id"])
        self.assertEqual(recovered["state"], "interrupted", recovered)
        self.assertIsNone(recovered["result"])
        self.assertEqual(self.service.store.list_drafts(), [])
        token = str(uuid4())
        retry = client.post(f"/api/jobs/{original['id']}/retry", json = {"request_id": token})
        self.assertEqual(retry.status_code, 202, retry.text)
        duplicate = client.post(f"/api/jobs/{original['id']}/retry", json = {"request_id": token})
        self.assertEqual(duplicate.json()["id"], retry.json()["id"])
        done = self.wait_job(client, retry.json()["id"])
        self.assertEqual(done["state"], "succeeded", done)
        again = client.post(f"/api/jobs/{original['id']}/retry", json = {"request_id": str(uuid4())})
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(len(self.service.store.list_drafts()), 1)

    def test_analysis_commit_before_job_status_is_recovered_without_reanalysis(self):
        request = self.request()
        job = self.repository.enqueue(request)
        self.repository.claim("process-that-exited")
        graph = asyncio.run(self.service.analyze(AnalysisRequest.model_validate(request.payload), operation_id = job["id"]))
        self.stack.enter_context(patch.object(self.service, "analyze", side_effect = AssertionError("Committed analysis must not be repeated")))
        client = self.open_client()
        recovered = self.wait_job(client, job["id"])
        self.assertEqual(recovered["state"], "succeeded", recovered)
        self.assertEqual(recovered["draft_id"], graph.id)
        self.assertEqual(recovered["result"]["revision"], 1)
        self.assertEqual(len(self.service.store.list_drafts()), 1)
        self.assertEqual(client.get(recovered["result"]["outputs"]["draft_diagram"]).status_code, 200)

    def test_edit_commit_before_job_status_is_recovered_without_a_second_revision(self):
        graph = asyncio.run(self.service.analyze(AnalysisRequest.model_validate(self.payload())))
        payload = {"expected_revision": 1, "operations": [{"op": "update_node", "id": graph.nodes[0].id, "label": "Saved once"}]}
        job = self.repository.enqueue(self.request("edit", graph.id, payload))
        self.repository.claim("process-that-exited")
        self.service.edit(graph.id, EditRequest.model_validate(payload), operation_id = job["id"])
        self.stack.enter_context(patch.object(self.service, "edit", side_effect = AssertionError("Committed edit must not be repeated")))
        client = self.open_client()
        recovered = self.wait_job(client, job["id"])
        self.assertEqual(recovered["state"], "succeeded", recovered)
        self.assertEqual(recovered["result"]["revision"], 2)
        self.assertEqual(len(self.service.store.history(graph.id)), 2)

    def test_generation_commit_before_job_status_preserves_original_download_after_restart(self):
        graph = asyncio.run(self.service.analyze(AnalysisRequest.model_validate(self.payload())))
        job = self.repository.enqueue(self.request("generate", graph.id, {"expected_revision": 1}))
        self.repository.claim("process-that-exited")
        first = asyncio.run(self.service.generate(graph.id, GenerateRequest(expected_revision = 1), operation_id = job["id"]))
        # A direct client can produce a second report before a restart. Recovery
        # must still point at the exact generation committed by this job.
        other = asyncio.run(self.service.generate(graph.id, GenerateRequest(expected_revision = 1)))
        self.assertNotEqual(first["generation_id"], other["generation_id"])
        self.stack.enter_context(patch.object(self.service, "generate", side_effect = AssertionError("Committed generation must not repeat")))
        client = self.open_client()
        recovered = self.wait_job(client, job["id"])
        self.assertEqual(recovered["state"], "succeeded", recovered)
        self.assertEqual(recovered["result"]["generation"]["generation_id"], first["generation_id"])
        downloaded = client.get(recovered["result"]["outputs"]["flowchart_download"])
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertIn("text/html", downloaded.headers["content-type"])
        missing = self.service.artifact_path(graph.id, first["generation_id"], "workflow_flowchart.html")
        missing.unlink()
        unavailable = client.get(recovered["result"]["outputs"]["flowchart_download"])
        self.assertEqual(unavailable.status_code, 404)
        self.assertIn("reviewed graph is still saved", unavailable.json()["detail"])
        self.assertEqual(client.get(f"/api/drafts/{graph.id}").status_code, 200)

    def test_second_server_does_not_interrupt_the_active_worker(self):
        started, release = threading.Event(), threading.Event()
        original = self.service.analyze

        async def delayed(*args, **kwargs):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original(*args, **kwargs)

        self.stack.enter_context(patch.object(self.service, "analyze", delayed))
        first = self.open_client()
        job = self.submit(first, self.request())
        self.assertTrue(started.wait(2))
        second = self.open_client(WorkflowService(WorkflowStore(self.root / "store")))
        self.assertEqual(second.get("/api/health").json()["worker_state"], "standby")
        self.assertEqual(second.get(f"/api/jobs/{job['id']}").json()["state"], "running")
        time.sleep(0.1)
        self.assertEqual(second.get(f"/api/jobs/{job['id']}").json()["state"], "running")
        release.set()
        result = self.wait_job(second, job["id"])
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(len(self.service.store.list_drafts()), 1)

    def test_shutdown_verifies_instance_and_refuses_while_work_is_active(self):
        stop_calls = []
        started, release = threading.Event(), threading.Event()
        original = self.service.analyze

        async def delayed(*args, **kwargs):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original(*args, **kwargs)

        self.stack.enter_context(patch.object(self.service, "analyze", delayed))
        client = self.open_client(shutdown_callback = lambda: stop_calls.append(True))
        instance = client.get("/api/health").json()["instance_id"]
        self.assertEqual(client.post("/api/shutdown", json = {"instance_id": "stale-instance"}).status_code, 409)
        job = self.submit(client, self.request())
        self.assertTrue(started.wait(2))
        self.assertEqual(client.post("/api/shutdown", json = {"instance_id": instance}).status_code, 409)
        self.assertFalse(stop_calls)
        release.set()
        self.assertEqual(self.wait_job(client, job["id"])["state"], "succeeded")
        stopped = client.post("/api/shutdown", json = {"instance_id": instance})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(client.post("/api/jobs", json = self.request().model_dump(mode = "json")).status_code, 503)
        time.sleep(0.3)
        self.assertEqual(stop_calls, [True])

    def test_invalid_job_contract_and_cross_origin_actions_are_rejected_before_queueing(self):
        client = self.open_client()
        payload = self.request().model_dump(mode = "json")
        payload["request_id"] = "not-a-request-uuid"
        self.assertEqual(client.post("/api/jobs", json = payload).status_code, 422)
        payload = self.request().model_dump(mode = "json")
        payload["payload"]["max_concurrency"] = 0
        self.assertEqual(client.post("/api/jobs", json = payload).status_code, 422)
        payload = self.request().model_dump(mode = "json")
        self.assertEqual(client.post("/api/jobs", headers = {"origin": "https://other.example"}, json = payload).status_code, 403)
        self.assertEqual(client.get("/api/jobs").json(), [])

    def test_temporary_shutdown_storage_error_does_not_disable_future_jobs(self):
        client = self.open_client(shutdown_callback = lambda: None)
        instance = client.get("/api/health").json()["instance_id"]
        with patch.object(client.app.state.workflow_jobs.repository, "busy", side_effect = sqlite3.OperationalError("Temporary storage failure")):
            response = client.post("/api/shutdown", json = {"instance_id": instance})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("left running", response.json()["detail"])
        self.assertEqual(client.get("/api/health").json()["status"], "ok")
        job = self.submit(client, self.request())
        self.assertEqual(self.wait_job(client, job["id"])["state"], "succeeded")


class WorkflowJobShutdownTests(JobFixture, unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_shutdown_check_restores_job_acceptance(self):
        application = api.create_app(self.service, shutdown_callback = lambda: None)
        manager = WorkflowJobs(self.service, application.state.instance_id)
        application.state.workflow_jobs = manager
        endpoint = next(route.endpoint for route in application.routes if getattr(route, "path", None) == "/api/shutdown")
        started, release = threading.Event(), threading.Event()

        def delayed_check():
            started.set()
            release.wait(timeout = 3)
            return False

        with patch.object(manager.repository, "busy", delayed_check):
            request = asyncio.create_task(endpoint(api.ShutdownRequest(instance_id = application.state.instance_id)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(manager.accepting)
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertTrue(manager.accepting)
                self.assertFalse(application.state.stopping)
            finally:
                release.set()

    async def test_graceful_shutdown_marks_running_work_interrupted_and_preserves_queued_work(self):
        started = asyncio.Event()

        async def unfinished(*args, **kwargs):
            started.set()
            await asyncio.Future()

        first = self.repository.enqueue(self.request())
        second = self.repository.enqueue(self.request())
        with patch.object(self.service, "analyze", unfinished):
            worker = WorkflowJobs(self.service, "worker-first", poll_seconds = 0.01)
            await worker.start()
            await asyncio.wait_for(started.wait(), timeout = 2)
            await worker.stop()
        self.assertEqual(self.repository.get(first["id"])["state"], "interrupted")
        self.assertEqual(self.repository.get(second["id"])["state"], "queued")
        successor = WorkflowJobs(self.service, "worker-after-restart", poll_seconds = 0.01)
        await successor.start()
        try:
            deadline = time.monotonic() + 5
            while self.repository.get(second["id"])["state"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(self.repository.get(second["id"])["state"], "succeeded")
            self.assertEqual(self.repository.get(first["id"])["state"], "interrupted")
            self.assertEqual(len(self.service.store.list_drafts()), 1)
        finally:
            await successor.stop()

    async def test_shutdown_waits_for_an_edit_commit_then_recovers_success(self):
        graph = await self.service.analyze(AnalysisRequest.model_validate(self.payload()))
        started, release = threading.Event(), threading.Event()
        original = self.service.edit

        def delayed_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            started.set()
            if not release.wait(timeout = 3):
                raise TimeoutError("Test failed to release a committed edit")
            return result

        payload = {"expected_revision": 1, "operations": [{"op": "update_node", "id": graph.nodes[0].id, "label": "Saved before stop"}]}
        job = self.repository.enqueue(self.request("edit", graph.id, payload))
        with patch.object(self.service, "edit", delayed_edit):
            worker = WorkflowJobs(self.service, "worker-stop-mid-edit", poll_seconds = 0.01)
            await worker.start()
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                stopping = asyncio.create_task(worker.stop())
                await asyncio.sleep(0.02)
                other_lock = WorkerLock(self.service.store.directory)
                self.assertFalse(other_lock.acquire(), "The worker must retain ownership until its committing thread finishes")
                release.set()
                await asyncio.wait_for(stopping, 3)
            finally:
                release.set()
                await worker.stop()
        saved = self.repository.get(job["id"])
        self.assertEqual(saved["state"], "succeeded", saved)
        self.assertEqual(saved["result"]["revision"], 2)
        self.assertEqual(len(self.service.store.history(graph.id)), 2)


if __name__ == "__main__":
    unittest.main()
