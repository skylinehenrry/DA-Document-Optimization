"""Atomic graph edit operations suitable for any future visual editor."""

from __future__ import annotations

from typing import Annotated, Literal
import uuid

from pydantic import Field

from .graph_models import EdgeKind, GraphDocument, GraphEdge, GraphNode, IDENTIFIER, Position, StrictModel


class AddNode(StrictModel):
    op: Literal["add_node"]
    node: GraphNode


class UpdateNode(StrictModel):
    op: Literal["update_node"]
    id: str
    label: str | None = Field(default=None, min_length=1, max_length=1000)
    position: Position | None = None


class RemoveNode(StrictModel):
    op: Literal["remove_node"]
    id: str


class NewEdge(StrictModel):
    id: str = Field(default_factory=lambda: "edge_" + uuid.uuid4().hex, pattern=IDENTIFIER)
    source: str = Field(pattern=IDENTIFIER)
    target: str = Field(pattern=IDENTIFIER)
    kind: EdgeKind = "unknown"
    label: str | None = Field(default=None, max_length=2000)
    review_note: str | None = None


class AddEdge(StrictModel):
    op: Literal["add_edge"]
    edge: NewEdge


class UpdateEdge(StrictModel):
    op: Literal["update_edge"]
    id: str
    source: str | None = Field(default=None, pattern=IDENTIFIER)
    target: str | None = Field(default=None, pattern=IDENTIFIER)
    kind: EdgeKind | None = None
    label: str | None = Field(default=None, max_length=2000)
    status: Literal["confirmed", "proposed"] | None = None
    review_note: str | None = None


class RemoveEdge(StrictModel):
    op: Literal["remove_edge"]
    id: str


EditOperation = Annotated[AddNode | UpdateNode | RemoveNode | AddEdge | UpdateEdge | RemoveEdge, Field(discriminator="op")]


class EditRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    operations: list[EditOperation] = Field(min_length=1, max_length=1000)


def apply_edits(graph: GraphDocument, operations: list[EditOperation]) -> GraphDocument:
    nodes = {node.id: node.model_copy(deep=True) for node in graph.nodes}
    edges = {edge.id: edge.model_copy(deep=True) for edge in graph.edges}
    for operation in operations:
        if isinstance(operation, AddNode):
            node = operation.node
            if node.id in nodes or node.id in edges:
                raise ValueError(f"ID already exists: {node.id}")
            if node.source_path is not None or node.script_type is not None:
                raise ValueError("New manual nodes cannot impersonate source scripts; retain original script nodes for source summaries.")
            nodes[node.id] = node
        elif isinstance(operation, UpdateNode):
            if operation.id not in nodes:
                raise ValueError(f"Node not found: {operation.id}")
            changes = operation.model_dump(exclude_unset=True, exclude={"op", "id"})
            nodes[operation.id] = GraphNode.model_validate({**nodes[operation.id].model_dump(), **changes})
        elif isinstance(operation, RemoveNode):
            if operation.id not in nodes:
                raise ValueError(f"Node not found: {operation.id}")
            del nodes[operation.id]
            edges = {key: edge for key, edge in edges.items() if operation.id not in (edge.source, edge.target)}
        elif isinstance(operation, AddEdge):
            edge = operation.edge
            if edge.id in edges or edge.id in nodes:
                raise ValueError(f"ID already exists: {edge.id}")
            edges[edge.id] = GraphEdge(**edge.model_dump(), origin="user", status="confirmed")
        elif isinstance(operation, UpdateEdge):
            if operation.id not in edges:
                raise ValueError(f"Edge not found: {operation.id}")
            original = edges[operation.id]
            changes = operation.model_dump(exclude_unset=True, exclude={"op", "id"})
            if any(name in changes and changes[name] is None for name in ("source", "target", "kind", "status")):
                raise ValueError("Edge source, target, kind and status cannot be null.")
            rewired = any(name in changes and changes[name] != getattr(original, name) for name in ("source", "target", "kind"))
            changes["origin"] = "user"
            changes.setdefault("status", "confirmed" if rewired else original.status)
            if rewired:
                # Citations supporting an old edge are not evidence for new endpoints.
                changes.update(evidence=[], condition=None)
                changes.setdefault("review_note", f"User changed {original.source} → {original.target} ({original.kind}); original evidence is retained in revision history.")
            edges[operation.id] = GraphEdge.model_validate({**original.model_dump(), **changes})
        elif isinstance(operation, RemoveEdge):
            if operation.id not in edges:
                raise ValueError(f"Edge not found: {operation.id}")
            del edges[operation.id]
    payload = graph.model_dump()
    payload.update(nodes=[node.model_dump() for node in nodes.values()], edges=[edge.model_dump() for edge in edges.values()])
    # Validate the entire batch, so failed reconnects cannot leave partial edits.
    return GraphDocument.model_validate(payload)
