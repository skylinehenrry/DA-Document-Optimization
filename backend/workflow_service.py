"""
Coordinate analysis, visual review and final flowchart generation.
- The reviewed graph is the authority for every node and connection.
- Model calls add explanations or explicit proposals; they do not rewrite it.
- Database transactions save changes before disposable diagram exports begin.
- Job operation identifiers accompany each commit so a restart can recover its
  result without applying an edit twice or repeating a completed model request.
- Final files have generation-specific addresses and integrity checks.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable
import uuid

from pydantic import Field, field_validator

from .drawio import export_drawio, import_drawio
from .graph_edits import EditRequest, apply_edits
from .graph_diagnostics import graph_diagnostics
from .graph_enrichment import ModelProvider, enrich_summaries, suggest_relationships
from .graph_models import GraphDocument, NarrativeSummary, StrictModel, topology_signature, utc_now
from .graph_rendering import layout_graph, render_graph_html, render_graph_svg
from .project_identity import flowchart_attachment
from .static_analysis import analyze_project
from .workflow_store import RevisionConflict, WorkflowStore, json_text


# Public workflow request contracts.
# - The browser, direct API, background jobs, and command line all validate through
#   these same models so no entry point can bypass limits or revision requirements.
# - Provider settings are optional workflow preferences; source and output locations
#   are explicit local paths and are never converted into browser URLs.
class AnalysisRequest(StrictModel):
    """Inputs for offline extraction and creation of revision one."""

    script_folder: str = Field(min_length = 1)
    da_document_folder: str = Field(min_length = 1)
    title: str | None = Field(default = None, max_length = 1000)
    working_directory: str | None = None
    sql_dialect: str | None = None
    database_namespace: str | None = None
    model: ModelProvider = "OpenAI"
    language: str = Field(default = "English", min_length = 1, max_length = 100)
    max_concurrency: int = Field(default = 3, ge = 1, le = 16)

    @field_validator("title", mode = "before")
    @classmethod
    def optional_project_name(cls, value):
        # - Keep project naming optional in the browser, API and command line.
        # - Normalize blank input to the same folder-name fallback as omission.
        # - Preserve the user's meaningful spelling instead of modifying saved titles.
        return value.strip() or None if isinstance(value, str) else value


class GenerateRequest(StrictModel):
    """Options for rendering one exact reviewed revision into final artifacts."""

    expected_revision: int = Field(ge = 1)
    use_llm: bool = False
    model: ModelProvider = "OpenAI"
    language: str = Field(default = "English", min_length = 1, max_length = 100)
    max_concurrency: int = Field(default = 3, ge = 1, le = 16)
    timeout_seconds: float = Field(default = 90, gt = 0, le = 300)
    allow_proposed: bool = False
    acknowledge_incomplete: bool = False


class ImportRequest(StrictModel):
    """One complete draw.io document replacing an expected graph revision."""

    expected_revision: int = Field(ge = 1)
    xml: str = Field(min_length = 1, max_length = 10_000_000)


class SuggestRequest(StrictModel):
    """Bounded provider options for explicit review-only relationship proposals."""

    expected_revision: int = Field(ge = 1)
    model: ModelProvider = "OpenAI"
    max_concurrency: int = Field(default = 3, ge = 1, le = 16)
    timeout_seconds: float = Field(default = 90, gt = 0, le = 300)


class ReviewRequired(ValueError):
    """Generation is blocked until the recorded analysis limitations are accepted."""

    pass


async def complete_in_thread(function, *args, **kwargs):
    """
    Finish a filesystem/database worker before reporting its cancellation.
    - Cancelling asyncio.to_thread alone does not stop the operating-system thread.
    - Waiting for that thread prevents it from committing after its job was marked
      interrupted and another worker was allowed to take ownership.
    - A force-closed process is still recoverable through SQLite transactions and
      the operation receipt; no source program is ever executed by this helper.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


def saved_model_options(request, settings: dict):
    """Omitted provider options inherit the draft; never switch local to cloud implicitly."""
    overrides = {key: settings[key] for key in ("model", "language", "max_concurrency")
                 if key in type(request).model_fields and key not in request.model_fields_set and key in settings}
    return type(request).model_validate({**request.model_dump(), **overrides})


def review_report(graph: GraphDocument) -> dict:
    """Build the complete review summary without modifying the saved graph.

    - Count source coverage, provenance, proposal state, and user corrections.
    - Retain detailed issues and the grouped, plain-language diagnostics view.
    - State the direction legend and project-dependency scope explicitly.
    """

    source_paths = {source.path for source in graph.sources}
    represented = {node.source_path for node in graph.nodes if node.kind == "script" and node.source_path}
    return {
        "draft_id": graph.id, "revision": graph.revision, "source_digest": graph.source_digest,
        "scope": "Project dependencies and explicit workflow connections; not a complete statement-level control-flow graph.",
        "direction_legend": {"reads": "resource → reader", "writes": "writer → resource",
                             "imports": "importer → imported module", "calls": "caller → callee",
                             "depends_on": "dependent → dependency", "control_flow": "explicit predecessor → successor"},
        "node_count": len(graph.nodes), "edge_count": len(graph.edges),
        "sources_by_type": dict(Counter(source.script_type for source in graph.sources)),
        "sources_by_status": dict(Counter(source.status for source in graph.sources)),
        "source_nodes_removed_by_user": sorted(source_paths - represented),
        "proposed_edge_ids": [edge.id for edge in graph.edges if edge.status == "proposed"],
        "user_edge_ids": [edge.id for edge in graph.edges if edge.origin == "user"],
        "issues": [issue.model_dump() for issue in graph.issues],
        "has_analysis_errors": any(source.status == "failed" for source in graph.sources) or any(issue.severity == "error" for issue in graph.issues),
        "has_warnings": any(issue.severity in {"warning", "error"} for issue in graph.issues),
        "diagnostics": graph_diagnostics(graph),
    }


def _write_directory(destination: Path, files: dict[str, str]) -> None:
    """
    Publish an entire new artifact directory in a single rename.
    - Work stays in a temporary sibling directory until every file is complete.
    - Flush each file before the database is allowed to advertise the generation.
    - Never overwrite a previously published revision or generation directory.
    - An interrupted temporary directory is private and is never served by HTTP.
    """
    destination.parent.mkdir(parents = True, exist_ok = True)
    temporary = Path(tempfile.mkdtemp(prefix = ".pending-", dir = destination.parent))
    try:
        for name, text in files.items():
            # - Manifest hashes describe UTF-8 text with LF line endings. Windows
            #   newline translation would otherwise make every download fail its
            #   integrity check even though the renderer returned correct text.
            # - Flush with a writable handle; Windows requires write access for
            #   FlushFileBuffers/_commit, unlike a permissive POSIX read handle.
            (temporary / name).write_text(text, encoding = "utf-8", newline = "\n")
            with (temporary / name).open("r+b") as written:
                os.fsync(written.fileno())
        if destination.exists():
            raise FileExistsError(f"Artifact directory already exists: {destination}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


class WorkflowService:
    """Coordinate every state-changing step around one authoritative store.

    - Analysis creates revision one from offline extractor output and snapshots.
    - Edits, imports, and suggestions create checked revisions before exports.
    - Generation enriches prose only, verifies topology, publishes complete files,
      and registers their hashes against one exact revision.
    - Disposable export failures never roll back a graph that already committed.
    """

    def __init__(self, store: WorkflowStore):
        self.store = store
        self._export_warnings: dict[tuple[str, int], str] = {}

    def require_revision(self, draft_id: str, expected_revision: int) -> GraphDocument:
        graph = self.store.load(draft_id)
        if graph.revision != expected_revision:
            raise RevisionConflict(expected_revision, graph.revision)
        return graph

    def save_draft_exports(self, graph: GraphDocument) -> dict[str, Path]:
        destination = self.store.artifact_root(graph.id) / "revisions" / str(graph.revision)
        try:
            files = {"draft.json": graph.model_dump_json(indent = 2), "draft.drawio": export_drawio(graph),
                     "draft.svg": render_graph_svg(graph), "review.json": json_text(review_report(graph))}
            if not destination.exists():
                _write_directory(destination, files)
        except (OSError, ValueError) as error:
            # The revision already committed successfully. Do not report a
            # failed edit when only a disposable filesystem export failed.
            self._export_warnings[(graph.id, graph.revision)] = (
                f"Revision {graph.revision} is saved. Local draft exports failed ({type(error).__name__}); "
                "download from the draft API or retry export after resolving the output-folder issue."
            )
            return {}
        self._export_warnings.pop((graph.id, graph.revision), None)
        return {name: destination / name for name in files}

    async def analyze(self, request: AnalysisRequest, logger: Callable[[str], None] | None = None, *,
                      operation_id: str | None = None) -> GraphDocument:
        log = logger or (lambda message: None)
        log(f"Script folder received: {request.script_folder}")
        log(f"DA Document folder received: {request.da_document_folder}")
        output_folder = Path(request.da_document_folder).expanduser().resolve()
        output_folder.mkdir(parents = True, exist_ok = True)
        log("Extracting imports, calls and data references using static parsers; no source is executed.")
        graph, snapshots = await complete_in_thread(
            analyze_project, request.script_folder, working_directory = request.working_directory,
            sql_dialect = request.sql_dialect, database_namespace = request.database_namespace,
            title = request.title, logger = log,
        )
        if not graph.sources and not any(node.kind == "script" for node in graph.nodes):
            raise ValueError("No supported source files were found. Choose a project containing Python (.py), SQL (.sql), BAT (.bat) or Alteryx (.yxmd, .yxwz, .yxmc) files.")
        log(f"Found {len(graph.sources)} supported script file(s).")
        # Store the initial layout in the draft so export/import starts from one
        # set of positions. It is presentation data, not an execution schedule.
        positions = await complete_in_thread(layout_graph, graph)
        for node in graph.nodes:
            if node.position is None:
                node.position = positions[node.id]
        graph = GraphDocument.model_validate(graph.model_dump())
        settings = request.model_dump(exclude = {"script_folder", "da_document_folder", "title"})
        await complete_in_thread(self.store.create, graph, snapshots, output_folder, settings, operation_id = operation_id)
        log("Dependency profile extraction complete. Validated graph saved before summary generation.")
        exports = await complete_in_thread(self.save_draft_exports, graph)
        if not exports:
            log(self._export_warnings[(graph.id, graph.revision)])
        log(f"Analysis complete. Draft {graph.id}, revision {graph.revision}, is ready for review.")
        return graph

    def edit(self, draft_id: str, request: EditRequest, *, operation_id: str | None = None) -> GraphDocument:
        graph = self.store.update(draft_id, request.expected_revision,
                                  lambda current: apply_edits(current, request.operations),
                                  {"action": "edit", "operations": [operation.model_dump(exclude_unset = True) for operation in request.operations]},
                                  operation_id = operation_id)
        self.save_draft_exports(graph)
        return graph

    def import_diagram(self, draft_id: str, request: ImportRequest, *, operation_id: str | None = None) -> GraphDocument:
        digest = hashlib.sha256(request.xml.encode("utf-8")).hexdigest()
        graph = self.store.update(draft_id, request.expected_revision,
                                  lambda current: import_drawio(current, request.xml),
                                  {"action": "import_drawio", "diagram_sha256": digest}, operation_id = operation_id)
        self.save_draft_exports(graph)
        return graph

    async def suggest(self, draft_id: str, request: SuggestRequest, logger = None, *, chain = None,
                      operation_id: str | None = None) -> GraphDocument:
        original = self.require_revision(draft_id, request.expected_revision)
        request = saved_model_options(request, self.store.metadata(draft_id)["settings"])
        proposed = await suggest_relationships(
            original, self.store.snapshots(draft_id), model = request.model,
            suppressed = self.store.suppressed_edges(draft_id), max_concurrency = request.max_concurrency,
            timeout_seconds = request.timeout_seconds, logger = logger, chain = chain,
        )
        graph = await complete_in_thread(
            self.store.update, draft_id, request.expected_revision, lambda current: proposed,
            {"action": "llm_suggestions", "provider": request.model,
             "added_edge_count": len(proposed.edges) - len(original.edges)}, operation_id = operation_id,
        )
        await complete_in_thread(self.save_draft_exports, graph)
        return graph

    async def generate(self, draft_id: str, request: GenerateRequest, logger = None, *,
                       summary_chain = None, provider_identity: dict | None = None,
                       operation_id: str | None = None) -> dict:
        log = logger or (lambda message: None)
        graph = self.require_revision(draft_id, request.expected_revision)
        request = saved_model_options(request, self.store.metadata(draft_id)["settings"])
        review = review_report(graph)
        if review["proposed_edge_ids"] and not request.allow_proposed:
            raise ReviewRequired("Unconfirmed edges remain. Confirm or remove them before generating, or explicitly enable allow_proposed to retain visibly marked suggestions.")
        if review["has_analysis_errors"] and not request.acknowledge_incomplete:
            raise ReviewRequired("Some source analysis failed. Review the issues and set acknowledge_incomplete only if you want a visibly incomplete chart.")
        signature = topology_signature(graph)
        log(f"Generating from reviewed draft {draft_id}, revision {graph.revision}; analysis will not be rerun.")
        records = await enrich_summaries(
            graph, self.store.snapshots(draft_id), self.store, use_llm = request.use_llm, model = request.model,
            language = request.language, max_concurrency = request.max_concurrency,
            timeout_seconds = request.timeout_seconds, logger = log, chain = summary_chain, provider_identity = provider_identity,
        )
        summaries = {node_id: NarrativeSummary.model_validate(record["summary"]) for node_id, record in records.items()}
        statuses = {node_id: record["status"] for node_id, record in records.items()}
        if topology_signature(graph) != signature:
            raise ValueError("Summary enrichment changed graph topology; generation rejected.")
        log("Script summary generation complete. Rendering the reviewed graph.")
        rendered = await complete_in_thread(
            render_graph_html, graph, summaries = summaries, summary_statuses = statuses,
            summary_errors = {node_id: record["error"] for node_id, record in records.items() if record.get("error")},
        )
        if topology_signature(graph) != signature:
            raise ValueError("Rendering changed graph topology; generation rejected.")
        generation_id = "generation_" + uuid.uuid4().hex
        destination = self.store.artifact_root(draft_id) / "generations" / generation_id
        files = {
            "workflow_flowchart.html": rendered,
            "workflow_graph.json": graph.model_dump_json(indent = 2),
            "summaries.json": json_text({"draft_id": draft_id, "revision": graph.revision, "source_digest": graph.source_digest, "summaries": records}),
            "review.json": json_text(review),
        }
        manifest = {
            "schema_version": "1.0", "generation_id": generation_id, "draft_id": draft_id,
            "revision": graph.revision, "source_digest": graph.source_digest, "created_at": utc_now(),
            "output_directory": str(destination), "settings": request.model_dump(exclude = {"expected_revision"}),
            "summary_status_counts": dict(Counter(statuses.values())),
            "has_analysis_warnings": review["has_warnings"], "has_analysis_errors": review["has_analysis_errors"],
            "has_proposed_edges": bool(review["proposed_edge_ids"]),
            "artifacts": {name: {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                                 "url": f"/api/drafts/{draft_id}/generations/{generation_id}/{name}"}
                          for name, content in files.items()},
        }
        files["generation_manifest.json"] = json_text(manifest)
        writer = asyncio.create_task(asyncio.to_thread(_write_directory, destination, files))
        try:
            # Cancelling to_thread does not stop its worker. Finish the write
            # before cleaning it up, otherwise it can recreate an orphan later.
            await asyncio.shield(writer)
            # Edits made while the model was running must not publish stale HTML
            # as the latest result. Older successful generations stay accessible.
            await complete_in_thread(self.store.record_generation, draft_id, graph.revision, manifest,
                                     operation_id = operation_id)
        except BaseException:
            while not writer.done():
                try:
                    await asyncio.shield(writer)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if writer.done() and not writer.cancelled():
                writer.exception()  # Consume any failed writer exception.
            # A cancellation can arrive just after the database commit. Keep
            # files belonging to a registered generation; its receipt lets the
            # job recover success instead of retrying paid model work.
            try:
                committed = self.store.generation(draft_id, generation_id) == manifest
            except FileNotFoundError:
                committed = False
            if not committed and destination.exists():
                shutil.rmtree(destination)
            raise
        log(f"Generation complete. All {len(graph.edges)} reviewed edges preserved in revision {graph.revision}.")
        return manifest

    def describe(self, graph: GraphDocument, *, generation_id: str | None = None) -> dict:
        base = f"/api/drafts/{graph.id}"
        revision = f"?revision={graph.revision}"
        generation = (self.store.generation(graph.id, generation_id) if generation_id is not None
                      else self.store.latest_generation(graph.id, graph.revision))
        if generation and generation["revision"] != graph.revision:
            raise ValueError("The generation belongs to a different graph revision.")
        outputs = {
            "draft_diagram": f"{base}/export/draft.drawio{revision}",
            "draft_preview": f"{base}/export/draft.svg{revision}",
            "network": f"{base}/export/draft.json{revision}",
            "profiles": f"{base}/review{revision}",
            "review": f"{base}/review{revision}",
            "flowchart_spec": f"{base}/export/draft.json{revision}",
            "flowchart": None, "flowchart_download": None, "summaries": None, "da_document": None,
        }
        if generation:
            outputs["flowchart"] = generation["artifacts"]["workflow_flowchart.html"]["url"]
            outputs["flowchart_download"] = outputs["flowchart"] + "?download=1"
            outputs["summaries"] = generation["artifacts"]["summaries.json"]["url"]
        export_warning = self._export_warnings.get((graph.id, graph.revision))
        if export_warning is None:
            local_root = self.store.artifact_root(graph.id) / "revisions" / str(graph.revision)
            if not all((local_root / name).is_file() for name in ("draft.json", "draft.drawio", "draft.svg", "review.json")):
                export_warning = "The revision is saved, but local draft exports are unavailable. Download from the draft API or retry export."
        return {"draft_id": graph.id, "revision": graph.revision, "status": "generated" if generation else "draft",
                "title": graph.title, "project_root": graph.project_root,
                "output_folder": str(self.store.artifact_root(graph.id)), "settings": self.store.metadata(graph.id)["settings"],
                "graph": graph.model_dump(), "review": review_report(graph), "outputs": outputs,
                "generation": generation, "export_warning": export_warning,
                "message": "Draft ready for review." if not generation else "Flowchart generated from the reviewed revision."}

    def _verified_artifact(self, draft_id: str, generation_id: str, file_name: str) -> tuple[Path, bytes]:
        """
        Read one owned artifact and verify the exact bytes the browser receives.
        - Only artifacts registered to this draft/generation can be requested.
        - Resolve paths inside the expected generation directory before opening.
        - Validate the returned bytes, not a path that could be replaced between
          verification and a later HTTP response opening the file again.
        """
        manifest = self.store.generation(draft_id, generation_id)
        if file_name not in manifest["artifacts"] and file_name != "generation_manifest.json":
            raise FileNotFoundError("Unknown generated artifact.")
        root = (self.store.artifact_root(draft_id) / "generations" / generation_id).resolve()
        path = (root / file_name).resolve()
        if path.parent != root or not path.is_file():
            raise FileNotFoundError("This generated file is no longer in its saved output folder. Restore the moved file or open the saved draft and generate it again; the reviewed graph is still saved.")
        content = path.read_bytes()
        if file_name in manifest["artifacts"]:
            if hashlib.sha256(content).hexdigest() != manifest["artifacts"][file_name]["sha256"]:
                raise ValueError("Generated artifact has changed on disk. Regenerate from the saved graph.")
        elif json.loads(content.decode("utf-8")) != manifest:
            raise ValueError("Generation manifest has changed on disk. The database retains the authoritative manifest.")
        return path, content

    def artifact_path(self, draft_id: str, generation_id: str, file_name: str) -> Path:
        return self._verified_artifact(draft_id, generation_id, file_name)[0]

    def artifact_bytes(self, draft_id: str, generation_id: str, file_name: str) -> bytes:
        return self._verified_artifact(draft_id, generation_id, file_name)[1]

    def flowchart_download_header(self, draft_id: str, generation_id: str) -> str:
        # - Use the exact saved generation's title, even after another revision
        #   or another project becomes the currently selected item in the UI.
        # - This changes only the suggested browser download name, never disk
        #   paths, canonical links, checksums or existing report contents.
        manifest = self.store.generation(draft_id, generation_id)
        graph = self.store.load(draft_id, manifest["revision"])
        return flowchart_attachment(graph.title)


def default_service() -> WorkflowService:
    """Create the local service with private storage outside public file mounts.

    - ``DA_WORKFLOW_STORE`` supports isolated tests and an explicit custom location.
    - The default remains inside the backend project folder for launcher portability.
    - Constructing the service opens local SQLite only; it does not analyze sources
      or initialize a model provider.
    """

    directory = os.environ.get("DA_WORKFLOW_STORE") or Path(__file__).parent / ".workflow_store"
    return WorkflowService(WorkflowStore(directory))
