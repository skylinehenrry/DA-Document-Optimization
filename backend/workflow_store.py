"""
Keep the saved workflow, its history and completed operations in one database.
- SQLite is the authority; exported diagrams are disposable views of a revision.
- Every edit checks its starting revision before changing nodes or connections.
- Source snapshots remain private and are checked before later model requests.
- An operation receipt commits in the same transaction as the graph or report.
  This lets a restarted app recognize work that finished just before it stopped.
- Existing database files are extended in place; saved drafts are not migrated
  into a new folder or discarded when the application starts.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable

from .graph_models import GraphDocument, utc_now


class RevisionConflict(ValueError):
    """A stale mutation that must reload before it can replace a newer revision."""

    def __init__(self, expected: int, actual: int):
        self.expected, self.actual = expected, actual
        super().__init__(f"Draft changed: expected revision {expected}, current revision is {actual}. Reload or re-export before editing/generating.")


class DraftNotFound(FileNotFoundError):
    """A requested draft, revision, or generation is absent from this store."""

    pass


def json_text(value: object) -> str:
    """Serialize portable, deterministic JSON for hashes and database records."""

    return json.dumps(value, ensure_ascii = False, sort_keys = True, indent = 2, allow_nan = False)


class WorkflowStore:
    """Own all durable graph, snapshot, history, cache, and generation records.

    - Open one short-lived SQLite connection per operation for thread safety.
    - Use immediate transactions for revision-changing operations.
    - Commit operation receipts with their result so process recovery can tell a
      completed mutation from work interrupted before its transaction committed.
    - Keep source snapshots and generated manifests outside public web directories.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents = True, exist_ok = True)
        self.database = self.directory / "workflows.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    graph_json TEXT NOT NULL, output_folder TEXT NOT NULL,
                    settings_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    draft_id TEXT NOT NULL REFERENCES drafts(id),
                    revision INTEGER NOT NULL, graph_json TEXT NOT NULL,
                    change_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (draft_id, revision)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    draft_id TEXT NOT NULL REFERENCES drafts(id), path TEXT NOT NULL,
                    content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    PRIMARY KEY (draft_id, path)
                );
                CREATE TABLE IF NOT EXISTS summary_cache (
                    draft_id TEXT NOT NULL REFERENCES drafts(id),
                    node_id TEXT NOT NULL, cache_key TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    PRIMARY KEY (draft_id, node_id, cache_key)
                );
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(id),
                    revision INTEGER NOT NULL, manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS suppressed_edges (
                    draft_id TEXT NOT NULL REFERENCES drafts(id),
                    source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
                    PRIMARY KEY (draft_id, source, target, kind)
                );
                CREATE TABLE IF NOT EXISTS operation_receipts (
                    operation_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES drafts(id),
                    revision INTEGER NOT NULL,
                    generation_id TEXT,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        """Yield a configured connection and always commit, roll back, and close it."""

        db = sqlite3.connect(self.database, timeout = 30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _row(db, draft_id: str):
        row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise DraftNotFound(f"Draft not found: {draft_id}")
        return row

    def create(self, graph: GraphDocument, snapshots: dict[str, str], output_folder: str | Path,
               settings: dict | None = None, *, operation_id: str | None = None) -> GraphDocument:
        """Save revision one, its source snapshots, settings, and optional receipt.

        - Validate snapshot ownership and completeness before opening a transaction.
        - Create the selected output folder explicitly; it is never a public mount.
        - Commit the current draft and immutable revision-history row together.
        """

        graph = GraphDocument.model_validate(graph.model_dump())
        if graph.revision != 1:
            raise ValueError("New drafts must start at revision 1.")
        known_paths = {source.path for source in graph.sources}
        if not set(snapshots).issubset(known_paths):
            raise ValueError("Source snapshots must belong to the source manifest.")
        missing = [source.path for source in graph.sources if source.status != "failed" and source.path not in snapshots]
        if missing:
            raise ValueError(f"Missing source snapshots: {', '.join(missing)}")
        output_folder = Path(output_folder).expanduser().resolve()
        output_folder.mkdir(parents = True, exist_ok = True)
        encoded = graph.model_dump_json()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?, ?)", (
                graph.id, 1, encoded, str(output_folder), json_text(settings or {}), graph.created_at, graph.updated_at,
            ))
            db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?)", (
                graph.id, 1, encoded, json_text({"action": "analyze", "source_digest": graph.source_digest}), graph.created_at,
            ))
            db.executemany("INSERT INTO sources VALUES (?, ?, ?, ?)", [
                (graph.id, path, content, hashlib.sha256(content.encode("utf-8")).hexdigest())
                for path, content in snapshots.items()
            ])
            self._record_operation(db, operation_id, graph.id, graph.revision)
        return graph

    @staticmethod
    def _record_operation(db, operation_id: str | None, draft_id: str, revision: int,
                          generation_id: str | None = None) -> None:
        """
        Record a durable completion marker inside the caller's transaction.
        - The marker and the saved change either both commit or both roll back.
        - A duplicate operation cannot silently create a second revision.
        - Direct service/CLI calls may omit an operation identifier.
        """
        if operation_id is not None:
            db.execute("INSERT INTO operation_receipts VALUES (?, ?, ?, ?, ?)",
                       (operation_id, draft_id, revision, generation_id, utc_now()))

    def operation_receipt(self, operation_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM operation_receipts WHERE operation_id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def load(self, draft_id: str, revision: int | None = None) -> GraphDocument:
        with self.connection() as db:
            row = self._row(db, draft_id)
            if revision is not None:
                row = db.execute("SELECT graph_json FROM revisions WHERE draft_id=? AND revision=?", (draft_id, revision)).fetchone()
                if row is None:
                    raise DraftNotFound(f"Revision {revision} not found for draft {draft_id}.")
            return GraphDocument.model_validate_json(row["graph_json"])

    def metadata(self, draft_id: str) -> dict:
        with self.connection() as db:
            row = self._row(db, draft_id)
            return {"id": row["id"], "revision": row["revision"], "output_folder": row["output_folder"],
                    "settings": json.loads(row["settings_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_drafts(self, output_folder: str | Path | None = None, limit: int = 100) -> list[dict]:
        """
        Return the saved-work library without loading all source snapshots.
        - Each entry includes its own title, project path and current revision.
        - Generated status belongs to that revision, never another project.
        - The UI can rebuild its library after a reload using only this endpoint.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000.")
        with self.connection() as db:
            query = "SELECT id, revision, output_folder, created_at, updated_at, graph_json FROM drafts"
            args: list = []
            if output_folder is not None:
                query += " WHERE output_folder=?"
                args.append(str(Path(output_folder).expanduser().resolve()))
            query += " ORDER BY created_at DESC, id DESC LIMIT ?"
            args.append(limit)
            result = []
            for row in db.execute(query, args).fetchall():
                item = dict(row)
                graph = json.loads(item.pop("graph_json"))
                generation = db.execute(
                    "SELECT id FROM generations WHERE draft_id=? AND revision=? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (item["id"], item["revision"]),
                ).fetchone()
                item.update(
                    draft_id = item["id"], title = graph["title"], project_root = graph["project_root"],
                    node_count = len(graph["nodes"]), edge_count = len(graph["edges"]),
                    source_count = len(graph["sources"]), issue_count = len(graph["issues"]),
                    status = "generated" if generation else "draft",
                    generation_id = generation["id"] if generation else None,
                    has_analysis_errors = any(source["status"] == "failed" for source in graph["sources"])
                    or any(issue["severity"] == "error" for issue in graph["issues"]),
                )
                result.append(item)
            return result

    def snapshots(self, draft_id: str) -> dict[str, str]:
        with self.connection() as db:
            self._row(db, draft_id)
            result = {}
            for row in db.execute("SELECT path, content, content_sha256 FROM sources WHERE draft_id=?", (draft_id,)):
                if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row["content_sha256"]:
                    raise ValueError(f"Saved source snapshot failed its integrity check: {row['path']}")
                result[row["path"]] = row["content"]
            return result

    def update(self, draft_id: str, expected_revision: int,
               transform: Callable[[GraphDocument], GraphDocument], change: dict, *,
               operation_id: str | None = None) -> GraphDocument:
        """Apply one optimistic, atomic graph revision and retain its audit record.

        - Lock before reading so the revision comparison and write cannot race.
        - Reject changes to graph identity, source manifest, or analysis baseline.
        - Remember removed relationships so model suggestions do not restore them.
        - Save the new revision and operation receipt in the same transaction.
        """

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, draft_id)
            if row["revision"] != expected_revision:
                raise RevisionConflict(expected_revision, row["revision"])
            previous = GraphDocument.model_validate_json(row["graph_json"])
            updated = transform(previous.model_copy(deep = True))
            # A diagram import/patch cannot replace source identity or the baseline.
            for field in ("id", "project_root", "source_digest", "sources", "created_at"):
                if getattr(updated, field) != getattr(previous, field):
                    raise ValueError(f"Editing cannot change protected graph field: {field}")
            payload = updated.model_dump()
            payload.update(revision = expected_revision + 1, updated_at = utc_now())
            updated = GraphDocument.model_validate(payload)
            remaining = {(edge.source, edge.target, edge.kind) for edge in updated.edges}
            suppressed = {(edge.source, edge.target, edge.kind) for edge in previous.edges} - remaining
            db.executemany("INSERT OR IGNORE INTO suppressed_edges VALUES (?, ?, ?, ?)", [
                (draft_id, source, target, kind) for source, target, kind in suppressed
            ])
            encoded = updated.model_dump_json()
            db.execute("UPDATE drafts SET revision=?, graph_json=?, updated_at=? WHERE id=?", (
                updated.revision, encoded, updated.updated_at, draft_id,
            ))
            db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?)", (
                draft_id, updated.revision, encoded, json_text(change), updated.updated_at,
            ))
            self._record_operation(db, operation_id, draft_id, updated.revision)
            return updated

    def suppressed_edges(self, draft_id: str) -> set[tuple[str, str, str]]:
        with self.connection() as db:
            return {tuple(row) for row in db.execute("SELECT source, target, kind FROM suppressed_edges WHERE draft_id=?", (draft_id,))}

    def history(self, draft_id: str) -> list[dict]:
        with self.connection() as db:
            self._row(db, draft_id)
            return [{"revision": row["revision"], "created_at": row["created_at"], "change": json.loads(row["change_json"])}
                    for row in db.execute("SELECT revision, change_json, created_at FROM revisions WHERE draft_id=? ORDER BY revision", (draft_id,))]

    def cached_summary(self, draft_id: str, node_id: str, cache_key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT summary_json FROM summary_cache WHERE draft_id=? AND node_id=? AND cache_key=?", (draft_id, node_id, cache_key)).fetchone()
            return json.loads(row[0]) if row else None

    def cache_summary(self, draft_id: str, node_id: str, cache_key: str, summary: dict) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO summary_cache VALUES (?, ?, ?, ?)", (draft_id, node_id, cache_key, json_text(summary)))

    def record_generation(self, draft_id: str, revision: int, manifest: dict, *,
                          operation_id: str | None = None) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, draft_id)
            if row["revision"] != revision:
                raise RevisionConflict(revision, row["revision"])
            if manifest["draft_id"] != draft_id or manifest["revision"] != revision:
                raise ValueError("Generation manifest does not match the reviewed graph.")
            db.execute("INSERT INTO generations VALUES (?, ?, ?, ?, ?)", (
                manifest["generation_id"], draft_id, revision, json_text(manifest), manifest["created_at"],
            ))
            self._record_operation(db, operation_id, draft_id, revision, manifest["generation_id"])

    def latest_generation(self, draft_id: str, revision: int | None = None) -> dict | None:
        with self.connection() as db:
            row = self._row(db, draft_id)
            revision = row["revision"] if revision is None else revision
            generation = db.execute("SELECT manifest_json FROM generations WHERE draft_id=? AND revision=? ORDER BY created_at DESC, id DESC LIMIT 1", (draft_id, revision)).fetchone()
            return json.loads(generation[0]) if generation else None

    def generation(self, draft_id: str, generation_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT manifest_json FROM generations WHERE draft_id=? AND id=?", (draft_id, generation_id)).fetchone()
            if row is None:
                raise DraftNotFound(f"Generation not found: {generation_id}")
            return json.loads(row[0])

    def artifact_root(self, draft_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", draft_id):
            raise ValueError("Invalid draft ID.")
        return Path(self.metadata(draft_id)["output_folder"]) / "outputs" / "workflows" / draft_id
