"""Check graph identity, review fidelity, layout, and safe report projections.

- Every canonical node and edge remains represented in SVG and interactive HTML.
- Saved positions remain stable while unsaved nodes receive deterministic layout.
- Routing avoids cards where possible and reports unavoidable overlap explicitly.
- Embedded labels, evidence, and graph JSON remain escaped as data, never markup.
- Compact direct-link presentation does not mutate or hide the canonical graph.
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
import re
import unittest
import xml.etree.ElementTree as ET

from backend.graph_models import (
    Evidence,
    GraphDocument,
    GraphEdge,
    GraphIssue,
    GraphNode,
    NarrativeSummary,
    Position,
    SourceFile,
    topology_signature,
)
from backend.graph_rendering import NODE_HEIGHT, NODE_WIDTH, layout_graph, render_graph_html, render_graph_svg
from backend.graph_presentation import direct_connections, file_card_label


class ReportParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs = True)
        self.nodes = {}
        self.edges = {}
        self.paths = {}
        self.script_tags = []
        self.all_attributes = []
        self.graph_json = ""
        self._in_graph_json = False
        self._edge = None
        self.feed(source)

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        self.all_attributes.append(attrs)
        if tag == "script":
            self.script_tags.append(attrs)
            self._in_graph_json = attrs.get("id") == "graph-data"
        if "data-node-id" in attrs:
            self.nodes[attrs["data-node-id"]] = attrs
        if "data-edge-id" in attrs:
            self.edges[attrs["data-edge-id"]] = attrs
            self._edge = attrs["data-edge-id"]
        if tag == "path" and "edge-path" in attrs.get("class", "").split():
            self.paths[self._edge] = attrs["d"]

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_graph_json = False
        if tag == "g":
            self._edge = None

    def handle_data(self, data):
        if self._in_graph_json:
            self.graph_json += data


def make_graph(nodes = (), edges = (), **overrides):
    source_paths = sorted({node.source_path for node in nodes if node.source_path})
    source_paths += sorted({evidence.source_path for edge in edges for evidence in edge.evidence} - set(source_paths))
    values = {
        "id": "graph_test",
        "project_root": "/example/project",
        "source_digest": "b" * 64,
        "sources": [SourceFile(path = path, sha256 = "a" * 64, script_type = "python", size_bytes = 10) for path in source_paths],
        "nodes": list(nodes),
        "edges": list(edges),
    }
    values.update(overrides)
    return GraphDocument(**values)


def node(node_id, **kwargs):
    return GraphNode(id = node_id, label = kwargs.pop("label", node_id), kind = kwargs.pop("kind", "process"), **kwargs)


def edge(edge_id, source, target, **kwargs):
    return GraphEdge(id = edge_id, source = source, target = target, kind = kwargs.pop("kind", "calls"), **kwargs)


def svg_elements(source, class_name):
    root = ET.fromstring(source)
    return [element for element in root.iter() if class_name in element.attrib.get("class", "").split()]


class DirectPresentationTests(unittest.TestCase):
    def test_file_cards_are_short_without_changing_paths_or_process_annotations(self):
        samples = [
            (node("script", kind = "script", label = "nested/deeper/job.py", source_path = "nested/deeper/job.py"), "job.py"),
            (node("posix", kind = "file", label = "/data/archive/report.csv"), "report.csv"),
            (node("windows", kind = "file", label = r"C:\Data exports\Monthly report.xlsx"), "Monthly report.xlsx"),
            (node("unc", kind = "file", label = r"\\server\share\folder\input.sql"), "input.sql"),
            (node("relative", kind = "file", label = r"C:input.json"), "input.json"),
            (node("url", kind = "file", label = "s3://bucket/prefix/data.parquet?version=1"), "data.parquet"),
            (node("tool", kind = "process", label = "Join sales / customers", source_path = "workflow.yxmd"), "Join sales / customers"),
        ]
        graph = make_graph([sample for sample, _ in samples])
        before = graph.model_dump(mode = "json")
        preview = ET.fromstring(render_graph_svg(graph))
        for sample, expected in samples:
            with self.subTest(sample = sample.id):
                self.assertEqual(file_card_label(sample), expected)
                card = next(item for item in preview.iter() if item.attrib.get("data-node-id") == sample.id)
                label = next(item for item in card if item.attrib.get("class") == "node-label")
                self.assertEqual("".join(label.itertext()), expected)
        report = render_graph_html(graph)
        self.assertIn('<div class="node-label">job.py</div>', report)
        self.assertNotIn('<div class="node-subtitle">', report)
        self.assertEqual(json.loads(ReportParser(report).graph_json), before)

    def test_compact_view_groups_references_without_creating_or_deleting_direct_links(self):
        graph = make_graph(
            [node("A", kind = "script"), node("B", kind = "script"), node("C", kind = "script")],
            [
                edge("a_import", "B", "A", kind = "imports"),
                edge("b_call", "B", "A", status = "proposed", evidence = [Evidence(source_path = "B.py", line_start = 7, extractor = "python_ast", excerpt = "A.run()")]),
                edge("c_import", "C", "B", kind = "imports"),
            ],
        )
        before = graph.model_dump(mode = "json")
        connections = direct_connections(graph)
        self.assertEqual({(item.source, item.target) for item in connections}, {("B", "A"), ("C", "B")})
        report = ReportParser(render_graph_html(graph))
        primary = report.edges["a_import"]
        self.assertEqual(primary["data-status"], "confirmed")
        self.assertEqual(primary["data-connection-status"], "proposed")
        self.assertEqual(json.loads(primary["data-member-ids"]), ["a_import", "b_call"])
        self.assertIn("connection-member", report.edges["b_call"]["class"])
        self.assertEqual(set(report.edges), {item.id for item in graph.edges})
        self.assertEqual(json.loads(report.graph_json), before)
        self.assertIn("A.run()", render_graph_html(graph))
        graph.edges.append(edge("explicit", "C", "A", kind = "imports"))
        graph.edges.append(edge("reverse", "A", "B", kind = "imports"))
        self.assertEqual(
            {(item.source, item.target) for item in direct_connections(graph)},
            {("B", "A"), ("C", "B"), ("C", "A"), ("A", "B")},
        )

    def test_different_data_relationships_do_not_collapse_into_one_arrow(self):
        graph = make_graph(
            [node("script", kind = "script"), node("file", kind = "file", label = "data.csv")],
            [edge("read", "file", "script", kind = "reads"), edge("write", "file", "script", kind = "writes")],
        )
        self.assertEqual(len(direct_connections(graph)), 2)
        self.assertNotIn("connection-member", ReportParser(render_graph_html(graph)).edges["read"]["class"])


class GraphRenderingTests(unittest.TestCase):
    def test_cycles_keep_their_descendants_and_disconnected_nodes(self):
        graph = make_graph(
            [node("A"), node("B"), node("C"), node("D")],
            [edge("e1", "A", "B"), edge("e2", "B", "A"), edge("e3", "B", "C")],
        )
        positions = layout_graph(graph)
        self.assertEqual(set(positions), {"A", "B", "C", "D"})
        self.assertEqual(positions["A"].x, positions["B"].x)
        self.assertGreater(positions["C"].x, positions["B"].x)
        self.assertNotEqual(positions["A"], positions["B"])
        reversed_graph = graph.model_copy(deep = True)
        reversed_graph.nodes.reverse()
        reversed_graph.edges.reverse()
        self.assertEqual(layout_graph(reversed_graph), positions)

    def test_large_graph_does_not_require_recursive_dfs(self):
        count = 1200
        graph = make_graph(
            [node(f"n{i}") for i in range(count)],
            [edge(f"e{i}", f"n{i}", f"n{i + 1}") for i in range(count - 1)],
        )
        positions = layout_graph(graph)
        self.assertEqual(len(positions), count)
        self.assertGreater(positions[f"n{count - 1}"].x, positions["n0"].x)

    def test_html_and_svg_preserve_every_identity_and_endpoint(self):
        graph = make_graph(
            [node("first.id"), node("second:id"), node("isolated")],
            [
                edge("a.edge", "first.id", "second:id", kind = "reads"),
                edge("b.edge", "second:id", "first.id", kind = "writes"),
                edge("c.edge", "first.id", "first.id", kind = "control_flow", status = "proposed"),
            ],
        )
        before = graph.model_dump(mode = "json")
        html_report = ReportParser(render_graph_html(graph))
        self.assertEqual(set(html_report.nodes), {node.id for node in graph.nodes})
        self.assertEqual(set(html_report.edges), {edge.id for edge in graph.edges})
        for relationship in graph.edges:
            rendered = html_report.edges[relationship.id]
            self.assertEqual(rendered["data-source"], relationship.source)
            self.assertEqual(rendered["data-target"], relationship.target)
            self.assertEqual(rendered["data-kind"], relationship.kind)
            self.assertEqual(rendered["data-status"], relationship.status)
        self.assertEqual(json.loads(html_report.graph_json), before)
        preview = render_graph_svg(graph)
        self.assertEqual({element.attrib["data-node-id"] for element in svg_elements(preview, "node")}, set(html_report.nodes))
        self.assertEqual({element.attrib["data-edge-id"] for element in svg_elements(preview, "edge")}, set(html_report.edges))
        metadata = ET.fromstring(preview).find("{http://www.w3.org/2000/svg}metadata")
        self.assertEqual(json.loads(metadata.text), before)
        self.assertEqual(graph.model_dump(mode = "json"), before)

    def test_parallel_relationships_and_self_loops_have_distinct_visible_paths(self):
        graph = make_graph(
            [node("A"), node("B")],
            [edge("read", "A", "B", kind = "reads"), edge("call", "A", "B"), edge("loop", "B", "B", kind = "control_flow")],
        )
        report = ReportParser(render_graph_html(graph))
        self.assertEqual(len(report.paths), 3)
        self.assertEqual(len(set(report.paths.values())), 3)
        self.assertTrue(all(attrs["data-route-status"] == "clear" for attrs in report.edges.values()))
        points = [tuple(map(float, pair)) for pair in re.findall(r"[ML](-?[\d.]+) (-?[\d.]+)", report.paths["loop"])]
        x, y = float(report.nodes["B"]["data-x"]), float(report.nodes["B"]["data-y"])
        self.assertTrue(any(px > x + NODE_WIDTH or py < y for px, py in points))
        self.assertNotEqual(points[0], points[-1])

    def test_long_edge_avoids_an_intervening_card(self):
        graph = make_graph(
            [node("A", position = Position(x = 50, y = 80)), node("obstacle", position = Position(x = 420, y = 80)), node("B", position = Position(x = 820, y = 80))],
            [edge("cross", "A", "B")],
        )
        report = ReportParser(render_graph_html(graph))
        self.assertEqual(report.edges["cross"]["data-route-status"], "clear")
        points = [tuple(map(float, pair)) for pair in re.findall(r"[ML](-?[\d.]+) (-?[\d.]+)", report.paths["cross"])]
        obstacle = report.nodes["obstacle"]
        left, top = float(obstacle["data-x"]), float(obstacle["data-y"])
        for a, b in zip(points, points[1:]):
            if a[1] == b[1]:
                intersects = top < a[1] < top + NODE_HEIGHT and max(min(a[0], b[0]), left) < min(max(a[0], b[0]), left + NODE_WIDTH)
            else:
                intersects = left < a[0] < left + NODE_WIDTH and max(min(a[1], b[1]), top) < min(max(a[1], b[1]), top + NODE_HEIGHT)
            self.assertFalse(intersects, "A visible connector should not pass through the intervening card")

    def test_distinct_relationships_do_not_merge_into_shared_line_segments(self):
        scenarios = {
            "fanout beside an independent input": make_graph(
                [node("input", position = Position(x = 64, y = 238)), node("runner", position = Position(x = 64, y = 412)),
                 node("first", position = Position(x = 524, y = 64)), node("second", position = Position(x = 524, y = 238))],
                [edge("e1", "input", "second", kind = "reads"), edge("e2", "runner", "second"), edge("e3", "runner", "first")],
            ),
            "opposite diagonals": make_graph(
                [node("left_top", position = Position(x = 64, y = 64)), node("left_bottom", position = Position(x = 64, y = 238)),
                 node("right_top", position = Position(x = 524, y = 64)), node("right_bottom", position = Position(x = 524, y = 238))],
                [edge("e1", "left_top", "right_bottom"), edge("e2", "left_bottom", "right_top")],
            ),
        }
        for name, graph in scenarios.items():
            with self.subTest(name = name):
                report = ReportParser(render_graph_html(graph))
                self.assertEqual(set(report.paths), {relationship.id for relationship in graph.edges})
                self.assertEqual(json.loads(report.graph_json), graph.model_dump(mode = "json"))
                segments = {}
                for edge_id, path in report.paths.items():
                    points = [tuple(map(float, pair)) for pair in re.findall(r"[ML](-?[\d.]+) (-?[\d.]+)", path)]
                    segments[edge_id] = list(zip(points, points[1:]))
                for index, (edge_id, parts) in enumerate(segments.items()):
                    for other_id, other_parts in list(segments.items())[index + 1:]:
                        for a, b in parts:
                            for c, d in other_parts:
                                for axis in (0, 1):
                                    if a[axis] == b[axis] == c[axis] == d[axis]:
                                        along = 1 - axis
                                        overlap = min(max(a[along], b[along]), max(c[along], d[along])) - max(min(a[along], b[along]), min(c[along], d[along]))
                                        self.assertLessEqual(overlap, 0, f"{edge_id} and {other_id} must not appear joined along a line")
                self.assertTrue(all(attrs["data-route-status"] == "clear" for attrs in report.edges.values()))

    def test_unavoidable_shared_connectors_have_a_notice_and_keep_every_edge(self):
        graph = make_graph(
            [node("A", position = Position(x = 100, y = 100)), node("B", position = Position(x = 100, y = 100)), node("C", position = Position(x = 600, y = 100))],
            [edge("a", "A", "C"), edge("b", "B", "C")],
        )
        for rendered in (render_graph_html(graph), render_graph_svg(graph)):
            self.assertIn("connectors share a line segment", rendered)
            self.assertEqual(set(ReportParser(rendered).edges), {"a", "b"})

    def test_saved_positions_are_exact_and_missing_positions_do_not_overlap_them(self):
        graph = make_graph([node("manual", position = Position(x = 64, y = 64)), node("automatic")])
        before = graph.model_dump(mode = "json")
        positions = layout_graph(graph)
        self.assertEqual(positions["manual"], Position(x = 64, y = 64))
        self.assertGreaterEqual(positions["automatic"].y, positions["manual"].y + NODE_HEIGHT)
        positions["manual"].x += 1
        self.assertEqual(graph.model_dump(mode = "json"), before)

    def test_negative_saved_positions_get_only_a_uniform_viewport_translation(self):
        graph = make_graph(
            [node("A", position = Position(x = -320, y = -180)), node("B", position = Position(x = 180, y = 40))],
            [edge("ab", "A", "B")],
        )
        positions = layout_graph(graph)
        self.assertEqual(positions["A"], graph.nodes[0].position)
        self.assertEqual(positions["B"], graph.nodes[1].position)
        report = ReportParser(render_graph_html(graph))
        shifts = [(float(report.nodes[node.id]["data-x"]) - node.position.x, float(report.nodes[node.id]["data-y"]) - node.position.y) for node in graph.nodes]
        self.assertEqual(shifts[0], shifts[1])
        self.assertTrue(all(float(attrs["data-x"]) >= 0 and float(attrs["data-y"]) >= 0 for attrs in report.nodes.values()))

    def test_summaries_attach_only_by_id_with_duplicate_basenames(self):
        graph = make_graph([
            node("alpha_main", label = "main.py", kind = "script", source_path = "alpha/main.py", script_type = "python"),
            node("beta_main", label = "main.py", kind = "script", source_path = "beta/main.py", script_type = "python"),
        ])
        signature = topology_signature(graph)
        summaries = {
            "alpha_main": NarrativeSummary(high_level = "Alpha purpose", detailed = "Alpha detailed processing"),
            "beta_main": NarrativeSummary(high_level = "Beta purpose", detailed = "Beta detailed processing"),
            "main.py": NarrativeSummary(high_level = "Must never attach", detailed = "Wrong basename summary"),
        }
        report = ReportParser(render_graph_html(graph, summaries))
        self.assertEqual(report.nodes["alpha_main"]["data-high-level-summary"], "Alpha purpose")
        self.assertEqual(report.nodes["beta_main"]["data-high-level-summary"], "Beta purpose")
        self.assertEqual(topology_signature(graph), signature)
        wrong = ReportParser(render_graph_html(graph, {"main.py": summaries["main.py"]}))
        self.assertEqual(wrong.nodes["alpha_main"]["data-high-level-summary"], "")
        self.assertEqual(wrong.nodes["beta_main"]["data-high-level-summary"], "")

    def test_untrusted_text_cannot_escape_html_svg_or_json(self):
        hostile = '</script><script>alert("x")</script><img src=x onerror="bad()"> & {{ title }}'
        graph = make_graph(
            [node("source", kind = "script", script_type = "python", source_path = "main.py", label = hostile, details = {"description": hostile}), node("target")],
            [edge("danger", "source", "target", label = hostile, condition = hostile, evidence = [Evidence(source_path = "main.py", line_start = 2, excerpt = hostile, extractor = "static")])],
            title = hostile,
        )
        rendered = render_graph_html(graph, {"source": NarrativeSummary(high_level = hostile, detailed = hostile)})
        report = ReportParser(rendered)
        self.assertEqual(json.loads(report.graph_json), graph.model_dump(mode = "json"))
        self.assertEqual(report.nodes["source"]["data-high-level-summary"], hostile)
        self.assertFalse(any(key.startswith("on") for attrs in report.all_attributes for key in attrs))
        self.assertEqual(len(report.script_tags), 3)
        self.assertNotIn('<script>alert("x")', rendered)
        self.assertIn("{{ title }}", report.nodes["source"]["data-high-level-summary"])
        preview = render_graph_svg(graph)
        root = ET.fromstring(preview)
        self.assertEqual(len(list(root.iter("{http://www.w3.org/2000/svg}script"))), 0)
        metadata = root.find("{http://www.w3.org/2000/svg}metadata")
        self.assertEqual(json.loads(metadata.text), graph.model_dump(mode = "json"))

    def test_partial_analysis_proposals_and_source_evidence_are_visible(self):
        graph = make_graph(
            [node("A", kind = "script", source_path = "main.py", script_type = "python"), node("B")],
            [edge("uncertain", "A", "B", status = "proposed", evidence = [Evidence(source_path = "main.py", line_start = 12, line_end = 14, excerpt = "run(next_step)", extractor = "python_ast")])],
            sources = [SourceFile(path = "main.py", sha256 = "a" * 64, script_type = "python", size_bytes = 10, status = "partial")],
            issues = [GraphIssue(id = "issue1", severity = "warning", code = "dynamic_call", message = "Invocation cannot be resolved.")],
        )
        for rendered in (render_graph_html(graph, summary_statuses = {"A": "failed"}), render_graph_svg(graph)):
            self.assertIn("1 of 1 source files analyzed", rendered)
            self.assertIn("Dependency review:", rendered)
            self.assertNotIn("not fully parsed", rendered)
            self.assertIn("proposed relationship", rendered)
            self.assertIn("main.py:12–14", rendered)
            self.assertIn("run(next_step)", rendered)
        html_report = render_graph_html(graph, summary_statuses = {"A": "failed"})
        self.assertIn("Invocation cannot be resolved.", html_report)
        self.assertIn("requested model summaries were unavailable for 1 file", html_report)

    def test_grouped_diagnostics_show_locations_and_safe_model_failure_reasons(self):
        graph = make_graph(
            [node("source", kind = "script", source_path = "main.py", script_type = "python")],
            issues = [GraphIssue(
                id = f"issue{line}", severity = "warning", code = "dynamic_path_receiver", message = "Path is dynamic.",
                evidence = [Evidence(source_path = "main.py", line_start = line, line_end = line,
                                   excerpt = "target.read_text()", extractor = "python_ast")],
            ) for line in (4, 8, 12)],
        )
        rendered = render_graph_html(graph, summary_statuses = {"source": "fallback"},
                                     summary_errors = {"source": 'Missing package <script>alert("x")</script>'})
        self.assertEqual(rendered.count('data-issue-code="dynamic_path_receiver"'), 1)
        self.assertIn("3 occurrence(s)", rendered)
        self.assertIn("<code>main.py:8</code>", rendered)
        self.assertIn("Why local descriptions were used", rendered)
        self.assertNotIn('<script>alert("x")</script>', rendered)
        self.assertIn("Missing package &lt;script&gt;", rendered)
        self.assertEqual(json.loads(ReportParser(rendered).graph_json), graph.model_dump(mode = "json"))

    def test_overlapping_manual_cards_are_retained_with_an_explicit_notice(self):
        graph = make_graph(
            [node("A", position = Position(x = 100, y = 100)), node("B", position = Position(x = 120, y = 100))],
            [edge("ab", "A", "B")],
        )
        rendered = render_graph_html(graph)
        report = ReportParser(rendered)
        self.assertEqual(set(report.nodes), {"A", "B"})
        self.assertEqual(set(report.edges), {"ab"})
        self.assertIn("positions overlap or obstruct", rendered)

    def test_empty_graph_is_a_valid_but_clearly_empty_preview(self):
        graph = make_graph()
        self.assertEqual(layout_graph(graph), {})
        report = ReportParser(render_graph_html(graph))
        self.assertEqual(report.nodes, {})
        self.assertEqual(report.edges, {})
        self.assertEqual(json.loads(report.graph_json), graph.model_dump(mode = "json"))
        preview = render_graph_svg(graph)
        root = ET.fromstring(preview)
        self.assertGreater(int(root.attrib["width"]), 0)
        self.assertGreater(int(root.attrib["height"]), 0)
        self.assertIn("This graph has no nodes.", preview)

    def test_renderer_revalidates_mutated_models_instead_of_dropping_dangling_edges(self):
        graph = make_graph([node("A"), node("B")], [edge("ab", "A", "B")])
        graph.edges[0].target = "missing"
        for projection in (layout_graph, render_graph_html, render_graph_svg):
            with self.assertRaises(ValueError):
                projection(graph)


if __name__ == "__main__":
    unittest.main()
