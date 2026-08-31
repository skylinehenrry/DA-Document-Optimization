/*
Meaningful editor regressions, without browser or provider dependencies.

- Exercise reversible user actions and recovery across a page/server restart.
- Protect the atomic-operation ordering required by backend cascading deletion.
- Keep display grouping and filename simplification independent of topology.
- Verify direct-neighbor focus using graphs where a misleading extra link exists.
*/
import test from "node:test";
import assert from "node:assert/strict";
import {GraphSession, graphChanges, validateConnection} from "../graph-state.js";
import {basename, connectionGroups, nodeName} from "../graph-presentation.js";
import {GraphEditor} from "../graph-editor.js";

function fixture() {
  return {
    id: "draft_test", revision: 1, title: "Example", source_digest: "a".repeat(64),
    sources: [{path: "one/a.py", status: "parsed", script_type: "python"}],
    nodes: [
      {id: "a", label: "one/a.py", kind: "script", source_path: "one/a.py", position: {x: 0, y: 0}},
      {id: "b", label: "b.py", kind: "module", position: {x: 400, y: 0}},
      {id: "c", label: "c.py", kind: "module", position: {x: 800, y: 0}},
    ],
    edges: [{id: "ab", source: "a", target: "b", kind: "imports", origin: "static", status: "confirmed", evidence: [{source_path: "one/a.py", line_start: 1, excerpt: "import b"}]}],
    issues: [],
  };
}

test("reconnecting a retained arrow precedes deletion of its former endpoint", () => {
  const base = fixture();
  const session = new GraphSession(base);
  session.change(graph => { graph.edges[0].target = "c"; graph.nodes = graph.nodes.filter(node => node.id !== "b"); });
  const operations = session.changes;
  assert.equal(operations[0].op, "update_edge");
  assert.equal(operations[0].target, "c");
  assert.deepEqual(operations[1], {op: "remove_node", id: "b"});
  assert.equal(base.edges[0].target, "b", "the saved graph remains immutable");
});

test("new endpoint is added before a retained arrow reconnects to it", () => {
  const base = fixture();
  const working = structuredClone(base);
  working.nodes.push({id: "new_target", label: "New target", kind: "process", position: {x: 400, y: 200}, source_path: null});
  working.edges[0].target = "new_target";
  const operations = graphChanges(base, working);
  assert.equal(operations[0].op, "add_node");
  assert.equal(operations[1].op, "update_edge");
  assert.equal(operations[0].node.source_path, undefined);
});

test("node removal and its incident edges undo as one user action", () => {
  const session = new GraphSession(fixture());
  session.change(graph => { graph.nodes = graph.nodes.filter(node => node.id !== "b"); graph.edges = []; });
  assert.equal(session.graph.nodes.length, 2);
  assert.equal(session.graph.edges.length, 0);
  assert.equal(session.undo(), true);
  assert.equal(session.graph.nodes.length, 3);
  assert.equal(session.graph.edges[0].id, "ab");
  assert.equal(session.dirty, false);
  assert.equal(session.redo(), true);
  assert.equal(session.graph.edges.length, 0);
});

test("reload restores unsaved topology at the same revision with its submission key", () => {
  const original = new GraphSession(fixture());
  original.change(graph => { graph.edges[0].target = "c"; });
  original.submittedRequestId = "request-123";
  const restored = new GraphSession(fixture(), JSON.parse(JSON.stringify(original.recovery())));
  assert.equal(restored.dirty, true);
  assert.equal(restored.graph.edges[0].target, "c");
  assert.equal(restored.submittedRequestId, "request-123");
  restored.change(graph => { graph.nodes[0].position.y = 200; });
  assert.equal(restored.submittedRequestId, null, "new edits cannot be cleared as part of an older submitted save");
});

test("new server revision retains conflicting edits without silently applying them", () => {
  const session = new GraphSession(fixture());
  session.change(graph => { graph.edges[0].target = "c"; });
  const recovery = session.recovery();
  const next = fixture(); next.revision = 2; next.nodes[0].label = "Saved elsewhere";
  const restored = new GraphSession(next, recovery);
  assert.equal(restored.conflict.base_revision, 1);
  assert.equal(restored.graph.edges[0].target, "b");
  assert.throws(() => restored.change(graph => { graph.edges = []; }), /conflict/i);
  assert.deepEqual(restored.recovery(), recovery);
  restored.discard();
  assert.equal(restored.conflict, null);
  assert.equal(restored.graph.nodes[0].label, "Saved elsewhere");
});

test("source digest changes and invalid local endpoints trigger recoverable conflicts", () => {
  const recovery = new GraphSession(fixture()).recovery();
  recovery.source_digest = "b".repeat(64);
  assert.ok(new GraphSession(fixture(), recovery).conflict);
  recovery.source_digest = "a".repeat(64);
  recovery.edges[0].target = "missing";
  assert.ok(new GraphSession(fixture(), recovery).conflict);
});

test("new edits cannot smuggle source metadata or original evidence into the API", () => {
  const base = fixture();
  const working = structuredClone(base);
  working.nodes[0].label = "Changed";
  working.nodes[0].source_path = "another.py";
  working.edges[0].status = "proposed";
  working.edges[0].evidence = [{source_path: "made_up.py"}];
  const changes = graphChanges(base, working);
  assert.deepEqual(changes, [{op: "update_node", id: "a", label: "Changed"}, {op: "update_edge", id: "ab", status: "proposed"}]);
});

test("parallel saved evidence records remain valid while new duplicates are rejected", () => {
  const graph = fixture();
  graph.edges.push({...structuredClone(graph.edges[0]), id: "ab_second"});
  const session = new GraphSession(graph);
  session.change(next => { next.edges[1].review_note = "Reviewed the second citation"; });
  assert.equal(session.changes.length, 1);
  assert.throws(() => validateConnection(graph, {source: "a", target: "b", kind: "imports"}), /already exists/);
});

test("filename labels support POSIX, Windows, UNC, drive-relative and resource URLs", () => {
  for (const [path, expected] of [
    ["project/sub/main.py", "main.py"], ["C:\\Finance\\run.bat", "run.bat"],
    ["\\\\server\\share\\exports\\orders.csv", "orders.csv"], ["C:input.json", "input.json"],
    ["s3://bucket/prefix/data.parquet?version=1#part", "data.parquet"],
    ["relative name with spaces.yxmd", "relative name with spaces.yxmd"],
  ]) assert.equal(basename(path), expected);
  assert.equal(nodeName({kind: "file", label: "output", details: {normalized_path: "C:\\Data\\result.csv"}}), "result.csv");
  assert.equal(nodeName({kind: "module", label: "module", source_path: "app/helpers.py"}), "helpers.py");
  assert.equal(nodeName({kind: "process", label: "Tool 7: Join customers", source_path: "workflows/load.yxmd"}), "Tool 7: Join customers");
});

test("grouping combines code references without deleting explicit direct relationships", () => {
  const graph = fixture();
  graph.edges.push(
    {...graph.edges[0], id: "ab_call", kind: "calls", status: "proposed"},
    {id: "bc", source: "b", target: "c", kind: "calls", status: "confirmed"},
    {id: "ac", source: "a", target: "c", kind: "imports", status: "confirmed"},
    {id: "ba", source: "b", target: "a", kind: "imports", status: "confirmed"},
  );
  const before = structuredClone(graph);
  const groups = connectionGroups(graph);
  assert.equal(groups.length, 4);
  assert.deepEqual(groups[0].member_ids, ["ab", "ab_call"]);
  assert.equal(groups[0].status, "proposed");
  assert.match(groups[0].display_label, /imports · calls · 2 references/);
  assert.ok(groups.some(group => group.id === "ac"), "an explicitly saved direct edge survives an alternate route");
  assert.ok(groups.some(group => group.id === "ba"), "opposite direction is a separate arrow");
  assert.deepEqual(graph, before, "display grouping is not a canonical graph edit");
});

test("different resource relation kinds stay in distinct visible arrows", () => {
  const graph = fixture(); graph.nodes[1].kind = "file";
  graph.edges.push({...graph.edges[0], id: "ab_writes", kind: "writes"});
  assert.equal(connectionGroups(graph).length, 2);
});

function focus(graph, id, grouped = true) {
  return GraphEditor.prototype.shownGraph.call({graph, grouped, selection: {type: "node", id}, filter: {query: "", kind: "all", scope: "neighbors"}});
}
test("one-hop focus excludes downstream hops and connections between neighbors", () => {
  const graph = fixture();
  graph.edges.push({id: "bc", source: "b", target: "c", kind: "calls", status: "confirmed"});
  let shown = focus(graph, "a");
  assert.deepEqual(shown.nodes.map(node => node.id), ["a", "b"]);
  assert.deepEqual(shown.edges.map(edge => edge.id), ["ab"]);
  graph.edges.push({id: "ac", source: "a", target: "c", kind: "imports", status: "confirmed"});
  shown = focus(graph, "a", false);
  assert.equal(shown.nodes.length, 3);
  assert.deepEqual(shown.edges.map(edge => edge.id), ["ab", "ac"], "B→C is not adjacent to selected A even though both nodes are visible");
});
