/*
Editable graph state shared by the canvas, inspector and recovery storage.

- Keep the server revision as the immutable baseline for every editing session.
- Store edits locally until the user explicitly saves a complete revision.
- Derive the smallest supported API operation batch from that baseline.
- Refuse to restore local edits over a different server revision; a user must
  resolve that conflict instead of silently overwriting someone else's work.
- Keep this module independent of the DOM so its recovery rules can be tested.
*/

export const EDGE_KINDS = ["reads", "writes", "imports", "calls", "depends_on", "control_flow", "unknown"];
export const NODE_KINDS = ["process", "decision", "file", "table", "database", "api", "module", "unknown"];
export const clone = value => JSON.parse(JSON.stringify(value));
const equal = (left, right) => JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
const editable = graph => clone({nodes: graph.nodes, edges: graph.edges});

export function newId(prefix) {
  // - Identifiers survive relabeling and stay within the backend's ID contract.
  // - Never derive identity from a file basename or from a visible card label.
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

export function graphChanges(base, working) {
  /*
  Build one atomic save request, in dependency order.

  - Remove obsolete edges before removing their endpoints.
  - Add new nodes before adding or reconnecting edges to them.
  - Only send fields that the backend explicitly allows a user to change.
  - Source identity, analysis evidence and summary provenance stay server-owned.
  */
  const operations = [];
  const originalNodes = new Map(base.nodes.map(node => [node.id, node]));
  const originalEdges = new Map(base.edges.map(edge => [edge.id, edge]));
  const nodes = new Map(working.nodes.map(node => [node.id, node]));
  const edges = new Map(working.edges.map(edge => [edge.id, edge]));

  for (const id of originalEdges.keys()) if (!edges.has(id)) operations.push({op: "remove_edge", id});
  for (const node of nodes.values()) {
    const original = originalNodes.get(node.id);
    if (!original) {
      operations.push({op: "add_node", node: {
        id: node.id, label: node.label, kind: node.kind, position: node.position ?? null,
      }});
      continue;
    }
    const change = {op: "update_node", id: node.id};
    for (const key of ["label", "position"]) if (!equal(original[key], node[key])) change[key] = node[key] ?? null;
    if (Object.keys(change).length > 2) operations.push(change);
  }
  const newEdges = [];
  for (const edge of edges.values()) {
    const original = originalEdges.get(edge.id);
    if (!original) {
      newEdges.push({op: "add_edge", edge: {
        id: edge.id, source: edge.source, target: edge.target, kind: edge.kind,
        label: edge.label ?? null, review_note: edge.review_note ?? null,
      }});
      continue;
    }
    const change = {op: "update_edge", id: edge.id};
    for (const key of ["source", "target", "kind", "label", "status", "review_note"]) {
      if (!equal(original[key], edge[key])) change[key] = edge[key] ?? null;
    }
    if (Object.keys(change).length > 2) operations.push(change);
  }
  // - Reconnect retained arrows before removing an old endpoint: node removal
  //   cascades to incident arrows on the server. Reversing these steps could
  //   delete a connection this same atomic request intends to keep.
  for (const id of originalNodes.keys()) if (!nodes.has(id)) operations.push({op: "remove_node", id});
  operations.push(...newEdges);
  return operations;
}

export function validateEditableGraph(graph) {
  const nodes = new Set();
  const edges = new Set();
  const triples = new Set();
  for (const node of graph.nodes) {
    if (nodes.has(node.id)) throw new Error("A node with that identity already exists.");
    if (!node.label?.trim()) throw new Error("Give the node a name before applying this change.");
    if (node.position && (![node.position.x, node.position.y].every(Number.isFinite) ||
        Math.abs(node.position.x) > 1000000 || Math.abs(node.position.y) > 1000000)) {
      throw new Error("The node position is outside the supported canvas.");
    }
    nodes.add(node.id);
  }
  for (const edge of graph.edges) {
    if (edges.has(edge.id) || nodes.has(edge.id)) throw new Error("A connection with that identity already exists.");
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) throw new Error("Both ends of a connection must be existing nodes.");
    if (!EDGE_KINDS.includes(edge.kind)) throw new Error("Choose a supported connection type.");
    // - Existing parser output can contain parallel evidence records.
    // - The inspector checks new duplicate triples separately; do not make an
    //   otherwise valid saved draft uneditable because it has parallel edges.
    edges.add(edge.id);
    triples.add(`${edge.source}\0${edge.target}\0${edge.kind}`);
  }
  return graph;
}

export function validateConnection(graph, candidate, exceptId = null) {
  if (!candidate.source || !candidate.target) throw new Error("Choose a source and a destination.");
  if (graph.edges.some(edge => edge.id !== exceptId && edge.source === candidate.source &&
      edge.target === candidate.target && edge.kind === candidate.kind)) {
    throw new Error("This connection already exists. Select the existing arrow to edit it.");
  }
}

export class GraphSession {
  /*
  Keep one reversible local editing session tied to an immutable server revision.

  - Undo and redo store editable topology only; source evidence stays on the base.
  - Recovery data includes revision and source digest before it may be restored.
  - A mismatch remains a visible conflict instead of overwriting newer server work.
  - The submitted request ID links unsaved local state to a durable save job.
  */
  constructor(base, saved = null) {
    this.base = clone(base);
    this.graph = clone(base);
    this.undoStack = [];
    this.redoStack = [];
    this.conflict = null;
    this.submittedRequestId = saved?.submitted_request_id ?? null;
    if (saved?.nodes && saved?.edges) {
      if (saved.draft_id !== base.id || saved.base_revision !== base.revision || saved.source_digest !== base.source_digest) {
        this.conflict = saved;
      } else {
        try {
          this.graph = validateEditableGraph({...clone(base), ...editable(saved)});
        } catch {
          this.conflict = saved;
        }
      }
    }
  }

  get changes() { return graphChanges(this.base, this.graph); }
  get dirty() { return this.changes.length > 0; }

  change(update) {
    if (this.conflict) throw new Error("Resolve the revision conflict before editing this draft.");
    const before = editable(this.graph);
    const next = clone(this.graph);
    update(next);
    validateEditableGraph(next);
    if (equal(before, editable(next))) return false;
    this.undoStack.push(before);
    if (this.undoStack.length > 40) this.undoStack.shift();
    this.redoStack = [];
    this.graph = next;
    this.submittedRequestId = null;
    return true;
  }

  undo() {
    if (!this.undoStack.length || this.conflict) return false;
    this.redoStack.push(editable(this.graph));
    this.graph = {...this.graph, ...this.undoStack.pop()};
    this.submittedRequestId = null;
    return true;
  }

  redo() {
    if (!this.redoStack.length || this.conflict) return false;
    this.undoStack.push(editable(this.graph));
    this.graph = {...this.graph, ...this.redoStack.pop()};
    this.submittedRequestId = null;
    return true;
  }

  discard() {
    this.graph = clone(this.base);
    this.conflict = null;
    this.undoStack = [];
    this.redoStack = [];
    this.submittedRequestId = null;
  }

  recovery() {
    if (this.conflict) return clone(this.conflict);
    return {
      draft_id: this.base.id, base_revision: this.base.revision,
      source_digest: this.base.source_digest, saved_at: new Date().toISOString(),
      submitted_request_id: this.submittedRequestId, ...editable(this.graph),
    };
  }
}
