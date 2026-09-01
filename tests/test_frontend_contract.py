"""
Verify the real visual-editor/backend editing contract across both languages.
- Node imports the production graph-state module and computes the operation batch.
- Python validates that batch as an EditRequest and applies the real graph editor.
- Fixtures travel through JSON on stdin; no project source program is executed.
- Assertions describe the resulting reviewed graph rather than duplicating the
  JavaScript diff algorithm in a second implementation.
- Skip only when Node is unavailable; no browser or model provider is required.
"""

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from backend.graph_edits import EditRequest, apply_edits
from backend.graph_models import Evidence, GraphDocument, GraphEdge, GraphNode, Position, SourceFile


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
DIFF_SCRIPT = """
import {graphChanges, newId, NODE_KINDS} from './frontend/graph-state.js';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const working = input.working ?? JSON.parse(JSON.stringify(input.base));
const identities = [];
if (input.addManualKinds) {
  for (const kind of NODE_KINDS) {
    const nodeId = newId('node');
    const edgeId = newId('edge');
    identities.push({nodeId, edgeId, kind});
    working.nodes.push({
      id: nodeId, label: `Manual ${kind}`, kind, position: {x: 220, y: 480},
      source_path: 'main.py', script_type: 'python', resource_key: 'should-not-be-sent',
      details: {instruction: 'must stay out of the operation payload'},
    });
    working.edges.push({
      id: edgeId, source: input.base.nodes[0].id, target: nodeId, kind: 'calls',
      label: 'Manually reviewed', review_note: 'Added in the visual editor',
      origin: 'static', status: 'proposed', evidence: input.base.edges[0].evidence,
      condition: 'not supporting evidence for a new relationship',
    });
  }
}
process.stdout.write(JSON.stringify({
  operations: graphChanges(input.base, working), identities,
}));
"""


@unittest.skipUnless(NODE, "Node is needed to verify the actual frontend/backend contract")
class FrontendGraphContractTests(unittest.TestCase):
    def setUp(self):
        self.graph = GraphDocument(
            id = "draft_editor_contract", revision = 7, title = "Reviewed workflow", project_root = "/fixture/project",
            source_digest = "1" * 64,
            sources = [SourceFile(path = "main.py", sha256 = "2" * 64, script_type = "python", size_bytes = 32)],
            nodes = [
                GraphNode(id = "script_a", label = "main.py", kind = "script", source_path = "main.py", script_type = "python",
                          position = Position(x = 10, y = 30), resource_key = "script:main.py",
                          details = {"definitions": ["run"], "source_sha256": "2" * 64}),
                GraphNode(id = "process_b", label = "Original destination", kind = "process", position = Position(x = 280, y = 30)),
                GraphNode(id = "process_other", label = "Another retained step", kind = "process", position = Position(x = 540, y = 30)),
            ],
            edges = [GraphEdge(id = "edge_retained", source = "script_a", target = "process_b", kind = "calls",
                             label = "Suggested relationship", origin = "llm", status = "proposed", condition = "ready",
                             review_note = "Original proposal explanation",
                             evidence = [Evidence(source_path = "main.py", line_start = 1, line_end = 1,
                                                excerpt = "if ready: next_step()", extractor = "llm")])],
        )
        self.base = self.graph.model_dump(mode = "json")

    def frontend_changes(self, working = None, *, add_manual_kinds = False):
        payload = {"base": self.base, "addManualKinds": add_manual_kinds}
        if working is not None:
            payload["working"] = working
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", DIFF_SCRIPT], cwd = ROOT,
            input = json.dumps(payload, ensure_ascii = False), text = True,
            capture_output = True, timeout = 15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def apply_frontend_changes(self, working = None, *, add_manual_kinds = False):
        output = self.frontend_changes(working, add_manual_kinds = add_manual_kinds)
        request = EditRequest.model_validate({"expected_revision": self.graph.revision, "operations": output["operations"]})
        before = self.graph.model_dump(mode = "json")
        updated = apply_edits(self.graph, request.operations)
        self.assertEqual(self.graph.model_dump(mode = "json"), before, "Applying the batch must not mutate the saved baseline")
        return updated, output

    def test_reconnecting_retained_edge_to_new_node_before_removing_previous_destination(self):
        working = copy.deepcopy(self.base)
        working["nodes"] = [node for node in working["nodes"] if node["id"] != "process_b"]
        working["nodes"].append({"id": "node_new_destination", "label": "Correct destination", "kind": "process",
                                 "position": {"x": 450, "y": 280}})
        working["edges"][0]["target"] = "node_new_destination"
        updated, _ = self.apply_frontend_changes(working)
        self.assertNotIn("process_b", {node.id for node in updated.nodes})
        self.assertIn("node_new_destination", {node.id for node in updated.nodes})
        self.assertEqual([(edge.id, edge.source, edge.target) for edge in updated.edges],
                         [("edge_retained", "script_a", "node_new_destination")])
        self.assertEqual(updated.edges[0].origin, "user")
        self.assertEqual(updated.edges[0].status, "confirmed")
        self.assertEqual(updated.edges[0].evidence, [])

    def test_source_node_label_and_position_changes_keep_server_owned_identity_and_metadata(self):
        working = copy.deepcopy(self.base)
        source = working["nodes"][0]
        source.update(label = "Prepare the monthly data", position = {"x": -125.5, "y": 600},
                      source_path = "different.py", script_type = "sql", resource_key = "different-resource", details = {"replaced": True})
        updated, output = self.apply_frontend_changes(working)
        result = updated.nodes[0]
        self.assertEqual(result.label, "Prepare the monthly data")
        self.assertEqual(result.position, Position(x = -125.5, y = 600))
        for name in ("id", "kind", "source_path", "script_type", "resource_key", "details"):
            self.assertEqual(getattr(result, name), getattr(self.graph.nodes[0], name))
        self.assertEqual(updated.sources, self.graph.sources)
        self.assertEqual(updated.source_digest, self.graph.source_digest)
        self.assertEqual(set(output["operations"][0]), {"op", "id", "label", "position"})

    def test_relabeling_a_proposed_connection_does_not_confirm_or_remove_its_evidence(self):
        working = copy.deepcopy(self.base)
        working["edges"][0]["label"] = "A more readable explanation"
        updated, output = self.apply_frontend_changes(working)
        edge = updated.edges[0]
        self.assertEqual(edge.status, "proposed")
        self.assertEqual(edge.evidence, self.graph.edges[0].evidence)
        self.assertEqual(edge.condition, "ready")
        self.assertEqual(edge.review_note, "Original proposal explanation")
        self.assertNotIn("status", output["operations"][0])
        self.assertNotIn("evidence", output["operations"][0])

    def test_changing_an_endpoint_or_relation_type_confirms_user_edit_and_clears_old_evidence(self):
        for field, value in (("source", "process_other"), ("target", "process_other"), ("kind", "depends_on")):
            with self.subTest(changed_field = field):
                working = copy.deepcopy(self.base)
                working["edges"][0][field] = value
                updated, output = self.apply_frontend_changes(working)
                edge = updated.edges[0]
                self.assertEqual(edge.id, "edge_retained")
                self.assertEqual(getattr(edge, field), value)
                self.assertEqual(edge.origin, "user")
                self.assertEqual(edge.status, "confirmed")
                self.assertEqual(edge.evidence, [])
                self.assertIsNone(edge.condition)
                self.assertNotIn("origin", output["operations"][0])
                self.assertNotIn("evidence", output["operations"][0])

    def test_explicit_confirmation_keeps_the_original_source_citations(self):
        working = copy.deepcopy(self.base)
        working["edges"][0]["status"] = "confirmed"
        updated, _ = self.apply_frontend_changes(working)
        self.assertEqual(updated.edges[0].status, "confirmed")
        self.assertEqual(updated.edges[0].evidence, self.graph.edges[0].evidence)
        self.assertEqual(updated.edges[0].condition, "ready")
        self.assertEqual(updated.edges[0].source, self.graph.edges[0].source)
        self.assertEqual(updated.edges[0].target, self.graph.edges[0].target)

    def test_manual_node_types_identifiers_and_payload_fields_match_the_backend_contract(self):
        updated, output = self.apply_frontend_changes(add_manual_kinds = True)
        new_nodes = {node.id: node for node in updated.nodes if node.id not in {item.id for item in self.graph.nodes}}
        new_edges = {edge.id: edge for edge in updated.edges if edge.id != "edge_retained"}
        all_ids = {node.id for node in updated.nodes} | {edge.id for edge in updated.edges}
        self.assertEqual(len(all_ids), len(updated.nodes) + len(updated.edges))
        self.assertEqual(len(new_nodes), len(output["identities"]))
        self.assertGreater(len(new_nodes), 1)
        for item in output["identities"]:
            self.assertRegex(item["nodeId"], r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
            self.assertRegex(item["edgeId"], r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
            node, edge = new_nodes[item["nodeId"]], new_edges[item["edgeId"]]
            self.assertEqual(node.kind, item["kind"])
            self.assertIsNone(node.source_path)
            self.assertIsNone(node.script_type)
            self.assertIsNone(node.resource_key)
            self.assertEqual(node.details, {})
            self.assertEqual(edge.origin, "user")
            self.assertEqual(edge.status, "confirmed")
            self.assertEqual(edge.evidence, [])
            self.assertIsNone(edge.condition)
            self.assertEqual(edge.review_note, "Added in the visual editor")
        for operation in output["operations"]:
            if operation["op"] == "add_node":
                self.assertEqual(set(operation["node"]), {"id", "label", "kind", "position"})
            elif operation["op"] == "add_edge":
                self.assertEqual(set(operation["edge"]), {"id", "source", "target", "kind", "label", "review_note"})
            else:
                self.fail(f"Adding manual nodes/edges unexpectedly edited existing content: {operation}")


if __name__ == "__main__":
    unittest.main()
