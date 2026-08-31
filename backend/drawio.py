"""A lossless *topology* review boundary for diagrams.net / draw.io.

Export a .drawio file, edit it in diagrams.net, and use File > Save to return the
whole document. Import replaces the graph's nodes and edges, including deletions;
it never runs a model or infers connections from labels. Dragging, relabelling,
adding, deleting and reconnecting ordinary shapes/connectors are supported.

The server graph is the authority for source associations and analysis metadata.
The file carries identity, positions, labels and connector ``daKind`` / ``daStatus``
properties (editable through Edit Data). New shapes are unsourced process nodes.
New connectors without that property have unknown semantics. Reconnecting or
retyping an existing edge clears its old evidence and condition; immutable server
revision history, rather than misleading citations, retains the old provenance.

This module does not advance revisions or timestamps. The caller must compare and
persist the returned document atomically against the input revision. Shape sizes,
waypoints, colours and text formatting are presentation only. Groups, multiple
pages/layers, ports and separately attached edge labels are deliberately rejected
with an explanation instead of silently disappearing during conversion.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from html.parser import HTMLParser
import math
import re
from typing import get_args
from urllib.parse import unquote_to_bytes
import xml.etree.ElementTree as ET
import zlib

from .graph_models import EdgeKind, GraphDocument, GraphEdge, GraphNode, IDENTIFIER, Position, stable_id
from .graph_presentation import file_card_label


MAX_XML_BYTES = 10 * 1024 * 1024
MAX_CELLS = 20_000
MAX_XML_ELEMENTS = 200_000
MAX_XML_DEPTH = 32
NODE_WIDTH = 300
NODE_HEIGHT = 82
_KINDS = frozenset(get_args(EdgeKind))
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_ID = re.compile(IDENTIFIER)
_FORBIDDEN_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_REMOTE_STYLE = re.compile(r"(?:https?:|ftp:|file:|javascript:|data:|url\s*\()", re.I)
_GEOMETRY_NUMBERS = {"x", "y", "width", "height"}
_STYLE_NUMBERS = {
    "rotation", "opacity", "fillOpacity", "strokeOpacity", "textOpacity", "strokeWidth",
    "fontSize", "spacing", "spacingTop", "spacingBottom", "spacingLeft", "spacingRight",
    "entryX", "entryY", "entryDx", "entryDy", "exitX", "exitY", "exitDx", "exitDy",
    "perimeterSpacing", "sourcePerimeterSpacing", "targetPerimeterSpacing", "startSize",
    "endSize", "arcSize",
}
_MODEL_NUMBERS = {"dx", "dy", "gridSize", "pageScale", "pageWidth", "pageHeight"}


def _number(value: str | None, field: str, default: float = 0) -> float:
    if value is None:
        return default
    value = value.strip()
    if not _NUMBER.fullmatch(value):
        raise ValueError(f"Invalid numeric {field}: use a finite decimal number.")
    result = float(value)
    if not math.isfinite(result) or abs(result) > 1_000_000:
        raise ValueError(f"Invalid numeric {field}: the value must be finite and within ±1,000,000.")
    return result


def _check_payload(xml: str) -> None:
    if not isinstance(xml, str):
        raise ValueError("The diagram must be XML text from a .drawio file.")
    try:
        size = len(xml.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("The diagram contains invalid Unicode.") from exc
    if size > MAX_XML_BYTES:
        raise ValueError("The diagram exceeds the 10 MiB XML size limit.")
    if _FORBIDDEN_CONTROLS.search(xml):
        raise ValueError("The diagram contains invalid XML control characters.")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
        raise ValueError("DTD and entity declarations are not permitted in review diagrams.")
    if re.search(r"<\?(?!xml(?:\s|\?>))", xml, re.I):
        raise ValueError("XML processing instructions and external resources are not supported.")


def _check_reference_attributes(element: ET.Element) -> None:
    for name, value in element.attrib.items():
        if "{" in name or ":" in name:
            raise ValueError("Namespaced XML attributes and external references are not supported.")
        if name.lower() in {"href", "src", "url", "link", "resource"} and value.strip():
            raise ValueError("Remove external links and resource references from the review diagram.")
        if name in {"backgroundImage", "extFonts"} and value.strip():
            raise ValueError("Background images and external font resources are not supported; remove them before importing.")
        if name == "style":
            parts = _style(value)
            if "image" in parts or parts.get("shape") == "image" or _REMOTE_STYLE.search(value):
                raise ValueError("Images and remote resources are not supported; use ordinary labelled shapes.")
        if name == "placeholders" and value not in {"", "0"}:
            raise ValueError("Replace label placeholders with literal text before importing the diagram.")


def _parse_xml(xml: str) -> ET.Element:
    _check_payload(xml)
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError(f"Invalid diagram XML: {exc}") from exc
    count = 0
    pending = [(root, 1)]
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > MAX_XML_ELEMENTS or depth > MAX_XML_DEPTH:
            raise ValueError("The diagram exceeds the XML element or nesting limit.")
        if "{" in element.tag or ":" in element.tag:
            raise ValueError("Namespaced XML elements and external references are not supported.")
        _check_reference_attributes(element)
        pending.extend((child, depth + 1) for child in element)
    return root


def _decode_diagram(payload: str) -> ET.Element:
    payload = payload.strip()
    if payload.startswith("<"):
        return _parse_xml(payload)
    try:
        compressed = base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid compressed .drawio payload; save the full diagram as .drawio XML.") from exc
    try:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        expanded = decoder.decompress(compressed, MAX_XML_BYTES + 1)
        if len(expanded) > MAX_XML_BYTES or decoder.unconsumed_tail:
            raise ValueError("The expanded diagram exceeds the 10 MiB size limit.")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("The compressed diagram is truncated or contains trailing streams.")
        text = expanded.decode("utf-8")
    except (zlib.error, UnicodeError) as exc:
        raise ValueError("Invalid compressed diagram data.") from exc
    if not text.lstrip().startswith("<"):
        if re.search(r"%(?![0-9a-fA-F]{2})", text):
            raise ValueError("Invalid percent encoding in the compressed diagram.")
        try:
            text = unquote_to_bytes(text).decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("The expanded diagram is not valid UTF-8 XML.") from exc
    return _parse_xml(text)


def _whitespace_only(element: ET.Element) -> None:
    if element.text and element.text.strip():
        raise ValueError(f"Unexpected text in {element.tag}; save the full .drawio document.")
    if any(child.tail and child.tail.strip() for child in element):
        raise ValueError(f"Unexpected text in {element.tag}; save the full .drawio document.")


def _model_and_metadata(xml: str) -> tuple[ET.Element, list[ET.Element]]:
    document = _parse_xml(xml)
    metadata = [document]
    if document.tag == "mxGraphModel":
        return document, metadata
    if document.tag != "mxfile":
        raise ValueError("Expected an mxfile or mxGraphModel document from diagrams.net.")
    _whitespace_only(document)
    pages = list(document)
    if len(pages) != 1 or pages[0].tag != "diagram":
        raise ValueError("Exactly one diagram page is supported. Keep the review on one page and remove other pages.")
    page = pages[0]
    metadata.append(page)
    children = list(page)
    if children:
        _whitespace_only(page)
        if len(children) != 1 or children[0].tag != "mxGraphModel":
            raise ValueError("The page must contain exactly one mxGraphModel.")
        model = children[0]
    else:
        if not page.text or not page.text.strip():
            raise ValueError("The diagram page is empty.")
        model = _decode_diagram(page.text)
        if model.tag != "mxGraphModel":
            raise ValueError("The expanded page must contain exactly one mxGraphModel.")
    metadata.append(model)
    return model, metadata


def _style(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in value.split(";"):
        token = token.strip()
        if token:
            key, _, val = token.partition("=")
            result[key] = val
    return result


class _PlainLabel(HTMLParser):
    """Keep common diagrams.net text formatting, but no active HTML content."""

    _tags = {"div", "p", "span", "br", "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup", "font", "pre", "code", "ul", "ol", "li"}
    _blocks = {"div", "p", "pre", "li", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._tags:
            raise ValueError(f"Unsupported HTML label element <{tag}>; use plain text or basic text formatting.")
        for name, value in attrs:
            if name.startswith("on") or name in {"href", "src", "url", "link"} or (value and _REMOTE_STYLE.search(value)):
                raise ValueError("Active HTML and external resources are not allowed in diagram labels.")
        if tag == "br":
            self.parts.append("\n")
        elif tag in self._blocks:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._tags:
            raise ValueError(f"Unsupported HTML label element </{tag}>; use plain text.")
        if tag in self._blocks:
            self._newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data.replace("\xa0", " "))

    def handle_comment(self, data: str) -> None:
        raise ValueError("HTML comments are not supported in diagram labels.")

    def handle_decl(self, decl: str) -> None:
        raise ValueError("HTML declarations are not supported in diagram labels.")

    def unknown_decl(self, data: str) -> None:
        raise ValueError("HTML declarations are not supported in diagram labels.")

    def handle_pi(self, data: str) -> None:
        raise ValueError("Processing instructions are not supported in diagram labels.")


@dataclass(frozen=True)
class _Cell:
    id: str
    element: ET.Element
    wrapper: ET.Element | None = None

    def get_property(self, name: str) -> str | None:
        inner = self.element.get(name)
        outer = self.wrapper.get(name) if self.wrapper is not None else None
        if inner is not None and outer is not None and inner != outer:
            raise ValueError(f"Cell {self.id} has conflicting {name} properties.")
        return outer if outer is not None else inner

    def label(self) -> str:
        value = self.element.get("value")
        label = self.wrapper.get("label") if self.wrapper is not None else None
        if value is not None and label is not None and value != label:
            raise ValueError(f"Cell {self.id} has conflicting label and value properties.")
        text = label if label is not None else value or ""
        if self.style.get("html", "0") == "1":
            parser = _PlainLabel()
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts).strip("\n")
        return text

    @property
    def style(self) -> dict[str, str]:
        return _style(self.element.get("style", ""))


def _read_cells(model: ET.Element) -> list[_Cell]:
    _whitespace_only(model)
    if len(model) != 1 or model[0].tag != "root":
        raise ValueError("mxGraphModel must contain exactly one root element.")
    for name in _MODEL_NUMBERS:
        if name in model.attrib:
            _number(model.get(name), f"graph {name}")
    root = model[0]
    _whitespace_only(root)
    if len(root) > MAX_CELLS:
        raise ValueError(f"The diagram exceeds the {MAX_CELLS:,} cell limit.")
    cells: list[_Cell] = []
    seen: set[str] = set()
    for entry in root:
        if entry.tag == "mxCell":
            wrapper, element = None, entry
        elif entry.tag in {"object", "UserObject"}:
            _whitespace_only(entry)
            if len(entry) != 1 or entry[0].tag != "mxCell":
                raise ValueError("Each object/UserObject must wrap exactly one mxCell.")
            wrapper, element = entry, entry[0]
        else:
            raise ValueError(f"Unsupported diagram element {entry.tag}; use ordinary shapes and connectors.")
        inner_id = element.get("id")
        outer_id = wrapper.get("id") if wrapper is not None else None
        if inner_id is not None and outer_id is not None and inner_id != outer_id:
            raise ValueError("An object wrapper and its mxCell have conflicting IDs.")
        cell_id = outer_id if outer_id is not None else inner_id
        if not cell_id or len(cell_id) > 512:
            raise ValueError("Every diagram cell needs a nonempty ID of at most 512 characters.")
        if cell_id in seen:
            raise ValueError(f"Duplicate diagram cell ID: {cell_id}.")
        seen.add(cell_id)
        cell = _Cell(cell_id, element, wrapper)
        for flag in ("vertex", "edge", "collapsed", "visible", "connectable"):
            if element.get(flag) not in {None, "0", "1"}:
                raise ValueError(f"Cell {cell_id} has an invalid {flag} flag.")
        if element.get("vertex") == "1" and element.get("edge") == "1":
            raise ValueError(f"Cell {cell_id} cannot be both a shape and a connector.")
        if cell.style.get("html", "0") not in {"0", "1"}:
            raise ValueError(f"Cell {cell_id} has an invalid html style flag.")
        for name in _STYLE_NUMBERS & cell.style.keys():
            _number(cell.style[name], f"{cell_id} style {name}")
        _validate_geometry(cell)
        cells.append(cell)
    return cells


def _validate_geometry(cell: _Cell) -> None:
    _whitespace_only(cell.element)
    geometry = list(cell.element)
    if not geometry:
        if cell.element.get("vertex") == "1":
            raise ValueError(f"Node {cell.id} has no geometry. Save the full .drawio diagram.")
        return
    if len(geometry) != 1 or geometry[0].tag != "mxGeometry":
        raise ValueError(f"Cell {cell.id} has unsupported content; only mxGeometry is supported.")
    geom = geometry[0]
    if geom.get("as") not in {None, "geometry"} or geom.get("relative") not in {None, "0", "1"}:
        raise ValueError(f"Cell {cell.id} has invalid geometry attributes.")
    if cell.element.get("vertex") == "1" and geom.get("relative") == "1":
        raise ValueError("Relative vertices and ports are not supported. Use independent shapes on the default layer.")
    for name in _GEOMETRY_NUMBERS & geom.attrib.keys():
        result = _number(geom.get(name), f"{cell.id} {name}")
        if name in {"width", "height"} and result < 0:
            raise ValueError(f"Cell {cell.id} has a negative {name}.")
    _whitespace_only(geom)
    for child in geom:
        _whitespace_only(child)
        if child.tag == "Array" and child.get("as") == "points":
            points = list(child)
            if any(point.tag != "mxPoint" or len(point) for point in points):
                raise ValueError(f"Cell {cell.id} contains an unsupported waypoint.")
        elif child.tag == "mxPoint" and child.get("as") in {"sourcePoint", "targetPoint", "offset"} and not len(child):
            points = [child]
        elif child.tag == "mxRectangle" and child.get("as") == "alternateBounds" and not len(child):
            points = [child]
        else:
            raise ValueError(f"Cell {cell.id} contains unsupported geometry {child.tag}.")
        for point in points:
            _whitespace_only(point)
            for name in _GEOMETRY_NUMBERS & point.attrib.keys():
                result = _number(point.get(name), f"{cell.id} waypoint {name}")
                if name in {"width", "height"} and result < 0:
                    raise ValueError(f"Cell {cell.id} contains a negative waypoint {name}.")


def _validate_metadata(graph: GraphDocument, records: list[ET.Element]) -> None:
    found = False
    for record in records:
        graph_id, revision = record.get("daGraphId"), record.get("daRevision")
        if graph_id is None and revision is None:
            continue
        if graph_id is None or revision is None:
            raise ValueError("The diagram has incomplete graph/revision metadata. Export a fresh review diagram.")
        if graph_id != graph.id:
            raise ValueError("This review diagram belongs to a different graph. Export the intended graph and edit that file.")
        if revision != str(graph.revision):
            raise ValueError("This review diagram has a stale or invalid revision. Export the current revision before editing.")
        found = True
    if not found:
        raise ValueError("Graph/revision metadata is missing. Edit an exported review diagram and use File > Save.")


def _edge_display_label(edge: GraphEdge) -> str:
    return edge.label if edge.label is not None else edge.kind.replace("_", " ")


def export_drawio(graph: GraphDocument) -> str:
    """Export a one-page, uncompressed .drawio diagram without summaries or HTML."""
    graph = GraphDocument.model_validate(graph.model_dump())
    if len(graph.nodes) + len(graph.edges) + 2 > MAX_CELLS:
        raise ValueError(f"The graph exceeds the {MAX_CELLS:,} review-diagram cell limit.")
    document = ET.Element("mxfile", {"host": "app.diagrams.net", "agent": "DA Document Optimization", "compressed": "false"})
    page = ET.SubElement(document, "diagram", {"id": graph.id, "name": graph.title})
    metadata = {"daGraphId": graph.id, "daRevision": str(graph.revision)}
    model = ET.SubElement(page, "mxGraphModel", {**metadata, "grid": "1", "gridSize": "10", "guides": "1", "connect": "1", "arrows": "1", "page": "0"})
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    # Custom mxCell values survive normal diagrams.net Save; arbitrary model
    # attributes are sometimes regenerated by the editor, so retain both copies.
    layer = ET.SubElement(root, "object", {"id": "1", "label": "Workflow review", **metadata})
    ET.SubElement(layer, "mxCell", {"parent": "0"})
    for index, node in enumerate(graph.nodes):
        x = node.position.x if node.position else (index % 4) * (NODE_WIDTH + 160)
        y = node.position.y if node.position else (index // 4) * (NODE_HEIGHT + 92)
        shape = "shape=rhombus;" if node.kind == "decision" else "rounded=1;"
        fill = "#e8f0fe" if node.source_path else "#f3f4f6"
        cell = ET.SubElement(root, "mxCell", {
            "id": node.id, "value": file_card_label(node), "vertex": "1", "parent": "1",
            "style": f"{shape}whiteSpace=wrap;html=0;fillColor={fill};strokeColor=#64748b;fontColor=#111827;fontSize=13;",
        })
        ET.SubElement(cell, "mxGeometry", {"x": str(x), "y": str(y), "width": str(NODE_WIDTH), "height": str(NODE_HEIGHT), "as": "geometry"})
    for edge in graph.edges:
        wrapper = ET.SubElement(root, "object", {"id": edge.id, "label": _edge_display_label(edge), "daKind": edge.kind, "daStatus": edge.status})
        colour = "#b45309" if edge.status == "proposed" else "#475569"
        dashed = "dashed=1;dashPattern=8 5;" if edge.status == "proposed" else "dashed=0;"
        cell = ET.SubElement(wrapper, "mxCell", {
            "edge": "1", "parent": "1", "source": edge.source, "target": edge.target,
            "style": f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=0;startArrow=none;endArrow=block;endFill=1;strokeColor={colour};fontColor={colour};{dashed}",
        })
        ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
    xml = ET.tostring(document, encoding="unicode", xml_declaration=True)
    _check_payload(xml)
    return xml


def import_drawio(graph: GraphDocument, xml: str) -> GraphDocument:
    """Apply one matching review diagram to a server graph without mutating it.

    The caller owns authorization, optimistic concurrency and revision history.
    Arbitrary XML metadata never changes source paths, summaries or evidence.
    """
    model, metadata = _model_and_metadata(xml)
    cells = _read_cells(model)
    by_id = {cell.id: cell for cell in cells}
    if "0" not in by_id or "1" not in by_id:
        raise ValueError("The diagram must retain the original root and default layer. Export a fresh review diagram.")
    for cell in (by_id["0"], by_id["1"]):
        expected_parent = None if cell.id == "0" else "0"
        if cell.element.get("parent") != expected_parent or cell.element.get("vertex") == "1" or cell.element.get("edge") == "1":
            raise ValueError("The original diagram root/default layer has been changed. Export a fresh review diagram.")
        metadata.append(cell.element)
        if cell.wrapper is not None:
            metadata.append(cell.wrapper)
    _validate_metadata(graph, metadata)
    vertices: list[_Cell] = []
    connectors: list[_Cell] = []
    for cell in cells:
        if cell.id in {"0", "1"}:
            continue
        if cell.element.get("parent") != "1":
            raise ValueError(f"Cell {cell.id} is outside the default layer. Ungroup shapes, move them to the default layer, and remove extra layers/attached edge labels.")
        if "group" in cell.style or "swimlane" in cell.style or cell.style.get("container") == "1" or cell.style.get("shape") in {"group", "swimlane"}:
            raise ValueError(f"Group/container {cell.id} is unsupported. Ungroup it into independent shapes before importing.")
        if cell.element.get("vertex") == "1":
            if cell.element.get("source") or cell.element.get("target"):
                raise ValueError(f"Node {cell.id} unexpectedly has connector endpoints.")
            vertices.append(cell)
        elif cell.element.get("edge") == "1":
            if cell.style.get("startArrow", "none") not in {"", "none"} or cell.style.get("endArrow") == "none":
                raise ValueError(f"Connector {cell.id} must have one arrow at its target end. Reconnect source/target endpoints to reverse direction.")
            connectors.append(cell)
        else:
            raise ValueError(f"Unsupported cell/layer {cell.id}. Use one default layer with ordinary shapes and directed connectors.")
    old_nodes = {node.id: node for node in graph.nodes}
    old_edges = {edge.id: edge for edge in graph.edges}
    node_ids = {
        cell.id: cell.id if _ID.fullmatch(cell.id) else stable_id("review_node", graph.id, cell.id)
        for cell in vertices
    }
    edge_ids = {
        cell.id: cell.id if _ID.fullmatch(cell.id) else stable_id("review_edge", graph.id, cell.id)
        for cell in connectors
    }
    all_ids = list(node_ids.values()) + list(edge_ids.values())
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("Diagram IDs collide after normalization. Rename the conflicting shape/connector IDs.")
    nodes: list[GraphNode] = []
    for cell in vertices:
        node_id = node_ids[cell.id]
        if node_id in old_edges:
            raise ValueError(f"Connector ID {node_id} cannot be reused for a node.")
        label = cell.label()
        if not label.strip():
            raise ValueError(f"Node {cell.id} needs a nonblank label before import.")
        geometry = cell.element[0]
        position = Position(x=_number(geometry.get("x"), f"{cell.id} x"), y=_number(geometry.get("y"), f"{cell.id} y"))
        if node_id in old_nodes:
            data = old_nodes[node_id].model_dump()
            # - A short exported filename is a display choice, not a source rename.
            # - Preserve the original label when the visible label is unchanged.
            # - A deliberate new label is still accepted without changing identity.
            if label == file_card_label(old_nodes[node_id]):
                label = old_nodes[node_id].label
            data.update(label=label, position=position)
            node = GraphNode.model_validate(data)
        else:
            # A copied source node gets a new ID and therefore no source metadata.
            node = GraphNode(id=node_id, label=label, kind="process", position=position)
        nodes.append(node)
    edges: list[GraphEdge] = []
    for cell in connectors:
        edge_id = edge_ids[cell.id]
        if edge_id in old_nodes:
            raise ValueError(f"Node ID {edge_id} cannot be reused for a connector.")
        source, target = cell.element.get("source"), cell.element.get("target")
        if source not in node_ids or target not in node_ids:
            raise ValueError(f"Connector {cell.id} has a missing/dangling endpoint. Attach both ends to shapes, or delete the connector.")
        source, target = node_ids[source], node_ids[target]
        previous = old_edges.get(edge_id)
        kind = cell.get_property("daKind")
        if kind is None:
            kind = previous.kind if previous else "unknown"
        if kind not in _KINDS:
            raise ValueError(f"Connector {cell.id} has unsupported daKind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}.")
        requested_status = cell.get_property("daStatus")
        if requested_status not in {None, "confirmed", "proposed"}:
            raise ValueError(f"Connector {cell.id} has unsupported daStatus {requested_status!r}. Use confirmed or proposed.")
        label = cell.label()
        if previous and label == _edge_display_label(previous):
            label = previous.label
        elif not previous and not label:
            label = None
        if previous:
            topology_changed = (source, target, kind) != (previous.source, previous.target, previous.kind)
            status_changed = requested_status is not None and requested_status != previous.status
            changed = topology_changed or label != previous.label or status_changed
            data = previous.model_dump()
            data.update(source=source, target=target, kind=kind, label=label)
            if changed:
                note = "Edited in diagrams.net."
                if topology_changed:
                    note += f" Previous connection: {previous.source} → {previous.target} ({previous.kind}); reviewed connection: {source} → {target} ({kind}). Previous evidence and condition cleared; consult the prior revision for provenance."
                    data.update(evidence=[], condition=None)
                elif label != previous.label:
                    note += " Label changed; endpoint direction, kind and source evidence were retained."
                if status_changed:
                    note += f" Review status explicitly changed from {previous.status} to {requested_status}."
                if previous.review_note:
                    note += f" Previous review note: {previous.review_note}"
                # Exported proposals already carry daStatus=proposed. A label
                # edit must not implicitly approve that proposal. A substantive
                # user correction does confirm it unless status was explicitly
                # changed in the opposite direction.
                status = requested_status if status_changed else "confirmed" if topology_changed else previous.status
                data.update(origin="user", status=status, review_note=note)
            edge = GraphEdge.model_validate(data)
        else:
            edge = GraphEdge(
                id=edge_id, source=source, target=target, kind=kind, label=label,
                origin="user", status=requested_status or "confirmed", review_note="Added in diagrams.net by the user; no source evidence has been inferred.",
            )
        edges.append(edge)
    data = graph.model_dump()
    data.update(nodes=nodes, edges=edges)
    return GraphDocument.model_validate(data)
