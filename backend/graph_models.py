"""Define the versioned contract shared by analysis, review, storage, and rendering.

- ``GraphDocument`` is the authoritative topology; summaries, SVG, draw.io, and
  interactive HTML are reversible projections of that saved document.
- Stable identifiers preserve full path and resource identity without exposing
  display labels as keys.
- Evidence records exact source locations and extractor ownership for each link.
- Strict models reject unknown fields, invalid endpoints, duplicate identifiers,
  non-finite positions, and malformed revisions before data reaches storage.
- Provider-specific response objects remain outside this module so saved drafts
  stay portable between local and cloud model configurations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


NodeKind = Literal["script", "file", "table", "database", "api", "module", "process", "decision", "unknown"]
EdgeKind = Literal["reads", "writes", "imports", "calls", "depends_on", "control_flow", "unknown"]
ScriptType = Literal["python", "sql", "alteryx", "bat"]
IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"


def stable_id(prefix: str, *parts: str) -> str:
    """Hash the full identity; do not collapse punctuation, Unicode or paths."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def utc_now() -> str:
    """Return an explicit UTC timestamp suitable for durable JSON records."""

    return datetime.now(timezone.utc).isoformat()


# Shared validation base for persisted and API-facing structures.
# - Forbid unexpected fields so a misspelled frontend property cannot be ignored.
# - Reject NaN and infinity because they are invalid in portable JSON and SVG.
class StrictModel(BaseModel):
    model_config = ConfigDict(extra = "forbid", allow_inf_nan = False)


class Evidence(StrictModel):
    """Source-owned support for a node, edge, or diagnostic statement."""

    source_path: str
    line_start: int | None = Field(default = None, ge = 1)
    line_end: int | None = Field(default = None, ge = 1)
    excerpt: str = ""
    extractor: str
    note: str | None = None

    @model_validator(mode = "after")
    def valid_lines(self) -> Evidence:
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("Evidence line_end must not precede line_start.")
        return self


class Position(StrictModel):
    """User-reviewable card coordinates shared by all diagram projections."""

    x: float = Field(ge = -1_000_000, le = 1_000_000)
    y: float = Field(ge = -1_000_000, le = 1_000_000)


class SourceFile(StrictModel):
    """One discovered source and the truthful outcome of its offline analysis."""

    path: str = Field(min_length = 1)
    sha256: str = Field(pattern = r"^[a-f0-9]{64}$")
    script_type: ScriptType
    size_bytes: int = Field(ge = 0)
    encoding: str = "utf-8"
    status: Literal["parsed", "partial", "failed"] = "parsed"


class GraphNode(StrictModel):
    """A source, resource, or user-added process displayed in the workflow."""

    id: str = Field(pattern = IDENTIFIER)
    label: str = Field(min_length = 1, max_length = 1000)
    kind: NodeKind
    source_path: str | None = None
    script_type: ScriptType | None = None
    resource_key: str | None = None
    position: Position | None = None
    details: dict[str, Any] = Field(default_factory = dict)


class GraphEdge(StrictModel):
    """One directed, typed relationship with provenance and review status."""

    id: str = Field(pattern = IDENTIFIER)
    source: str = Field(pattern = IDENTIFIER)
    target: str = Field(pattern = IDENTIFIER)
    kind: EdgeKind
    label: str | None = Field(default = None, max_length = 2000)
    origin: Literal["static", "llm", "user"] = "static"
    status: Literal["confirmed", "proposed"] = "confirmed"
    evidence: list[Evidence] = Field(default_factory = list)
    condition: str | None = None
    review_note: str | None = None


class GraphIssue(StrictModel):
    """A visible limitation or failure that must survive review and generation."""

    id: str = Field(pattern = IDENTIFIER)
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    node_ids: list[str] = Field(default_factory = list)
    edge_ids: list[str] = Field(default_factory = list)
    evidence: list[Evidence] = Field(default_factory = list)


class GraphDocument(StrictModel):
    """The complete saved graph, source manifest, revision, and diagnostics."""

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern = IDENTIFIER)
    revision: int = Field(default = 1, ge = 1)
    title: str = Field(default = "Workflow Flowchart", min_length = 1, max_length = 1000)
    project_root: str
    source_digest: str = Field(pattern = r"^[a-f0-9]{64}$")
    created_at: str = Field(default_factory = utc_now)
    updated_at: str = Field(default_factory = utc_now)
    sources: list[SourceFile] = Field(default_factory = list)
    nodes: list[GraphNode] = Field(default_factory = list)
    edges: list[GraphEdge] = Field(default_factory = list)
    issues: list[GraphIssue] = Field(default_factory = list)
    analysis_options: dict[str, Any] = Field(default_factory = dict)

    @model_validator(mode = "after")
    def valid_graph(self) -> GraphDocument:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        paths = [source.path for source in self.sources]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Duplicate node IDs are not allowed.")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("Duplicate edge IDs are not allowed.")
        if set(node_ids) & set(edge_ids):
            raise ValueError("Node and edge IDs must not overlap.")
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate source paths are not allowed.")
        known_nodes, known_paths = set(node_ids), set(paths)
        for path in paths:
            if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/") or ":" in path:
                raise ValueError(f"Source paths must be relative to the project root: {path}")
        for node in self.nodes:
            if node.source_path is not None and node.source_path not in known_paths:
                raise ValueError(f"Unknown source path on node {node.id}: {node.source_path}")
        for edge in self.edges:
            if edge.source not in known_nodes or edge.target not in known_nodes:
                raise ValueError(f"Dangling edge {edge.id}: both endpoints must exist.")
            for evidence in edge.evidence:
                if evidence.source_path not in known_paths:
                    raise ValueError(f"Unknown evidence source on edge {edge.id}.")
        return self


class NarrativeSummary(StrictModel):
    """The only LLM-generated fields accepted by final generation."""

    high_level: str = Field(min_length = 1, max_length = 8000)
    detailed: str = Field(min_length = 1, max_length = 40000)


class ProjectSummary(StrictModel):
    """Project-level narrative assembled from reviewed topology and script summaries.

    - Narrative fields explain the project without carrying node or edge mutations.
    - Inputs and outputs remain short display lists rather than alternate resources.
    - Limitations preserve uncertainty from analysis and summary fallback.
    """

    overview: str = Field(min_length = 1, max_length = 12000)
    processing_flow: str = Field(min_length = 1, max_length = 24000)
    key_inputs: list[str] = Field(default_factory = list, max_length = 50)
    key_outputs: list[str] = Field(default_factory = list, max_length = 50)
    limitations: list[str] = Field(default_factory = list, max_length = 50)


def topology_signature(graph: GraphDocument) -> tuple:
    """A renderer/enricher must preserve IDs, endpoint direction and edge type."""
    return (
        tuple(sorted((node.id, node.kind, node.source_path) for node in graph.nodes)),
        tuple(sorted((edge.id, edge.source, edge.target, edge.kind, edge.status) for edge in graph.edges)),
    )
