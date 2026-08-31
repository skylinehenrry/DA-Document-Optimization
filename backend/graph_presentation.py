"""Small, reversible presentation choices for the reviewed dependency graph.

- File cards use basenames; stable IDs and full source paths remain authoritative.
- Repeated direct relationships share one visible connection by default.
- Grouping never invents transitive links, removes evidence, or edits the graph.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import ntpath
import re
from urllib.parse import urlsplit

from .graph_models import GraphDocument, GraphEdge, GraphNode, stable_id


def file_card_label(node: GraphNode) -> str:
    """Return a short file label consistently on macOS and Windows.

    - Source files use their real filename, including the extension.
    - File resources may contain Windows, UNC, POSIX, or URL paths.
    - Process annotations and table/module names are not mistaken for paths.
    - The underlying label and source identity are never rewritten.
    """
    if node.kind == "script" or (node.kind == "module" and node.source_path):
        value = node.source_path or node.label
    elif node.kind == "file":
        value = str(node.details.get("normalized_path") or node.label)
    else:
        return node.label
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        try:
            value = urlsplit(value).path or value
        except ValueError:
            # - Malformed resource text is still display data, not a URL to open.
            # - Keep the safe basename fallback instead of breaking generation.
            pass
    return ntpath.basename(value.rstrip("/\\")) or node.label


@dataclass(frozen=True)
class DirectConnection:
    """A display group whose members remain individually reviewable."""

    id: str
    source: str
    target: str
    members: tuple[GraphEdge, ...]

    @property
    def status(self) -> str:
        # - One uncertain member makes the whole compact arrow visibly uncertain.
        # - Choosing a confirmed representative must never hide a proposal.
        return "proposed" if any(edge.status == "proposed" for edge in self.members) else "confirmed"

    @property
    def label(self) -> str:
        order = {"imports": 0, "calls": 1, "depends_on": 2}
        kinds = sorted({edge.kind for edge in self.members}, key=lambda kind: (order.get(kind, 3), kind))
        return " / ".join(kind.replace("_", " ") for kind in kinds) + f" · {len(self.members)} references"


def direct_connections(graph: GraphDocument) -> tuple[DirectConnection, ...]:
    """Group repeated direct dependencies without reducing actual topology.

    - Imports and calls between the same two source modules share a code arrow.
    - Other relationships group only when their endpoints AND kinds match.
    - Reverse arrows, self-loops, branches, and explicit shortcuts remain present.
    - A → B and B → C never generate an implied A → C connection.
    - Every original edge ID, condition, review note, and evidence record survives.
    """
    nodes = {node.id: node for node in graph.nodes}
    groups: dict[tuple[str, str, str], list[GraphEdge]] = defaultdict(list)
    for edge in graph.edges:
        code_dependency = (
            edge.kind in {"imports", "calls", "depends_on"}
            and nodes[edge.source].kind in {"script", "module"}
            and nodes[edge.target].kind in {"script", "module"}
        )
        key = (edge.source, edge.target, "code" if code_dependency else edge.kind)
        groups[key].append(edge)
    return tuple(
        DirectConnection(
            id=stable_id("connection", source, target, kind), source=source, target=target,
            members=tuple(sorted(members, key=lambda edge: edge.id)),
        )
        for (source, target, kind), members in sorted(groups.items())
    )
