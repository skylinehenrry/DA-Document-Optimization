"""Recovery checks after an actual worker process is force-closed.

- Use temporary sources, reports and databases; never touch the user's library.
- Kill only the test child at a recorded pre-commit or post-commit boundary.
- Verify that the OS releases worker ownership and SQLite recovers durable work.
- Do not bind a network port, execute source programs, or contact a model.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from backend.workflow_jobs import WorkflowJobs
from backend.workflow_service import WorkflowService
from backend.workflow_store import WorkflowStore


CHILD = r'''
import asyncio, json, sys
from pathlib import Path
from uuid import uuid4
from backend.workflow_jobs import JobRequest, WorkflowJobs
from backend.workflow_service import AnalysisRequest, WorkflowService
from backend.workflow_store import WorkflowStore

root = Path(sys.argv[1])
scenario = sys.argv[2]

class CheckpointService(WorkflowService):
    async def analyze(self, request, logger=None, *, operation_id=None):
        if operation_id is not None and scenario == 'before_commit':
            (root / 'checkpoint.json').write_text(json.dumps({'job_id': operation_id}))
            await asyncio.Event().wait()
        graph = await super().analyze(request, logger, operation_id=operation_id)
        if operation_id is not None and scenario == 'after_draft_commit':
            (root / 'checkpoint.json').write_text(json.dumps({'job_id': operation_id, 'draft_id': graph.id}))
            await asyncio.Event().wait()
        return graph

    async def generate(self, draft_id, request, logger=None, *, operation_id=None, **kwargs):
        manifest = await super().generate(draft_id, request, logger, operation_id=operation_id, **kwargs)
        (root / 'checkpoint.json').write_text(json.dumps({
            'job_id': operation_id, 'draft_id': draft_id, 'generation_id': manifest['generation_id']}))
        await asyncio.Event().wait()

async def main():
    service = CheckpointService(WorkflowStore(root / 'store'))
    manager = WorkflowJobs(service, 'child_' + uuid4().hex)
    payload = {'script_folder': str(root / 'sources'), 'da_document_folder': str(root / 'reports'),
               'working_directory': str(root / 'sources')}
    if scenario == 'after_generation_commit':
        graph = await service.analyze(AnalysisRequest(**payload))
        request = JobRequest(kind='generate', draft_id=graph.id, request_id=uuid4(),
                             payload={'expected_revision': 1})
    else:
        request = JobRequest(kind='analyze', request_id=uuid4(), payload=payload)
    manager.repository.enqueue(request)
    await manager.start()
    await asyncio.Event().wait()

asyncio.run(main())
'''


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "sources"
        source.mkdir()
        self.sentinel = self.root / "source-was-executed.txt"
        (source / "worker.py").write_text(
            f"from pathlib import Path\nPath({str(self.sentinel)!r}).write_text('must never run')\n",
            encoding = "utf-8",
        )

    def force_close(self, scenario):
        """Stop the owned child only after its checkpoint has reached disk.

        - A bounded wait reports startup failures instead of hanging the suite.
        - Killing bypasses all graceful-shutdown callbacks and finally blocks.
        - Waiting for exit ensures the subsequent worker tests real lock release.
        """
        with (self.root / "child.log").open("w") as output:
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", CHILD, str(self.root), scenario],
                cwd = Path(__file__).resolve().parents[1],
                env = {**os.environ, "DA_WORKFLOW_STORE": str(self.root / "store")},
                stdin = subprocess.DEVNULL, stdout = output, stderr = subprocess.STDOUT,
            )
            try:
                checkpoint = self.root / "checkpoint.json"
                deadline = time.monotonic() + 12
                while not checkpoint.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not checkpoint.exists():
                    self.fail("Child did not reach its checkpoint: " + (self.root / "child.log").read_text()[-4000:])
                # The write completes before the checkpoint is consumed; a
                # second read can otherwise race a partially flushed tiny file.
                state = None
                while state is None and time.monotonic() < deadline:
                    try:
                        state = json.loads(checkpoint.read_text())
                    except ValueError:
                        time.sleep(0.01)
                self.assertIsNotNone(state)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout = 5)
        self.assertFalse(self.sentinel.exists())
        return state

    async def recover(self, job_id):
        manager = WorkflowJobs(WorkflowService(WorkflowStore(self.root / "store")), "recovery_" + uuid4().hex)
        await manager.start()
        try:
            for _ in range(200):
                job = manager.repository.get(job_id)
                if job["state"] in {"succeeded", "failed", "interrupted"}:
                    return job
                await asyncio.sleep(0.01)
            self.fail("A force-closed worker was not recovered.")
        finally:
            await manager.stop()

    def test_force_closed_analysis_is_interrupted_without_automatic_retry(self):
        checkpoint = self.force_close("before_commit")
        result = asyncio.run(self.recover(checkpoint["job_id"]))
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["error"]["code"], "backend_interrupted")
        self.assertEqual(WorkflowStore(self.root / "store").list_drafts(), [])

    def test_force_close_after_draft_commit_recovers_saved_result_once(self):
        checkpoint = self.force_close("after_draft_commit")
        result = asyncio.run(self.recover(checkpoint["job_id"]))
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["draft_id"], checkpoint["draft_id"])
        self.assertEqual(result["result"]["revision"], 1)
        store = WorkflowStore(self.root / "store")
        self.assertEqual(len(store.list_drafts()), 1)
        self.assertEqual(len(store.history(checkpoint["draft_id"])), 1)

    def test_force_close_after_generation_commit_keeps_files_and_exact_download(self):
        checkpoint = self.force_close("after_generation_commit")
        result = asyncio.run(self.recover(checkpoint["job_id"]))
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["generation"]["generation_id"], checkpoint["generation_id"])
        self.assertIn(checkpoint["generation_id"], result["result"]["outputs"]["flowchart_download"])
        service = WorkflowService(WorkflowStore(self.root / "store"))
        path = service.artifact_path(checkpoint["draft_id"], checkpoint["generation_id"], "workflow_flowchart.html")
        self.assertIn('id="graph-data"', path.read_text())
        with service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM generations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
