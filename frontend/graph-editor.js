/*
Dependency diagram canvas, using only the browser's built-in SVG support.

- Render the saved nodes and connections without inferring extra topology.
- Support dragging, panning, zooming, selection and keyboard movement.
- Keep temporary pointer movement separate from committed editor changes.
- Send complete position changes to the app only when a drag ends.
- Label filtered views explicitly; a hidden connection is never a deleted one.
- Leave all save, undo, validation and recovery decisions to GraphSession.
*/

import {connectionGroups, nodeName} from "./graph-presentation.js";

const SVG_NS = "http://www.w3.org/2000/svg";
// - These dimensions match draw.io and final HTML, preserving reviewed spacing.
const CARD_WIDTH = 300;
const CARD_HEIGHT = 82;
const COLORS = {python: "#3566a2", sql: "#8054c7", alteryx: "#a04fb3", bat: "#566dca", script: "#3566a2", file: "#7b63bf", table: "#8054c7", database: "#6853c5", process: "#4e68cb", decision: "#c43f91", module: "#7d66bd", api: "#4967c8", unknown: "#76818c"};
const LABELS = {python: "PYTHON", sql: "SQL", alteryx: "ALTERYX", bat: "BATCH", script: "SCRIPT", file: "FILE", table: "TABLE", database: "DATABASE", process: "PROCESS", decision: "DECISION", module: "MODULE", api: "API", unknown: "NODE"};

function svgElement(name, attributes = {}, text = null) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text !== null) element.textContent = text;
  return element;
}
function shortened(value, limit) {
  const text = String(value ?? "");
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}
function pointString(point) { return `${point.x},${point.y}`; }

export class GraphEditor {
  /*
  Manage canvas interaction while leaving graph ownership to ``GraphSession``.

  - Pointer movement updates temporary card positions until a drag completes.
  - Panning and zooming change the viewport only and never become graph edits.
  - Selection callbacks identify canonical IDs used by the inspector and keyboard.
  - Filtering controls visibility only; it cannot remove a saved node or edge.
  */
  constructor(svg, {onSelect, onMove, onDelete, onViewChange}) {
    this.svg = svg;
    this.onSelect = onSelect;
    this.onMove = onMove;
    this.onDelete = onDelete;
    this.onViewChange = onViewChange;
    this.graph = null;
    this.selection = null;
    this.disabled = false;
    this.grouped = true;
    this.filter = {query: "", kind: "all", scope: "all"};
    this.view = {x: 0, y: 0, width: 1000, height: 650};
    this.drag = null;
    this.previewPositions = new Map();
    this.visibleNodes = [];
    this.visibleEdges = [];
    this.frame = null;
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.graph) return;
      const rect = this.svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      this.view.height = this.view.width * rect.height / rect.width;
      this.applyView();
    });
    this.resizeObserver.observe(svg);
    svg.addEventListener("pointerdown", event => this.pointerDown(event));
    svg.addEventListener("pointermove", event => this.pointerMove(event));
    svg.addEventListener("pointerup", event => this.pointerUp(event));
    svg.addEventListener("pointercancel", () => this.cancelDrag());
    svg.addEventListener("wheel", event => {
      if (!this.graph) return;
      event.preventDefault();
      this.zoom(Math.exp(-event.deltaY * 0.0015), event);
    }, {passive: false});
    svg.addEventListener("keydown", event => this.keyDown(event));
  }

  setGraph(graph, {fit = false} = {}) {
    this.graph = graph;
    if (this.selection && !graph[this.selection.type === "node" ? "nodes" : "edges"].some(item => item.id === this.selection.id)) this.selection = null;
    this.previewPositions.clear();
    this.render();
    if (fit) requestAnimationFrame(() => this.fit());
  }

  setSelection(selection) {
    this.selection = selection;
    this.render();
  }

  setDisabled(disabled) {
    this.disabled = disabled;
    this.svg.classList.toggle("is-locked", disabled);
  }

  setFilter(filter) {
    this.filter = {...this.filter, ...filter};
    this.render();
    this.fit();
  }

  position(node) {
    const index = this.graph.nodes.indexOf(node);
    return this.previewPositions.get(node.id) ?? node.position ?? {x: (index % 4) * 300, y: Math.floor(index / 4) * 145};
  }

  shownGraph() {
    const {query, kind, scope} = this.filter;
    const search = query.trim().toLowerCase();
    let ids = new Set(this.graph.nodes.map(node => node.id));
    let focusSeeds = null;
    if (scope === "neighbors" && this.selection) {
      const selectedEdge = this.selection.type === "edge" ? this.graph.edges.find(edge => edge.id === this.selection.id) : null;
      const seed = new Set(selectedEdge ? [selectedEdge.source, selectedEdge.target] : [this.selection.id]);
      focusSeeds = seed;
      ids = new Set(seed);
      for (const edge of this.graph.edges) {
        if (seed.has(edge.source) || seed.has(edge.target)) { ids.add(edge.source); ids.add(edge.target); }
      }
    }
    if (search) {
      const matches = new Set(this.graph.nodes.filter(node => `${node.label} ${node.source_path ?? ""}`.toLowerCase().includes(search)).map(node => node.id));
      const context = new Set(matches);
      for (const edge of this.graph.edges) if (matches.has(edge.source) || matches.has(edge.target)) {
        context.add(edge.source); context.add(edge.target);
      }
      ids = new Set([...ids].filter(id => context.has(id)));
    }
    const nodes = this.graph.nodes.filter(node => ids.has(node.id) && (kind === "all" || node.kind === kind || node.script_type === kind));
    const visible = new Set(nodes.map(node => node.id));
    const edges = this.grouped ? connectionGroups(this.graph) : this.graph.edges;
    return {nodes, edges: edges.filter(edge => visible.has(edge.source) && visible.has(edge.target) &&
      (!focusSeeds || focusSeeds.has(edge.source) || focusSeeds.has(edge.target)))};
  }

  fit() {
    if (!this.visibleNodes.length) return;
    const rect = this.svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const positions = this.visibleNodes.map(node => this.position(node));
    const left = Math.min(...positions.map(point => point.x)) - 80;
    const top = Math.min(...positions.map(point => point.y)) - 80;
    const right = Math.max(...positions.map(point => point.x)) + CARD_WIDTH + 80;
    const bottom = Math.max(...positions.map(point => point.y)) + CARD_HEIGHT + 80;
    const width = Math.max(right - left, (bottom - top) * rect.width / rect.height, 450);
    const height = width * rect.height / rect.width;
    this.view = {x: (left + right - width) / 2, y: (top + bottom - height) / 2, width, height};
    this.applyView();
  }

  focusNode(id) {
    const node = this.graph?.nodes.find(item => item.id === id);
    if (!node) return;
    this.selection = {type: "node", id};
    this.filter.query = "";
    this.filter.kind = "all";
    this.render();
    const rect = this.svg.getBoundingClientRect();
    const position = this.position(node);
    const width = Math.max(560, rect.width);
    const height = width * (rect.height || 600) / (rect.width || 900);
    this.view = {x: position.x + CARD_WIDTH / 2 - width / 2, y: position.y + CARD_HEIGHT / 2 - height / 2, width, height};
    this.applyView();
  }

  zoom(factor, event = null) {
    const width = Math.min(3000000, Math.max(260, this.view.width / factor));
    const ratio = width / this.view.width;
    const anchor = event ? this.worldPoint(event) : {x: this.view.x + this.view.width / 2, y: this.view.y + this.view.height / 2};
    this.view = {x: anchor.x - (anchor.x - this.view.x) * ratio, y: anchor.y - (anchor.y - this.view.y) * ratio, width, height: this.view.height * ratio};
    this.applyView();
  }

  applyView() {
    this.svg.setAttribute("viewBox", `${this.view.x} ${this.view.y} ${this.view.width} ${this.view.height}`);
    const width = this.svg.getBoundingClientRect().width;
    this.onViewChange?.({zoom: Math.round(width / this.view.width * 100), nodes: this.visibleNodes.length, edges: this.visibleEdges.length});
  }

  worldPoint(event) {
    const rect = this.svg.getBoundingClientRect();
    return {x: this.view.x + (event.clientX - rect.left) / rect.width * this.view.width, y: this.view.y + (event.clientY - rect.top) / rect.height * this.view.height};
  }

  edgePath(edge, index) {
    const source = this.graph.nodes.find(node => node.id === edge.source);
    const target = this.graph.nodes.find(node => node.id === edge.target);
    const a = this.position(source);
    const b = this.position(target);
    if (source.id === target.id) {
      return {d: `M ${a.x + CARD_WIDTH} ${a.y + 22} C ${a.x + CARD_WIDTH + 100} ${a.y - 55}, ${a.x + CARD_WIDTH + 100} ${a.y + 133}, ${a.x + CARD_WIDTH} ${a.y + 56}`, label: {x: a.x + CARD_WIDTH + 76, y: a.y + 38}};
    }
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    let start, end, controlA, controlB;
    const offset = (index % 5 - 2) * 5;
    if (Math.abs(dx) > Math.abs(dy) * 0.65) {
      const forward = dx >= 0;
      start = {x: a.x + (forward ? CARD_WIDTH : 0), y: a.y + CARD_HEIGHT / 2 + offset};
      end = {x: b.x + (forward ? 0 : CARD_WIDTH), y: b.y + CARD_HEIGHT / 2 + offset};
      const reach = Math.max(58, Math.abs(end.x - start.x) * 0.48);
      controlA = {x: start.x + (forward ? reach : -reach), y: start.y};
      controlB = {x: end.x + (forward ? -reach : reach), y: end.y};
    } else {
      const forward = dy >= 0;
      start = {x: a.x + CARD_WIDTH / 2 + offset, y: a.y + (forward ? CARD_HEIGHT : 0)};
      end = {x: b.x + CARD_WIDTH / 2 + offset, y: b.y + (forward ? 0 : CARD_HEIGHT)};
      const reach = Math.max(48, Math.abs(end.y - start.y) * 0.48);
      controlA = {x: start.x, y: start.y + (forward ? reach : -reach)};
      controlB = {x: end.x, y: end.y + (forward ? -reach : reach)};
    }
    return {d: `M ${pointString(start)} C ${pointString(controlA)} ${pointString(controlB)} ${pointString(end)}`, label: {x: (start.x + end.x) / 2, y: (start.y + end.y) / 2}};
  }

  render() {
    if (!this.graph) return;
    const visible = this.shownGraph();
    this.visibleNodes = visible.nodes;
    this.visibleEdges = visible.edges;
    const fragment = document.createDocumentFragment();
    const defs = svgElement("defs");
    // - Arrowheads follow the selected accent through CSS custom properties.
    // - Proposed edges retain semantic pink in every theme.
    // - This is a visual change only; endpoint IDs and direction stay untouched.
    for (const [name, color] of [["normal", "#8c96a5"], ["selected", "var(--accent)"], ["proposed", "var(--review)"]]) {
      const marker = svgElement("marker", {id: `arrow-${name}`, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse", markerUnits: "userSpaceOnUse"});
      marker.append(svgElement("path", {d: "M 1 1 L 9 5 L 1 9 Z", fill: color}));
      defs.append(marker);
    }
    fragment.append(defs);
    const edgeLayer = svgElement("g", {class: "canvas-edges"});
    const nodeLayer = svgElement("g", {class: "canvas-nodes"});
    for (const [index, edge] of visible.edges.entries()) {
      const selected = this.selection?.type === "edge" && (this.selection.id === edge.id || edge.member_ids?.includes(this.selection.id));
      const proposed = edge.status === "proposed";
      const style = selected ? "selected" : proposed ? "proposed" : "normal";
      const route = this.edgePath(edge, index);
      const sourceName = this.graph.nodes.find(node => node.id === edge.source)?.label;
      const targetName = this.graph.nodes.find(node => node.id === edge.target)?.label;
      const group = svgElement("g", {"data-edge": edge.id, class: `canvas-edge ${selected ? "is-selected" : ""} ${proposed ? "is-proposed" : ""}`, tabindex: 0, role: "button", "aria-label": `${sourceName} to ${targetName}, ${edge.kind.replaceAll("_", " ")}${proposed ? ", unconfirmed" : ""}`});
      group.append(svgElement("path", {d: route.d, class: "edge-halo"}));
      group.append(svgElement("path", {d: route.d, class: "edge-visible", "marker-end": `url(#arrow-${style})`}));
      group.append(svgElement("path", {d: route.d, class: "edge-hit"}));
      if (selected) {
        const label = edge.display_label ?? edge.kind.replaceAll("_", " ");
        const width = label.length * 6.8 + 18;
        group.append(svgElement("rect", {x: route.label.x - width / 2, y: route.label.y - 11, width, height: 23, rx: 5, class: "edge-label-bg"}));
        group.append(svgElement("text", {x: route.label.x, y: route.label.y + 4, "text-anchor": "middle", class: "edge-label"}, label));
      }
      edgeLayer.append(group);
    }
    for (const node of visible.nodes) {
      const position = this.position(node);
      const selected = this.selection?.type === "node" && this.selection.id === node.id;
      const type = node.script_type ?? node.kind;
      const color = COLORS[type] ?? COLORS.unknown;
      const group = svgElement("g", {"data-node": node.id, transform: `translate(${position.x} ${position.y})`, class: `canvas-node ${selected ? "is-selected" : ""}`, tabindex: 0, role: "button", "aria-label": `${node.label}, ${type}. Select to inspect; use arrow keys to move.`});
      group.append(svgElement("title", {}, node.source_path ?? node.resource_key ?? node.label));
      group.append(svgElement("rect", {width: CARD_WIDTH, height: CARD_HEIGHT, rx: 9, class: "node-card"}));
      group.append(svgElement("rect", {x: 13, y: 14, width: 7, height: 7, rx: 2, fill: color}));
      group.append(svgElement("text", {x: 27, y: 21, class: "node-type", fill: color}, LABELS[type] ?? "NODE"));
      // - Script/file cards display the filename and extension only.
      // - Full Windows/POSIX paths remain in the tooltip and inspector.
      const label = nodeName(node);
      group.append(svgElement("text", {x: 14, y: 45, class: "node-name"}, shortened(label, 37)));
      nodeLayer.append(group);
    }
    fragment.append(edgeLayer, nodeLayer);
    this.svg.replaceChildren(fragment);
    this.applyView();
  }

  pointerDown(event) {
    if (!this.graph || (event.button !== 0 && event.button !== 1)) return;
    const nodeElement = event.target.closest("[data-node]");
    const edgeElement = event.target.closest("[data-edge]");
    const selection = nodeElement ? {type: "node", id: nodeElement.dataset.node} : edgeElement ? {type: "edge", id: edgeElement.dataset.edge} : null;
    if (selection && event.button === 0 && !event.shiftKey) {
      this.selection = selection;
      this.onSelect?.(selection);
      if (selection.type === "edge" || this.disabled) { this.render(); return; }
      const node = this.graph.nodes.find(item => item.id === selection.id);
      this.drag = {type: "node", id: node.id, start: this.worldPoint(event), position: {...this.position(node)}, pointer: event.pointerId, moved: false};
    } else {
      this.drag = {type: "pan", clientX: event.clientX, clientY: event.clientY, view: {...this.view}, pointer: event.pointerId, moved: false};
    }
    this.svg.setPointerCapture(event.pointerId);
    this.svg.classList.add("is-dragging");
    event.preventDefault();
  }

  pointerMove(event) {
    if (!this.drag) return;
    const drag = this.drag;
    if (drag.type === "node") {
      const current = this.worldPoint(event);
      const x = Math.round((drag.position.x + current.x - drag.start.x) / 4) * 4;
      const y = Math.round((drag.position.y + current.y - drag.start.y) / 4) * 4;
      if (Math.abs(x - drag.position.x) + Math.abs(y - drag.position.y) > 3) drag.moved = true;
      this.previewPositions.set(drag.id, {x: Math.min(1000000, Math.max(-1000000, x)), y: Math.min(1000000, Math.max(-1000000, y))});
      if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = null; this.render(); });
    } else {
      const rect = this.svg.getBoundingClientRect();
      const dx = (event.clientX - drag.clientX) / rect.width * drag.view.width;
      const dy = (event.clientY - drag.clientY) / rect.height * drag.view.height;
      if (Math.abs(event.clientX - drag.clientX) + Math.abs(event.clientY - drag.clientY) > 3) drag.moved = true;
      this.view = {...drag.view, x: drag.view.x - dx, y: drag.view.y - dy};
      this.applyView();
    }
  }

  pointerUp(event) {
    const drag = this.drag;
    if (!drag) return;
    this.drag = null;
    this.svg.classList.remove("is-dragging");
    if (this.svg.hasPointerCapture(event.pointerId)) this.svg.releasePointerCapture(event.pointerId);
    if (drag.type === "node" && drag.moved) this.onMove?.(drag.id, this.previewPositions.get(drag.id) ?? drag.position);
    else if (drag.type === "pan" && !drag.moved) { this.selection = null; this.onSelect?.(null); }
    this.previewPositions.clear();
    this.render();
  }

  cancelDrag() {
    // - A lost pointer or OS interruption must never commit a half-finished drag.
    this.drag = null;
    this.previewPositions.clear();
    this.svg.classList.remove("is-dragging");
    this.render();
  }

  keyDown(event) {
    const nodeElement = event.target.closest("[data-node]");
    const edgeElement = event.target.closest("[data-edge]");
    const selection = nodeElement ? {type: "node", id: nodeElement.dataset.node} : edgeElement ? {type: "edge", id: edgeElement.dataset.edge} : this.selection;
    if (event.key === "Escape") { this.selection = null; this.onSelect?.(null); this.render(); return; }
    if (!selection) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); this.selection = selection; this.onSelect?.(selection); this.render(); return;
    }
    if (this.disabled) return;
    if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); this.onDelete?.(selection); return; }
    const direction = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
    if (selection.type === "node" && direction) {
      event.preventDefault();
      const node = this.graph.nodes.find(item => item.id === selection.id);
      const position = this.position(node);
      const distance = event.shiftKey ? 40 : 8;
      this.onMove?.(node.id, {x: position.x + direction[0] * distance, y: position.y + direction[1] * distance});
      requestAnimationFrame(() => this.svg.querySelector(`[data-node="${CSS.escape(node.id)}"]`)?.focus({preventScroll: true}));
    }
  }
}
