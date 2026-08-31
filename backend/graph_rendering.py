"""Deterministic projections of the reviewed graph; no analysis or LLM calls.

Drawing ranks are derived from strongly connected components. They are a layout
aid, not a claim about execution order. The original graph is embedded unchanged
in the HTML so presentation and narrative enrichment cannot become topology.
"""

from __future__ import annotations

import base64
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
import heapq
import html
import json
import math
from pathlib import Path
import re
from typing import Iterable

from .graph_models import GraphDocument, GraphEdge, GraphNode, NarrativeSummary, Position
from .graph_diagnostics import graph_diagnostics
from .graph_presentation import DirectConnection, direct_connections, file_card_label


NODE_WIDTH = 300
NODE_HEIGHT = 82
HORIZONTAL_GAP = 160
VERTICAL_GAP = 92
MARGIN = 64
_CLEARANCE = 16
_STUB_LENGTH = 28
_BACKEND_DIR = Path(__file__).resolve().parent
_TEMPLATE_PATH = _BACKEND_DIR / "templates" / "workflow_flowchart.html"
_ICON_DIR = _BACKEND_DIR / "assets" / "flowchart_icons"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
Point = tuple[float, float]
Rectangle = tuple[float, float, float, float]


def _validated(graph: GraphDocument) -> GraphDocument:
    # Lists on a Pydantic instance may have been edited after construction. Do
    # not let such edits bypass endpoint validation and silently hide an edge.
    return GraphDocument.model_validate(graph.model_dump(mode="json"))


def _drawing_layers(graph: GraphDocument) -> dict[str, int]:
    """Condense SCCs before ranking, including descendants of cyclic nodes."""
    adjacency = {node.id: set() for node in graph.nodes}
    reverse = {node.id: set() for node in graph.nodes}
    for edge in graph.edges:
        adjacency[edge.source].add(edge.target)
        reverse[edge.target].add(edge.source)

    # Iterative Kosaraju avoids Python's recursion limit on large projects.
    visited: set[str] = set()
    finished: list[str] = []
    for start in sorted(adjacency):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(adjacency[start])))]
        while stack:
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                finished.append(current)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(adjacency[child]))))

    component_of: dict[str, int] = {}
    components: list[tuple[str, ...]] = []
    for start in reversed(finished):
        if start in component_of:
            continue
        index = len(components)
        members: list[str] = []
        stack = [start]
        component_of[start] = index
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbor in sorted(reverse[current], reverse=True):
                if neighbor not in component_of:
                    component_of[neighbor] = index
                    stack.append(neighbor)
        components.append(tuple(sorted(members)))

    outgoing = {index: set() for index in range(len(components))}
    incoming = {index: 0 for index in outgoing}
    for edge in graph.edges:
        source, target = component_of[edge.source], component_of[edge.target]
        if source != target and target not in outgoing[source]:
            outgoing[source].add(target)
            incoming[target] += 1
    ready = [(components[index], index) for index, count in incoming.items() if count == 0]
    heapq.heapify(ready)
    ranks = {index: 0 for index in outgoing}
    while ready:
        _, current = heapq.heappop(ready)
        for target in sorted(outgoing[current], key=components.__getitem__):
            ranks[target] = max(ranks[target], ranks[current] + 1)
            incoming[target] -= 1
            if incoming[target] == 0:
                heapq.heappush(ready, (components[target], target))
    return {node_id: ranks[index] for node_id, index in component_of.items()}


def _overlaps(first: Position, second: Position, padding: float = 0) -> bool:
    return (
        first.x < second.x + NODE_WIDTH + padding
        and second.x < first.x + NODE_WIDTH + padding
        and first.y < second.y + NODE_HEIGHT + padding
        and second.y < first.y + NODE_HEIGHT + padding
    )


def _layout(graph: GraphDocument) -> dict[str, Position]:
    layers = _drawing_layers(graph)
    nodes = sorted(graph.nodes, key=lambda node: (layers[node.id], node.source_path or "", node.label, node.id))
    # Saved positions are constraints. Missing positions are placed around them;
    # even negative saved coordinates are retained until the viewport is built.
    positions = {
        node.id: node.position.model_copy(deep=True)
        for node in nodes
        if node.position is not None
    }
    next_y: dict[int, float] = {}
    for node in nodes:
        if node.id in positions:
            continue
        layer = layers[node.id]
        position = Position(x=MARGIN + layer * (NODE_WIDTH + HORIZONTAL_GAP), y=next_y.get(layer, MARGIN))
        while True:
            collisions = [other for other in positions.values() if _overlaps(position, other, _CLEARANCE)]
            if not collisions:
                break
            position = Position(x=position.x, y=max(other.y for other in collisions) + NODE_HEIGHT + VERTICAL_GAP)
        positions[node.id] = position
        next_y[layer] = position.y + NODE_HEIGHT + VERTICAL_GAP
    return positions


def layout_graph(graph: GraphDocument) -> dict[str, Position]:
    """Return deterministic coordinates without changing the graph or saved layout.

    Existing coordinates remain exact, including negative coordinates. Renderers
    apply a uniform viewport translation if needed to include every arrow. Graph
    exchange formats can use this function without moving the user's drawing.
    """
    return _layout(_validated(graph))


def _escape(value: object) -> str:
    return html.escape(_INVALID_XML.sub("\ufffd", str(value)), quote=True)


def _number(value: float) -> str:
    # Do not round saved coordinates: all cards must receive the same viewport
    # translation, including drawings with subpixel positions.
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _graph_json(graph: GraphDocument) -> str:
    # ASCII escaping also preserves otherwise-invalid XML control characters in
    # the original data, without putting those characters into the document.
    payload = json.dumps(graph.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, allow_nan=False)
    return payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _segment_clear(start: Point, end: Point, obstacles: Iterable[Rectangle]) -> bool:
    if start == end:
        return True
    if start[1] == end[1]:
        left, right = sorted((start[0], end[0]))
        return not any(top < start[1] < bottom and max(left, x1) < min(right, x2) for x1, top, x2, bottom in obstacles)
    if start[0] == end[0]:
        top, bottom = sorted((start[1], end[1]))
        return not any(left < start[0] < right and max(top, y1) < min(bottom, y2) for left, y1, right, y2 in obstacles)
    return False


def _simplify(points: Iterable[Point]) -> tuple[Point, ...]:
    result: list[Point] = []
    for point in points:
        if result and point == result[-1]:
            continue
        if len(result) > 1:
            a, b = result[-2:]
            # Remove only points on the segment; preserve U-turns.
            collinear = a[0] == b[0] == point[0] or a[1] == b[1] == point[1]
            between = min(a[0], point[0]) <= b[0] <= max(a[0], point[0]) and min(a[1], point[1]) <= b[1] <= max(a[1], point[1])
            if collinear and between:
                result.pop()
        result.append(point)
    return tuple(result)


def _route_cost(points: tuple[Point, ...]) -> float:
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:])) + max(0, len(points) - 2) * 24


class _SegmentIndex:
    """Union of occupied intervals per axis, without scanning unrelated edges.

    Perpendicular crossings are allowed. Positive-length collinear overlaps
    make independent relationships look joined, so routing avoids those first.
    """

    def __init__(self) -> None:
        self.horizontal: dict[float, list[tuple[float, float]]] = {}
        self.vertical: dict[float, list[tuple[float, float]]] = {}

    def _interval(self, start: Point, end: Point):
        if start[1] == end[1]:
            return self.horizontal, start[1], *sorted((start[0], end[0]))
        if start[0] == end[0]:
            return self.vertical, start[0], *sorted((start[1], end[1]))
        raise ValueError("Connector segments must be orthogonal")

    def add(self, points: Iterable[Point]) -> None:
        points = tuple(points)
        for start, end in zip(points, points[1:]):
            if start == end:
                continue
            axes, coordinate, low, high = self._interval(start, end)
            intervals = axes.setdefault(coordinate, [])
            index = bisect_left(intervals, (low, -math.inf))
            if index and intervals[index - 1][1] >= low:
                index -= 1
            stop = index
            while stop < len(intervals) and intervals[stop][0] <= high:
                low = min(low, intervals[stop][0])
                high = max(high, intervals[stop][1])
                stop += 1
            intervals[index:stop] = [(low, high)]

    def overlap(self, start: Point, end: Point) -> float:
        if start == end:
            return 0
        axes, coordinate, low, high = self._interval(start, end)
        intervals = axes.get(coordinate, [])
        index = bisect_left(intervals, (low, -math.inf))
        if index and intervals[index - 1][1] > low:
            index -= 1
        length = 0.0
        while index < len(intervals) and intervals[index][0] < high:
            first, last = intervals[index]
            length += max(0, min(high, last) - max(low, first))
            index += 1
        return length

    def shared_length(self, points: tuple[Point, ...]) -> float:
        return sum(self.overlap(start, end) for start, end in zip(points, points[1:]))


def _grid_route(
    start: Point, end: Point, obstacles: list[Rectangle], occupied: _SegmentIndex,
    extra_xs: Iterable[float], extra_ys: Iterable[float],
) -> tuple[Point, ...] | None:
    """Bounded rectilinear A* fallback for cards blocking a simple dogleg."""
    xs = sorted({start[0], end[0], *extra_xs, *(x for rectangle in obstacles for x in (rectangle[0], rectangle[2]))})
    ys = sorted({start[1], end[1], *extra_ys, *(y for rectangle in obstacles for y in (rectangle[1], rectangle[3]))})
    source = (xs.index(start[0]), ys.index(start[1]), 0)
    target = (xs.index(end[0]), ys.index(end[1]))
    distance = {source: 0.0}
    previous: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    queue = [(abs(start[0] - end[0]) + abs(start[1] - end[1]), 0.0, source)]
    clear_cache: dict[tuple[Point, Point], bool] = {}
    explored = 0
    while queue and explored < 20000:
        _, cost, current = heapq.heappop(queue)
        if cost != distance.get(current):
            continue
        explored += 1
        ix, iy, direction = current
        if (ix, iy) == target:
            route = [(xs[ix], ys[iy])]
            while current in previous:
                current = previous[current]
                route.append((xs[current[0]], ys[current[1]]))
            return _simplify(reversed(route))
        for nx, ny, next_direction in ((ix - 1, iy, 1), (ix + 1, iy, 1), (ix, iy - 1, 2), (ix, iy + 1, 2)):
            if not (0 <= nx < len(xs) and 0 <= ny < len(ys)):
                continue
            point, neighbor = (xs[ix], ys[iy]), (xs[nx], ys[ny])
            key = tuple(sorted((point, neighbor)))
            if key not in clear_cache:
                clear_cache[key] = _segment_clear(point, neighbor, obstacles)
            if not clear_cache[key]:
                continue
            step = abs(point[0] - neighbor[0]) + abs(point[1] - neighbor[1])
            next_cost = cost + step + (24 if direction and direction != next_direction else 0) + 1024 * occupied.overlap(point, neighbor)
            next_state = (nx, ny, next_direction)
            if next_cost < distance.get(next_state, math.inf):
                distance[next_state] = next_cost
                previous[next_state] = current
                estimate = abs(neighbor[0] - end[0]) + abs(neighbor[1] - end[1])
                heapq.heappush(queue, (next_cost + estimate, next_cost, next_state))
    return None


def _middle_route(start: Point, end: Point, obstacles: list[Rectangle], lane: float, occupied: _SegmentIndex) -> tuple[tuple[Point, ...], bool]:
    left = min(rectangle[0] for rectangle in obstacles)
    top = min(rectangle[1] for rectangle in obstacles)
    right = max(rectangle[2] for rectangle in obstacles)
    bottom = max(rectangle[3] for rectangle in obstacles)
    candidate_routes = [[start, end], [start, (end[0], start[1]), end], [start, (start[0], end[1]), end]]
    xs = ((start[0] + end[0]) / 2, start[0] + lane, end[0] - lane,
          (3 * start[0] + end[0]) / 4, (start[0] + 3 * end[0]) / 4, left - lane, right + lane)
    ys = ((start[1] + end[1]) / 2, start[1] - lane, end[1] + lane,
          (3 * start[1] + end[1]) / 4, (start[1] + 3 * end[1]) / 4, top - lane, bottom + lane)
    for x in xs:
        candidate_routes.append([start, (x, start[1]), (x, end[1]), end])
    for y in ys:
        candidate_routes.append([start, (start[0], y), (end[0], y), end])
    candidates = [_simplify(points) for points in candidate_routes]
    clear = [points for points in candidates if all(_segment_clear(a, b, obstacles) for a, b in zip(points, points[1:]))]
    def score(points):
        return occupied.shared_length(points), _route_cost(points), points

    best = min(clear, key=score) if clear else None
    if best is not None and occupied.shared_length(best) == 0:
        return best, False
    routed = _grid_route(start, end, obstacles, occupied, xs, ys)
    if routed is not None:
        return min((best, routed), key=score) if best is not None else routed, False
    if best is not None:
        return best, False
    # Overlapping user-positioned cards may leave no free port. Retain and mark
    # the relationship; never silently omit it or move individual saved cards.
    return _simplify([start, (start[0], bottom + lane), (end[0], bottom + lane), end]), True


@dataclass(frozen=True)
class _Route:
    edge: GraphEdge
    points: tuple[Point, ...]
    obstructed: bool = False


@dataclass(frozen=True)
class _Drawing:
    positions: dict[str, Point]
    routes: tuple[_Route, ...]
    width: int
    height: int
    overlaps: bool
    shared_routes: bool = False


def _drawing(graph: GraphDocument) -> _Drawing:
    positions = _layout(graph)
    if not positions:
        return _Drawing({}, (), 960, 280, False)
    obstacles = [
        (position.x - _CLEARANCE, position.y - _CLEARANCE, position.x + NODE_WIDTH + _CLEARANCE, position.y + NODE_HEIGHT + _CLEARANCE)
        for position in positions.values()
    ]
    outgoing: dict[str, list[GraphEdge]] = {node_id: [] for node_id in positions}
    incoming: dict[str, list[GraphEdge]] = {node_id: [] for node_id in positions}
    loops: dict[str, list[GraphEdge]] = {node_id: [] for node_id in positions}
    for edge in sorted(graph.edges, key=lambda edge: edge.id):
        outgoing[edge.source].append(edge)
        (loops if edge.source == edge.target else incoming)[edge.target].append(edge)

    def side_offset(index: int, count: int, size: int) -> float:
        return size / 2 if count == 1 else 18 + index * (size - 36) / (count - 1)

    source_ports: dict[str, Point] = {}
    target_ports: dict[str, Point] = {}
    for node_id, edges in outgoing.items():
        position = positions[node_id]
        for index, edge in enumerate(edges):
            source_ports[edge.id] = (position.x + NODE_WIDTH, position.y + side_offset(index, len(edges), NODE_HEIGHT))
    for node_id, edges in incoming.items():
        position = positions[node_id]
        for index, edge in enumerate(edges):
            target_ports[edge.id] = (position.x, position.y + side_offset(index, len(edges), NODE_HEIGHT))
    for node_id, edges in loops.items():
        position = positions[node_id]
        for index, edge in enumerate(edges):
            target_ports[edge.id] = (position.x + side_offset(index, len(edges), NODE_WIDTH), position.y)

    # Reserve every fixed port segment before routing middles, including ports
    # of edges drawn later. This prevents an early path from occupying a later
    # edge's only way out of its card.
    occupied = _SegmentIndex()
    stubs: dict[str, tuple[Point, Point]] = {}
    for edge in sorted(graph.edges, key=lambda edge: edge.id):
        start, end = source_ports[edge.id], target_ports[edge.id]
        start_stub = (start[0] + _STUB_LENGTH, start[1])
        end_stub = (end[0], end[1] - _STUB_LENGTH) if edge.source == edge.target else (end[0] - _STUB_LENGTH, end[1])
        stubs[edge.id] = start_stub, end_stub
        occupied.add((start, start_stub))
        occupied.add((end_stub, end))

    routes: list[_Route] = []
    rendered_segments = _SegmentIndex()
    shared_routes = False
    for index, edge in enumerate(sorted(graph.edges, key=lambda edge: edge.id)):
        start, end = source_ports[edge.id], target_ports[edge.id]
        start_stub, end_stub = stubs[edge.id]
        middle, obstructed = _middle_route(start_stub, end_stub, obstacles, 24 + (index % 8) * 8, occupied)
        # Stub segments are allowed to traverse the padding of their own card,
        # but not another actual card. This detects blocked ports after edits.
        other_rectangles = [
            (position.x, position.y, position.x + NODE_WIDTH, position.y + NODE_HEIGHT)
            for node_id, position in positions.items()
            if node_id not in {edge.source, edge.target}
        ]
        obstructed = obstructed or not _segment_clear(start, start_stub, other_rectangles) or not _segment_clear(end_stub, end, other_rectangles)
        points = _simplify([start, *middle, end])
        shared_routes = shared_routes or rendered_segments.shared_length(points) > 0
        rendered_segments.add(points)
        occupied.add(points)
        routes.append(_Route(edge, points, obstructed))

    # Only the viewport is translated. Relative coordinates, saved positions and
    # canonical graph data are never changed by rendering.
    all_points = [(position.x, position.y) for position in positions.values()]
    all_points.extend(point for route in routes for point in route.points)
    min_x = min(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    dx = MARGIN - min_x if min_x < 24 else 0
    dy = MARGIN - min_y if min_y < 24 else 0
    shifted = {node_id: (position.x + dx, position.y + dy) for node_id, position in positions.items()}
    shifted_routes = tuple(_Route(route.edge, tuple((x + dx, y + dy) for x, y in route.points), route.obstructed) for route in routes)
    max_x = max([point[0] + NODE_WIDTH for point in shifted.values()] + [point[0] for route in shifted_routes for point in route.points])
    max_y = max([point[1] + NODE_HEIGHT for point in shifted.values()] + [point[1] for route in shifted_routes for point in route.points])
    values = list(positions.values())
    overlaps = any(_overlaps(first, second) for index, first in enumerate(values) for second in values[index + 1:])
    return _Drawing(shifted, shifted_routes, max(960, math.ceil(max_x + MARGIN)), max(280, math.ceil(max_y + MARGIN)), overlaps, shared_routes)


def _edge_description(edge: GraphEdge, nodes: dict[str, GraphNode]) -> str:
    lines = [f"{nodes[edge.source].label} → {nodes[edge.target].label}", f"{edge.kind} · {edge.status} · {edge.origin}"]
    if edge.label:
        lines.append(edge.label)
    if edge.condition:
        lines.append(f"Condition: {edge.condition}")
    if edge.review_note:
        lines.append(f"Review: {edge.review_note}")
    for evidence in edge.evidence:
        location = evidence.source_path
        if evidence.line_start is not None:
            location += f":{evidence.line_start}"
            if evidence.line_end is not None and evidence.line_end != evidence.line_start:
                location += f"–{evidence.line_end}"
        lines.append(f"{location} ({evidence.extractor})")
        if evidence.excerpt:
            lines.append(evidence.excerpt)
        if evidence.note:
            lines.append(evidence.note)
    if not edge.evidence and edge.origin != "user":
        lines.append("No source evidence attached; review this relationship.")
    return "\n".join(lines)


def _edge_svg(route: _Route, nodes: dict[str, GraphNode], static: bool = False,
              connection: DirectConnection | None = None) -> str:
    edge = route.edge
    path = " ".join(("M" if index == 0 else "L") + _number(x) + " " + _number(y) for index, (x, y) in enumerate(route.points))
    segments = list(zip(route.points, route.points[1:]))
    first, last = max(segments, key=lambda pair: abs(pair[0][0] - pair[1][0]) + abs(pair[0][1] - pair[1][1]))
    label_x, label_y = (first[0] + last[0]) / 2, (first[1] + last[1]) / 2 - 8
    label = edge.label or edge.kind.replace("_", " ")
    if edge.condition:
        label += f" · {edge.condition}"
    display_label = label if len(label) <= 54 else label[:51] + "…"
    description = _edge_description(edge, nodes)
    group_attrs = ""
    group_class = ""
    compact_label = ""
    label_class = "edge-label" if static else "edge-tooltip-text"
    if connection is not None and len(connection.members) > 1:
        # - Render every canonical edge so the full view retains exact identities.
        # - In the compact view only the first member's route is visible.
        # - Its tooltip includes all members, and its colour includes uncertainty.
        primary = edge.id == connection.members[0].id
        group_class = " connection-primary" if primary else " connection-member"
        group_attrs = (
            f' data-connection-id="{_escape(connection.id)}"'
            f' data-connection-status="{connection.status}"'
            f' data-member-ids="{_escape(json.dumps([member.id for member in connection.members]))}"'
        )
        if primary:
            description = "Direct connection · " + connection.label + "\n\n" + "\n\n".join(
                f"Relationship {member.id}\n{_edge_description(member, nodes)}" for member in connection.members
            )
            compact_label = (
                f'<text class="{label_class} connection-label" x="{_number(label_x)}" '
                f'y="{_number(label_y)}" text-anchor="middle">{_escape(connection.label)}</text>'
            )
            label_class += " relationship-label"
    classes = "edge-path edge-low" if edge.status == "proposed" else "edge-path"
    return (
        f'<g class="edge{group_class}" id="edge-{_escape(edge.id)}" data-edge-id="{_escape(edge.id)}" '
        f'data-source="{_escape(edge.source)}" data-target="{_escape(edge.target)}" data-kind="{_escape(edge.kind)}" '
        f'data-origin="{_escape(edge.origin)}" data-status="{_escape(edge.status)}" data-route-status="{"obstructed" if route.obstructed else "clear"}"{group_attrs}>'
        f'<title>{_escape(description)}</title>'
        # A background halo makes crossings read as separate paths rather than
        # junctions. It follows the edge when hover brings that edge to the top.
        f'<path class="edge-separation" d="{path}"/><path class="{classes}" d="{path}"/>'
        + ("" if static else f'<path class="edge-hit" d="{path}"/>')
        + compact_label
        + f'<text class="{label_class}" x="{_number(label_x)}" y="{_number(label_y)}" text-anchor="middle">{_escape(display_label)}</text></g>'
    )


def _node_description(node: GraphNode) -> str:
    parts = [node.label, node.source_path or node.resource_key or node.kind]
    for key in ("description", "summary", "note"):
        if node.details.get(key):
            parts.append(str(node.details[key]))
    return "\n".join(parts)


def _visual_type(node: GraphNode) -> str:
    if node.kind == "script":
        return node.script_type or "file"
    if node.kind in {"table", "database", "api"}:
        return node.kind
    suffix = Path(file_card_label(node)).suffix.lower()
    return {".csv": "csv", ".tsv": "csv", ".xlsx": "excel", ".xls": "excel", ".json": "json", ".md": "markdown", ".html": "html", ".log": "log"}.get(suffix, "file")


@lru_cache(maxsize=20)
def _icon(visual_type: str) -> str:
    # Only internal, controlled visual types reach this function. Image context
    # also prevents an SVG asset from injecting active markup into the report.
    encoded = base64.b64encode((_ICON_DIR / f"{visual_type}.svg").read_bytes()).decode("ascii")
    return f'<img src="data:image/svg+xml;base64,{encoded}" alt="" width="36" height="36">'


def _node_html(node: GraphNode, position: Point, summaries: dict[str, NarrativeSummary], statuses: dict[str, str]) -> str:
    summary = summaries.get(node.id)
    status = statuses.get(node.id, "available" if summary is not None else "not generated")
    high_level = summary.high_level if summary else ""
    detailed = summary.detailed if summary else f"No narrative summary is available ({status}).\n\n{_node_description(node)}"
    interactive = node.kind == "script" or summary is not None
    attrs = (
        f' data-script-name="{_escape(file_card_label(node))}" data-script-type="{_escape(node.script_type or node.kind)}"'
        f' data-stage-order-id="{_escape(node.source_path or "Dependency view")}"'
        f' data-high-level-summary="{_escape(high_level)}" data-detailed-summary="{_escape(detailed)}"'
        f' data-summary-status="{_escape(status)}" tabindex="0" role="button"'
    ) if interactive else ""
    return (
        f'<article class="node type-{_visual_type(node)}{" script-node" if interactive else ""}" id="node-{_escape(node.id)}" '
        f'data-node-id="{_escape(node.id)}" data-kind="{_escape(node.kind)}" data-source-path="{_escape(node.source_path or "")}" '
        f'data-x="{_number(position[0])}" data-y="{_number(position[1])}" '
        f'style="left: {_number(position[0])}px; top: {_number(position[1])}px;" title="{_escape(_node_description(node))}"{attrs}>'
        f'<div class="icon">{_icon(_visual_type(node))}</div><div class="node-content">'
        f'<div class="node-kind">{_escape(node.script_type or node.kind)}</div>'
        f'<div class="node-label">{_escape(file_card_label(node))}</div></div></article>'
    )


def _notice_lines(graph: GraphDocument, drawing: _Drawing) -> list[str]:
    """Describe coverage without presenting static uncertainty as a parse failure.

    - Failed or skipped sources receive a separate, explicit warning.
    - Successfully analyzed sources can still contain unresolved dependencies.
    - Proposed arrows and obstructed drawing routes remain visibly disclosed.
    - Short lines are shared by the standalone HTML and plain SVG preview.
    """
    lines = []
    diagnostics = graph_diagnostics(graph)
    coverage = diagnostics["coverage"]
    if coverage["total_sources"]:
        lines.append(f"Analysis coverage: {coverage['analyzed_sources']} of {coverage['total_sources']} source files analyzed.")
    if coverage["failed_sources"] or coverage["skipped_sources"]:
        lines.append(f"Source problems: {coverage['failed_sources']} failed analysis; {coverage['skipped_sources']} were skipped. Their relationships may be missing.")
    if coverage["review_sources"]:
        lines.append(f"Dependency review: {coverage['review_sources']} analyzed file(s) contain unresolved constructs. Additional relationships may be missing.")
    proposed = sum(edge.status == "proposed" for edge in graph.edges)
    if proposed:
        lines.append(f"Review required: {proposed} proposed relationship(s) are shown with dashed amber arrows.")
    if graph.issues:
        counts = diagnostics["counts"]
        lines.append(f"Analysis details: {counts['error']} errors, {counts['warning']} warnings and {counts['info']} informational notes in {len(diagnostics['groups'])} categories.")
    if drawing.overlaps or any(route.obstructed for route in drawing.routes):
        lines.append("Some saved card positions overlap or obstruct connectors. All relationships are retained; adjust the layout to inspect them.")
    if drawing.shared_routes:
        lines.append("Some connectors share a line segment. They remain separate relationships; move nearby cards to distinguish their routes.")
    if not graph.nodes:
        lines.append("This graph has no nodes. No program flow has been established.")
    return lines


def _notices_html(graph: GraphDocument, drawing: _Drawing, statuses: dict[str, str],
                  summary_errors: dict[str, str] | None = None) -> str:
    """Render a compact overview with expandable, source-backed diagnostics.

    - Groups repeated messages while retaining every original occurrence.
    - Prints source paths and line numbers, so evidence is not hidden in a tooltip.
    - Explains requested-model fallback separately from local summaries by choice.
    - Escapes messages, labels, excerpts and errors as untrusted display text.
    """
    lines = _notice_lines(graph, drawing)
    visible = "".join(f"<p>{_escape(line)}</p>" for line in lines)
    diagnostics = graph_diagnostics(graph)
    groups = []
    for group in diagnostics["groups"]:
        occurrences = []
        for occurrence in group["occurrences"]:
            evidence = []
            for entry in occurrence["evidence"]:
                location = entry["source_path"]
                if entry["line_start"]:
                    location += f":{entry['line_start']}"
                    if entry["line_end"] and entry["line_end"] != entry["line_start"]:
                        location += f"–{entry['line_end']}"
                evidence.append(f'<div class="diagnostic-location"><code>{_escape(location)}</code></div>')
                if entry["excerpt"]:
                    evidence.append(f'<pre>{_escape(entry["excerpt"])}</pre>')
            if not evidence and occurrence["source_path"]:
                evidence.append(f'<div class="diagnostic-location"><code>{_escape(occurrence["source_path"])}</code></div>')
            occurrences.append(f'<li>{_escape(occurrence["message"])}{"".join(evidence)}</li>')
        groups.append(
            f'<details class="diagnostic-group" data-issue-code="{_escape(group["code"])}">'
            f'<summary>{_escape(group["title"])} · {group["count"]} occurrence(s) · {_escape(group["severity"])}</summary>'
            f'<p>{_escape(group["description"])}</p><p>{_escape(group["suggested_action"])}</p>'
            f'<ul>{"".join(occurrences)}</ul></details>'
        )
    if diagnostics["sources"]:
        sources = "".join(f'<li><code>{_escape(source["path"])}</code> — {_escape(source["description"])}</li>'
                          for source in diagnostics["sources"])
        groups.append(f'<details class="diagnostic-group"><summary>Source coverage · {len(diagnostics["sources"])} files</summary><ul>{sources}</ul></details>')
    details = (
        f'<details class="analysis-details"><summary>Review details · {len(diagnostics["groups"])} issue categories</summary>'
        f'<p>{_escape(diagnostics["scope_note"])}</p>{"".join(groups)}</details>'
    ) if groups else ""
    node_lookup = {node.id: node for node in graph.nodes}
    unavailable = [node_id for node_id, status in sorted(statuses.items()) if node_id in node_lookup
                   and status not in {"available", "ready", "generated", "cached", "deterministic", "llm"}]
    if unavailable:
        visible += (
            f'<p>Summary generation: requested model summaries were unavailable for {len(unavailable)} file(s). '
            'Local descriptions were used; reviewed connections are unchanged.</p>'
        )
        by_reason: dict[str, list[str]] = defaultdict(list)
        for node_id in unavailable:
            reason = (summary_errors or {}).get(node_id) or "No error detail was recorded. Check summaries.json or generate again for a current diagnosis."
            by_reason[reason].append(node_lookup[node_id].source_path or node_lookup[node_id].label)
        reasons = "".join(
            f'<li>{_escape(reason)}<p>Affected files: {_escape(", ".join(paths))}</p></li>'
            for reason, paths in sorted(by_reason.items())
        )
        details += f'<details><summary>Why local descriptions were used</summary><ul>{reasons}</ul></details>'
    elif statuses:
        counts = Counter(statuses.values())
        if counts["deterministic"]:
            visible += f'<p class="summary-note">Summaries: {counts["deterministic"]} local descriptions. Model summaries were not requested.</p>'
    return f'<section class="analysis-notices" aria-label="Analysis and review notices">{visible}{details}</section>' if visible or details else ""


_EXTRA_STYLE = """
    :root { --line: #7d8ba2; --line-hover: #275dad; }
    .edge-separation { fill: none; stroke: #ffffff; stroke-width: 6px; stroke-linejoin: round; pointer-events: none; }
    #arrow path { fill: #7d8ba2; }
    #arrowHover path { fill: #275dad; }
    .edge.is-active .edge-tooltip-text { opacity: 1; }
    .edge[data-status="proposed"] .edge-path { stroke: #b45309; stroke-dasharray: 6 5; }
    .edge[data-route-status="obstructed"] .edge-path { stroke: #c2410c; }
    .edge-tooltip-text { paint-order: stroke; stroke: #ffffff; stroke-width: 4px; stroke-linejoin: round; }
    .node-content { min-width: 0; }
    .icon img { display: block; width: 36px; height: 36px; }
    .analysis-notices { max-height: 190px; overflow: auto; font: 12px/1.5 system-ui, sans-serif; color: #72501c; }
    .analysis-notices p { margin: 6px 0 0; }
    .analysis-notices details { margin-top: 6px; }
    .analysis-notices summary { cursor: pointer; }
    .analysis-notices ul { margin: 8px 0; padding-left: 20px; }
    .analysis-notices li { padding: 4px 0; }
    .analysis-notices pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 160px; overflow: auto; color: #334155; background: #f5f7fa; padding: 8px; border-radius: 5px; }
    .analysis-notices .diagnostic-group { padding: 8px 10px; border: 1px solid #e5e9ef; border-radius: 6px; background: white; color: #334155; }
    .analysis-notices .diagnostic-location { margin-top: 5px; color: #64748b; }
    .analysis-notices .summary-note { color: #64748b; }
    .script-node:focus { outline: 2px solid #275dad; outline-offset: 3px; }
    .canvas:not(.show-all-relationships) .connection-member,
    .canvas:not(.show-all-relationships) .relationship-label,
    .canvas.show-all-relationships .connection-label { display: none; }
    .canvas:not(.show-all-relationships) .edge[data-connection-status="proposed"] .edge-path { stroke: #b45309; stroke-dasharray: 6 5; }
    .canvas .outside-focus { display: none !important; }
"""


def render_graph_html(
    graph: GraphDocument,
    summaries: dict[str, NarrativeSummary] | None = None,
    summary_statuses: dict[str, str] | None = None,
    *, summary_errors: dict[str, str] | None = None,
) -> str:
    """Build a standalone report; narratives attach strictly by canonical node ID."""
    graph = _validated(graph)
    summaries = {node_id: NarrativeSummary.model_validate(summary) for node_id, summary in (summaries or {}).items()}
    statuses = summary_statuses or {}
    drawing = _drawing(graph)
    nodes = {node.id: node for node in graph.nodes}
    connections = direct_connections(graph)
    edge_connections = {edge.id: group for group in connections for edge in group.members}
    overview = (
        f"Revision {graph.revision} · {len(graph.nodes)} nodes · {len(connections)} direct connections "
        f"({len(graph.edges)} relationship records). Repeated references share an arrow; no transitive links are added. "
        "Choose a node to see only its direct connections. Layout does not establish execution order or complete runtime control flow."
    )
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    template = template.replace("</style>", "{{ extra_style }}\n</style>", 1)
    template = template.replace("</header>", "{{ review_notices }}\n</header>", 1)
    template = template.replace("</body>", '{{ graph_data }}\n<script>document.querySelectorAll(".script-node").forEach(node => node.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); node.click(); } }));</script>\n</body>', 1)
    context = {
        "title": _escape(graph.title),
        "summary": _escape(overview),
        "canvas_width": str(drawing.width),
        "canvas_height": str(drawing.height),
        "edge_svg": "\n".join(_edge_svg(route, nodes, connection=edge_connections[route.edge.id]) for route in drawing.routes),
        "node_html": "\n".join(_node_html(node, drawing.positions[node.id], summaries, statuses) for node in sorted(graph.nodes, key=lambda node: node.id)),
        "extra_style": _EXTRA_STYLE,
        "review_notices": _notices_html(graph, drawing, statuses, summary_errors),
        "graph_data": f'<script type="application/json" id="graph-data" data-schema-version="{graph.schema_version}">{_graph_json(graph)}</script>',
    }
    # A single substitution pass is essential: user labels containing template
    # syntax must remain labels, and cannot replace later template slots.
    return _PLACEHOLDER.sub(lambda match: context[match.group(1)], template)


def _wrap_label(text: str, width: int = 35) -> list[str]:
    lines: list[str] = []
    remaining = text.replace("\n", " ")
    while remaining and len(lines) < 2:
        if len(remaining) <= width:
            lines.append(remaining)
            break
        split = remaining.rfind(" ", 0, width + 1)
        split = split if split > width // 2 else width
        lines.append(remaining[:split])
        remaining = remaining[split:].lstrip()
        if len(lines) == 2 and remaining:
            lines[-1] = lines[-1][:-1] + "…"
    return lines or [""]


def render_graph_svg(graph: GraphDocument) -> str:
    """Build an inert review preview with every node, edge and evidence tooltip."""
    graph = _validated(graph)
    drawing = _drawing(graph)
    node_lookup = {node.id: node for node in graph.nodes}
    connections = direct_connections(graph)
    edge_connections = {edge.id: group for group in connections for edge in group.members}
    notice_lines = _notice_lines(graph, drawing)
    header_height = 100 + 22 * len(notice_lines)
    width = max(drawing.width, 1100)
    height = drawing.height + header_height
    node_svg = []
    for node in sorted(graph.nodes, key=lambda node: node.id):
        x, y = drawing.positions[node.id]
        label_lines = _wrap_label(file_card_label(node))
        label = "".join(f'<tspan x="18" y="{39 + index * 16}">{_escape(line)}</tspan>' for index, line in enumerate(label_lines))
        node_svg.append(
            f'<g class="node" id="node-{_escape(node.id)}" data-node-id="{_escape(node.id)}" data-kind="{_escape(node.kind)}" '
            f'data-x="{_number(x)}" data-y="{_number(y)}" transform="translate({_number(x)} {_number(y)})">'
            f'<title>{_escape(_node_description(node))}</title><rect width="{NODE_WIDTH}" height="{NODE_HEIGHT}" rx="8"/>'
            f'<text class="node-kind" x="18" y="18">{_escape(node.script_type or node.kind)}</text><text class="node-label">{label}</text></g>'
        )
    notices = "".join(f'<text class="notice" x="24" y="{83 + index * 22}">{_escape(line)}</text>' for index, line in enumerate(notice_lines))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="graph-title graph-description">'
        f'<title id="graph-title">{_escape(graph.title)}</title><desc id="graph-description">Static dependency review preview. Positions do not establish execution order. Hover nodes and arrows for source evidence.</desc>'
        '<style>text{font-family:system-ui,sans-serif;fill:#243247}.heading{font-size:21px;font-weight:650}.caption,.notice{font-size:12px}.notice{fill:#854d0e}.node rect{fill:#fff;stroke:#b6c4d5;stroke-width:1.4}.node-kind{font-size:10px;fill:#66758b}.node-label{font-size:13px;font-weight:600}.edge-separation{fill:none;stroke:#f8fafd;stroke-width:6;stroke-linejoin:round;pointer-events:none}.edge-path{fill:none;stroke:#687d97;stroke-width:1.6;marker-end:url(#preview-arrow)}.edge-low,.edge[data-connection-status="proposed"] .edge-path{stroke:#b45309;stroke-dasharray:6 5}.edge-label{font-size:10px;paint-order:stroke;stroke:#f8fafd;stroke-width:4px;stroke-linejoin:round}.connection-member,.relationship-label{display:none}</style>'
        '<defs><marker id="preview-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0 0 L10 4 L0 8 z" fill="#687d97"/></marker></defs>'
        f'<rect width="{width}" height="{height}" fill="#f8fafd"/><text class="heading" x="24" y="30">{_escape(graph.title)}</text>'
        f'<text class="caption" x="24" y="54">Revision {graph.revision} · {len(graph.nodes)} nodes · {len(connections)} direct connections ({len(graph.edges)} relationships) · Hover for evidence.</text>{notices}'
        f'<g transform="translate(0 {header_height})">'
        + "\n".join(_edge_svg(route, node_lookup, static=True, connection=edge_connections[route.edge.id]) for route in drawing.routes)
        + "\n".join(node_svg)
        + f'</g><metadata id="graph-data">{_escape(_graph_json(graph))}</metadata></svg>'
    )
