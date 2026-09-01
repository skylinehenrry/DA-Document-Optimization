"""
Run browser actions as durable jobs owned by the local application.
- Save each request before returning an accepted response to the browser.
- Reusing its request identifier returns the same job; changed input is rejected.
- Keep only the job state needed for safe completion and browser recovery.
- Tag browser jobs with one launcher session so a newly opened command session
  starts with empty progress while a refresh of the same page can recover.
- One operating-system lock permits one worker per store, even when two server
  processes are opened; a second server can still read and submit requests.
- A stopped process releases that lock automatically. Its successor reconciles
  transaction receipts, then marks unfinished work interrupted for explicit retry.
- Never automatically repeat an interrupted model request: the remote provider
  may already have charged for it even when no final result reached this machine.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, model_validator

from .graph_edits import EditRequest
from .graph_models import IDENTIFIER, StrictModel, utc_now
from .workflow_service import (
    AnalysisRequest, GenerateRequest, ImportRequest, ReviewRequired,
    SuggestRequest, WorkflowService, complete_in_thread,
)
from .workflow_store import DraftNotFound, RevisionConflict, WorkflowStore, json_text


JobKind = Literal["analyze", "generate", "suggest", "import", "edit"]
REQUEST_MODELS = {
    "analyze": AnalysisRequest, "generate": GenerateRequest, "suggest": SuggestRequest,
    "import": ImportRequest, "edit": EditRequest,
}
TERMINAL_STATES = {"succeeded", "failed", "interrupted"}
log = logging.getLogger(__name__)


class JobConflict(ValueError):
    """The requested job action conflicts with already saved work."""


class JobRequest(StrictModel):
    """
    A browser action with a durable, client-created identifier.
    - The browser saves request_id before sending and reuses it if delivery is uncertain.
    - Non-analysis actions also identify the exact draft being changed.
    - Payloads use the same validated contracts as the direct draft endpoints.
    """

    kind: JobKind
    draft_id: str | None = Field(default = None, pattern = IDENTIFIER)
    payload: dict[str, Any]
    request_id: UUID
    session_id: UUID = UUID(int = 0)

    @model_validator(mode = "after")
    def validate_action(self):
        if self.kind == "analyze" and self.draft_id is not None:
            raise ValueError("Analysis creates a new draft; omit draft_id.")
        if self.kind != "analyze" and self.draft_id is None:
            raise ValueError("Choose a saved draft before requesting this action.")
        request = REQUEST_MODELS[self.kind].model_validate(self.payload)
        # Keep omitted settings omitted. In particular, a saved Ollama choice
        # must not become the schema's default cloud provider during recovery.
        self.payload = request.model_dump(mode = "json", exclude_unset = True)
        return self


class RetryRequest(StrictModel):
    """Require the original request identity before cloning interrupted work."""

    request_id: UUID


class WorkerLock:
    """
    Hold exclusive worker ownership without fragile timestamps or PID checks.
    - The lock belongs to an open file descriptor and is released on process exit.
    - It is never deleted; unlinking a lock file could create two independent locks.
    - A second backend waits without labelling the live worker's jobs interrupted.
    - The store should be on this machine's disk, not a shared network filesystem.
    """

    def __init__(self, directory: Path):
        self.path = directory / "worker.lock"
        self.file = None

    def acquire(self) -> bool:
        if self.file is not None:
            return True
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            handle.close()
            return False
        except OSError as error:
            handle.close()
            if error.errno in {11, 13, 35, 36}:
                return False
            raise
        self.file = handle
        return True

    def release(self) -> None:
        if self.file is None:
            return
        handle, self.file = self.file, None
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class JobRepository:
    """
    Persist job inputs and state changes using short transactions.
    - Input payloads stay private; the public job response contains status/result.
    - Request fingerprints prevent accidental reuse of an identifier for new input.
    - A running job is changed only by its recorded worker owner.
    - Per-run progress messages are deliberately neither stored nor returned.
    """

    def __init__(self, store: WorkflowStore):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    session_id TEXT,
                    fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_draft_id TEXT,
                    draft_id TEXT,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    result_summary_json TEXT,
                    error_json TEXT,
                    worker_id TEXT,
                    retry_of TEXT REFERENCES jobs(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created_at);
            """)
            # This additive migration also supports stores opened by an earlier
            # build of the job API. Acquire a transaction before checking so two
            # new server processes cannot both try to add the same column.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "result_summary_json" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN result_summary_json TEXT")
            if "session_id" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN session_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_session_created ON jobs(session_id, created_at)")
            # - Earlier builds retained hundreds of progress messages per run.
            # - The interface no longer exposes run history, so remove the old
            #   table during startup and never recreate it.
            db.execute("DROP TABLE IF EXISTS job_logs")

    @staticmethod
    def _row(db, job_id: str):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise DraftNotFound(f"Job not found: {job_id}")
        return row

    @staticmethod
    def _result_metadata(result: dict) -> dict:
        return {key: result[key] for key in (
            "draft_id", "revision", "status", "title", "project_root", "output_folder",
            "outputs", "generation", "export_warning", "message", "settings",
        ) if key in result}

    @staticmethod
    def _public_columns(summary: bool = False) -> str:
        result_column = "COALESCE(result_summary_json, result_json) AS result_json" if summary else "result_json"
        return "id, request_id, session_id, kind, state, draft_id, error_json, retry_of, created_at, updated_at, " + result_column

    @staticmethod
    def _public(row, *, summary: bool = False) -> dict:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if summary and result is not None:
            # Polling a list must not transfer every saved graph and evidence
            # excerpt. The selected draft or individual job still returns the
            # full result on demand, including its detailed review information.
            result = JobRepository._result_metadata(result)
        return {
            "id": row["id"], "request_id": row["request_id"], "session_id": row["session_id"], "kind": row["kind"],
            "state": row["state"], "draft_id": row["draft_id"],
            "result": result, "result_complete": not summary,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "retry_of": row["retry_of"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def enqueue(self, request: JobRequest, *, retry_of: str | None = None) -> dict:
        """
        Save an action exactly once for its client request identifier.
        - A repeated network request returns the already saved job.
        - A different payload with the same identifier is a visible conflict.
        - Retry requests retain the original revision; stale edits still fail.
        """
        fingerprint = hashlib.sha256(json_text({
            "kind": request.kind, "draft_id": request.draft_id, "payload": request.payload,
            "session_id": str(request.session_id),
            "retry_of": retry_of,
        }).encode("utf-8")).hexdigest()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE request_id=?", (str(request.request_id),)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise JobConflict("This request identifier was already used with different input. Start a new action instead.")
                return self._public(existing)
            if retry_of is not None:
                previous = self._row(db, retry_of)
                if previous["state"] not in {"failed", "interrupted"}:
                    raise JobConflict("Only a failed or interrupted job can be retried. Reload its saved result or wait for it to finish.")
                if db.execute("SELECT 1 FROM operation_receipts WHERE operation_id=?", (retry_of,)).fetchone():
                    raise JobConflict("This operation already committed. Reload the saved draft before trying another action.")
                root = previous
                while root["retry_of"] is not None:
                    root = self._row(db, root["retry_of"])
                related = db.execute("""WITH RECURSIVE attempts(id, state) AS (
                    SELECT id, state FROM jobs WHERE id=?
                    UNION ALL SELECT child.id, child.state FROM jobs child JOIN attempts parent ON child.retry_of=parent.id
                ) SELECT id, state FROM attempts WHERE state IN ('queued', 'running', 'succeeded') LIMIT 1""",
                                     (root["id"],)).fetchone()
                if related is not None:
                    raise JobConflict("This operation already has an active or completed retry. Open that job instead of repeating it.")
            if request.draft_id is not None:
                self.store._row(db, request.draft_id)
            now, job_id = utc_now(), "job_" + uuid4().hex
            db.execute("""INSERT INTO jobs
                (id, request_id, session_id, fingerprint, kind, target_draft_id, draft_id, payload_json,
                 state, retry_of, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""", (
                job_id, str(request.request_id), str(request.session_id), fingerprint, request.kind, request.draft_id,
                request.draft_id, json_text(request.payload), retry_of, now, now,
            ))
            return self._public(self._row(db, job_id))

    def retry(self, job_id: str, request_id: UUID) -> dict:
        with self.store.connection() as db:
            previous = self._row(db, job_id)
            request = JobRequest(kind = previous["kind"], draft_id = previous["target_draft_id"],
                                 payload = json.loads(previous["payload_json"]), request_id = request_id,
                                 session_id = previous["session_id"] or UUID(int = 0))
        return self.enqueue(request, retry_of = job_id)

    def get(self, job_id: str) -> dict:
        with self.store.connection() as db:
            row = db.execute("SELECT " + self._public_columns() + " FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise DraftNotFound(f"Job not found: {job_id}")
            return self._public(row)

    def list(self, limit: int = 100, *, summary: bool = False, session_id: UUID | None = None) -> list[dict]:
        with self.store.connection() as db:
            # Avoid reading uploaded XML and large graph JSON merely to poll
            # progress. New jobs store this small result beside the full record.
            query = "SELECT " + self._public_columns(summary) + " FROM jobs"
            arguments: tuple = (limit,)
            if session_id is not None:
                query += " WHERE session_id=?"
                arguments = (str(session_id), limit)
            query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
            rows = db.execute(query, arguments).fetchall()
            return [self._public(row, summary = summary) for row in rows]

    def busy(self) -> bool:
        with self.store.connection() as db:
            return db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone() is not None

    def orphaned(self) -> list[str]:
        with self.store.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM jobs WHERE state='running'")]

    def claim(self, worker_id: str) -> dict | None:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running', worker_id=?, updated_at=? WHERE id=? AND state='queued'",
                       (worker_id, utc_now(), row["id"]))
            claimed = dict(self._row(db, row["id"]))
            claimed["payload"] = json.loads(claimed.pop("payload_json"))
            return claimed

    def finish(self, job_id: str, state: str, *, result: dict | None = None,
               error: dict | None = None, worker_id: str | None = None) -> None:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id)
            if row["state"] != "running":
                return
            if worker_id is not None and row["worker_id"] != worker_id:
                raise JobConflict("The job belongs to a different running worker.")
            db.execute("""UPDATE jobs SET state=?, result_json=?, result_summary_json=?, error_json=?, draft_id=?,
                          updated_at=?, worker_id=NULL WHERE id=?""", (
                state, json_text(result) if result is not None else None,
                json_text(self._result_metadata(result)) if result is not None else None,
                json_text(error) if error is not None else None,
                result.get("draft_id", row["draft_id"]) if result else row["draft_id"], utc_now(), job_id,
            ))


def job_error(error: Exception) -> dict:
    """
    Return actionable errors without sending tracebacks or source payloads.
    - Conflicts tell the UI which revision needs to be reloaded.
    - Validation and missing-file errors preserve their useful explanation.
    - Unexpected provider/file failures are also recorded in server logging.
    """
    if isinstance(error, RevisionConflict):
        return {"message": str(error), "code": "revision_conflict", "status_code": 409,
                "expected_revision": error.expected, "current_revision": error.actual}
    if isinstance(error, ReviewRequired):
        return {"message": str(error), "code": "review_required", "status_code": 409}
    if isinstance(error, FileNotFoundError):
        return {"message": str(error), "code": "not_found", "status_code": 404}
    if isinstance(error, ValueError):
        return {"message": str(error)[:4000], "code": "invalid_request", "status_code": 422}
    return {"message": f"{type(error).__name__}: {str(error)[:2000]}", "code": "operation_failed", "status_code": 500}


class WorkflowJobs:
    """
    Own the single queue worker for one application instance.
    - HTTP requests finish after queueing; their cancellation does not cancel work.
    - The worker's lock spans analysis, model calls and final publication.
    - Shutdown waits for in-flight filesystem/database writes before releasing it.
    - Recovery checks receipts before offering a retry, including the narrow
      interval between a graph commit and the job's final status update.
    """

    def __init__(self, service: WorkflowService, instance_id: str, poll_seconds: float = 0.35):
        self.service, self.instance_id = service, instance_id
        self.repository = JobRepository(service.store)
        self.lock = WorkerLock(service.store.directory)
        self.poll_seconds = poll_seconds
        self.worker_state = "starting"
        self.accepting = True
        self.task = None
        self.current_job_id = None
        self.wake = asyncio.Event()
        self.submission_lock = asyncio.Lock()
        self.stopping = False

    async def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name = "workflow-job-worker")
            # Let the first lock attempt happen without delaying startup for an
            # entire analysis. Subsequent HTTP requests remain independent.
            await asyncio.sleep(0)

    async def stop(self) -> None:
        self.accepting, self.stopping = False, True
        self.worker_state = "stopping"
        try:
            if self.task is not None:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
        finally:
            self.lock.release()

    def _saved_result(self, operation_id: str) -> dict | None:
        receipt = self.service.store.operation_receipt(operation_id)
        if receipt is None:
            return None
        graph = self.service.store.load(receipt["draft_id"], receipt["revision"])
        return self.service.describe(graph, generation_id = receipt["generation_id"])

    async def _recover(self) -> None:
        """
        Reconcile work only after obtaining exclusive ownership of the store.
        - Committed operations become successful with their original saved revision.
        - Uncommitted operations become interrupted and need a deliberate retry.
        - Queued work never started and is safe to process normally.
        """
        for job_id in await complete_in_thread(self.repository.orphaned):
            result = await complete_in_thread(self._saved_result, job_id)
            if result is not None:
                await complete_in_thread(self.repository.finish, job_id, "succeeded", result = result)
            else:
                await complete_in_thread(self.repository.finish, job_id, "interrupted", error = {
                    "code": "backend_interrupted", "status_code": 409,
                    "message": "The backend stopped before this job finished. Saved drafts are unchanged. Review the job, then retry explicitly; an interrupted model request may already have incurred a charge.",
                })

    async def _execute(self, job: dict) -> dict:
        request = REQUEST_MODELS[job["kind"]].model_validate(job["payload"])

        existing = await complete_in_thread(self._saved_result, job["id"])
        if existing is not None:
            return existing
        draft_id, operation_id = job["target_draft_id"], job["id"]
        if job["kind"] == "analyze":
            graph = await self.service.analyze(request, operation_id = operation_id)
        elif job["kind"] == "generate":
            manifest = await self.service.generate(draft_id, request, operation_id = operation_id)
            graph = await complete_in_thread(self.service.store.load, draft_id, request.expected_revision)
            return await complete_in_thread(self.service.describe, graph, generation_id = manifest["generation_id"])
        elif job["kind"] == "suggest":
            graph = await self.service.suggest(draft_id, request, operation_id = operation_id)
        elif job["kind"] == "import":
            graph = await complete_in_thread(self.service.import_diagram, draft_id, request, operation_id = operation_id)
        else:
            graph = await complete_in_thread(self.service.edit, draft_id, request, operation_id = operation_id)
        return await complete_in_thread(self.service.describe, graph)

    async def _run_one(self, job: dict) -> None:
        self.current_job_id = job["id"]
        try:
            result = await self._execute(job)
            await complete_in_thread(self.repository.finish, job["id"], "succeeded", result = result,
                                     worker_id = self.instance_id)
        except asyncio.CancelledError:
            result = await complete_in_thread(self._saved_result, job["id"])
            if result is not None:
                await complete_in_thread(self.repository.finish, job["id"], "succeeded", result = result,
                                         worker_id = self.instance_id)
            else:
                await complete_in_thread(self.repository.finish, job["id"], "interrupted", worker_id = self.instance_id,
                                         error = {"message": "The backend stopped before this job finished. Review and retry explicitly when it is running again; model requests may already have incurred a charge.",
                                                "code": "backend_interrupted", "status_code": 409})
            raise
        except Exception as error:
            # The graph may have committed before an export or response failed.
            # Recover that successful receipt before telling the UI to retry.
            result = await complete_in_thread(self._saved_result, job["id"])
            if result is not None:
                await complete_in_thread(self.repository.finish, job["id"], "succeeded", result = result,
                                         worker_id = self.instance_id)
            else:
                log.warning("Workflow job %s failed: %s", job["id"], error)
                await complete_in_thread(self.repository.finish, job["id"], "failed", error = job_error(error),
                                         worker_id = self.instance_id)
        finally:
            self.current_job_id = None

    async def _run(self) -> None:
        needs_recovery = True
        try:
            while not self.stopping:
                try:
                    if self.lock.file is None:
                        if not self.lock.acquire():
                            self.worker_state = "standby"
                            await asyncio.sleep(self.poll_seconds)
                            continue
                        needs_recovery = True
                    if needs_recovery:
                        self.worker_state = "recovering"
                        await self._recover()
                        needs_recovery = False
                    self.worker_state = "active"
                    self.wake.clear()
                    job = await complete_in_thread(self.repository.claim, self.instance_id)
                    if job is not None:
                        await self._run_one(job)
                        continue
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.wake.wait(), timeout = self.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A full disk or temporary database failure must not strand
                    # the queue behind a silently dead background task.
                    self.worker_state = "recovering"
                    needs_recovery = True
                    log.exception("Workflow worker is waiting for storage to recover")
                    await asyncio.sleep(self.poll_seconds)
        finally:
            try:
                # Cancellation may arrive immediately after claim committed,
                # before the selected row reached _run_one. Reconcile that row
                # while this process still exclusively owns the worker lock.
                if self.lock.file is not None:
                    await self._recover()
            finally:
                self.lock.release()


router = APIRouter(prefix = "/api/jobs", tags = ["Saved operations"])


@router.post("", status_code = 202)
async def submit_job(body: JobRequest, request: Request):
    manager = request.app.state.workflow_jobs
    async with manager.submission_lock:
        if not manager.accepting:
            raise HTTPException(status_code = 503, detail = "The backend is stopping. Restart it before submitting another job.")
        job = await complete_in_thread(manager.repository.enqueue, body)
    manager.wake.set()
    return job


@router.get("")
async def list_jobs(request: Request, limit: int = Query(default = 100, ge = 1, le = 1000),
                    summary: bool = Query(default = False), session_id: UUID | None = None):
    return await asyncio.to_thread(
        request.app.state.workflow_jobs.repository.list,
        limit,
        summary = summary,
        session_id = session_id,
    )


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request):
    return await asyncio.to_thread(request.app.state.workflow_jobs.repository.get, job_id)


@router.post("/{job_id}/retry", status_code = 202)
async def retry_job(job_id: str, body: RetryRequest, request: Request):
    manager = request.app.state.workflow_jobs
    async with manager.submission_lock:
        if not manager.accepting:
            raise HTTPException(status_code = 503, detail = "The backend is stopping. Restart it before retrying this job.")
        job = await complete_in_thread(manager.repository.retry, job_id, body.request_id)
    manager.wake.set()
    return job
