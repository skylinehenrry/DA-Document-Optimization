"""Regression checks for truthful, grouped analysis coverage in review reports.

- Analysis limitations must not be reported as syntax failures.
- Grouping must preserve every occurrence and its source evidence.
- Building the report must never mutate reviewed topology or source metadata.
"""

import unittest

from backend.graph_diagnostics import graph_diagnostics
from backend.graph_models import Evidence, GraphDocument, GraphEdge, GraphIssue, GraphNode, SourceFile


class GraphDiagnosticsTests(unittest.TestCase):
    def graph(self, **changes):
        values = {
            "id": "graph_review", "project_root": "/project", "source_digest": "0" * 64,
            "sources": [SourceFile(path="main.py", sha256="1" * 64, script_type="python", size_bytes=40,
                                   status="partial")],
            "nodes": [GraphNode(id="main", label="main.py", kind="script", source_path="main.py"),
                      GraphNode(id="data", label="data.csv", kind="file")],
            "edges": [GraphEdge(id="read_data", source="data", target="main", kind="reads", status="proposed")],
            "issues": [GraphIssue(id="path_unknown", code="dynamic_file_path", severity="warning",
                                  message="The filename is computed.", node_ids=["main"],
                                  evidence=[Evidence(source_path="main.py", line_start=3, line_end=3,
                                                     excerpt="open(filename)", extractor="python_ast")])],
        }
        values.update(changes)
        return GraphDocument(**values)

    def test_successfully_parsed_sources_with_limitations_are_not_failures(self):
        graph = self.graph()
        before = graph.model_dump(mode="json")
        report = graph_diagnostics(graph)
        self.assertEqual(report["coverage"]["analyzed_sources"], 1)
        self.assertEqual(report["coverage"]["review_sources"], 1)
        self.assertEqual(report["coverage"]["failed_sources"], 0)
        self.assertEqual(report["coverage"]["proposed_relationships"], 1)
        self.assertIn("No source files failed analysis", report["summary"])
        self.assertNotIn("not fully parsed", report["summary"])
        self.assertEqual(report["sources"][0]["description"], "Analyzed; some dependencies need review")
        self.assertEqual(report["groups"][0]["category"], "review")
        self.assertTrue(report["has_review_items"])
        self.assertEqual(graph.model_dump(mode="json"), before)

    def test_real_failures_and_unreadable_sources_are_separately_counted(self):
        graph = self.graph(
            sources=[SourceFile(path="bad.py", sha256="2" * 64, script_type="python", size_bytes=12, status="failed")],
            nodes=[GraphNode(id="bad", label="bad.py", kind="script", source_path="bad.py"),
                   GraphNode(id="skipped", label="large.py", kind="script", script_type="python",
                             details={"analysis_status": "skipped", "relative_path": "large.py", "reason": "Too large"})],
            edges=[],
            issues=[GraphIssue(id="parse_error", code="source_analysis_failed", severity="error",
                               message="SyntaxError", node_ids=["bad"]),
                    GraphIssue(id="size_limit", code="source_skipped", severity="error",
                               message="Too large", node_ids=["skipped"])],
        )
        report = graph_diagnostics(graph)
        self.assertEqual(report["coverage"]["total_sources"], 2)
        self.assertEqual(report["coverage"]["analyzed_sources"], 0)
        self.assertEqual(report["coverage"]["failed_sources"], 1)
        self.assertEqual(report["coverage"]["skipped_sources"], 1)
        self.assertEqual({group["category"] for group in report["groups"]}, {"parse_failure"})
        skipped = next(group for group in report["groups"] if group["code"] == "source_skipped")
        self.assertEqual(skipped["occurrences"][0]["source_path"], "large.py")
        self.assertEqual(skipped["occurrences"][0]["line_start"], None)

    def test_grouped_occurrences_preserve_source_lines_evidence_and_new_codes(self):
        graph = self.graph()
        graph.issues.extend([
            GraphIssue(id="second_path", code="dynamic_file_path", severity="warning", message="Second path",
                       evidence=[Evidence(source_path="main.py", line_start=2, line_end=2,
                                          excerpt="open(other)", extractor="python_ast")]),
            GraphIssue(id="new_warning", code="future_code", severity="warning", message="Retain this new diagnostic",
                       node_ids=["main"], edge_ids=["read_data"]),
        ])
        report = graph_diagnostics(graph)
        self.assertEqual(report["counts"], {"total": 3, "error": 0, "warning": 3, "info": 0})
        paths = next(group for group in report["groups"] if group["code"] == "dynamic_file_path")
        self.assertEqual(paths["count"], 2)
        self.assertEqual([item["line_start"] for item in paths["occurrences"]], [2, 3])
        self.assertEqual(paths["occurrences"][0]["evidence"][0]["excerpt"], "open(other)")
        future = next(group for group in report["groups"] if group["code"] == "future_code")
        self.assertEqual(future["occurrences"][0]["message"], "Retain this new diagnostic")
        self.assertEqual(future["occurrences"][0]["edge_ids"], ["read_data"])
        self.assertEqual(report["sources"][0]["counts"]["warning"], 3)

    def test_intentional_exclusions_are_information_not_review_failures(self):
        graph = self.graph(
            sources=[SourceFile(path="main.py", sha256="1" * 64, script_type="python", size_bytes=4)],
            edges=[],
            issues=[GraphIssue(id="excluded", code="directory_skipped", severity="info", message="Excluded .venv")],
        )
        report = graph_diagnostics(graph)
        self.assertFalse(report["has_review_items"])
        self.assertEqual(report["groups"][0]["category"], "info")
        self.assertEqual(report["coverage"]["skipped_sources"], 0)
        self.assertEqual(report["coverage"]["complete_sources"], 1)

    def test_embedded_sql_error_does_not_claim_whole_python_source_failed(self):
        graph = self.graph(issues=[
            GraphIssue(id="sql_error", code="sql_parse_error", severity="error", message="Embedded SQL error",
                       evidence=[Evidence(source_path="main.py", line_start=6, extractor="sqlglot")]),
        ])
        report = graph_diagnostics(graph)
        self.assertEqual(report["coverage"]["failed_sources"], 0)
        self.assertEqual(report["coverage"]["review_sources"], 1)
        self.assertEqual(report["groups"][0]["category"], "parse_failure")
        self.assertEqual(report["counts"]["error"], 1)


if __name__ == "__main__":
    unittest.main()
