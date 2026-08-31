"""Regression checks for graph accuracy and for refusing unsafe guesses."""

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import unittest
from unittest.mock import patch

from backend.static_analysis import _Analysis, analyze_project


class StaticAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def analyze(self, **options):
        return analyze_project(self.root, **options)

    @staticmethod
    def relations(graph):
        nodes = {node.id: node for node in graph.nodes}
        return {(nodes[edge.source].label, nodes[edge.target].label, edge.kind) for edge in graph.edges}

    @staticmethod
    def codes(graph):
        return {issue.code for issue in graph.issues}

    def test_duplicate_basenames_have_stable_full_path_ids_and_no_invented_order(self):
        self.write("one/step.py", "answer = 1\n")
        self.write("two/step.py", "answer = 2\n")
        first, _ = self.analyze()
        second, _ = self.analyze()
        self.assertEqual({node.id for node in first.nodes}, {node.id for node in second.nodes})
        self.assertEqual(first.source_digest, second.source_digest)
        self.assertNotEqual(first.id, second.id, "New analyses must not overwrite reviewed graphs")
        self.assertEqual({node.source_path for node in first.nodes}, {"one/step.py", "two/step.py"})
        self.assertEqual(first.edges, [])

    def test_source_is_never_executed_and_raw_encoding_hash_is_preserved(self):
        marker = self.root / "must-not-exist"
        raw = ("# coding: cp1252\n# caf\xe9\nopen(" + repr(str(marker)) + ", 'w').write('executed')\n").encode("latin1")
        self.write("sample.py", raw)
        graph, snapshots = self.analyze()
        self.assertFalse(marker.exists())
        self.assertIn("café", snapshots["sample.py"])
        self.assertEqual(graph.sources[0].sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(graph.sources[0].size_bytes, len(raw))

    def test_exclusions_parse_failures_and_escaping_symlinks_stay_visible(self):
        self.write("bad.py", "def broken(\n")
        self.write("outputs/generated.py", "pass\n")
        self.write(".venv/library.py", "pass\n")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "outside.py"
        target.write_text("pass\n")
        (self.root / "outside.py").symlink_to(target)
        graph, snapshots = self.analyze()
        self.assertEqual(set(snapshots), {"bad.py"})
        self.assertEqual(graph.sources[0].status, "failed")
        self.assertEqual(len([node for node in graph.nodes if node.kind == "script"]), 2)
        skipped = next(node for node in graph.nodes if node.label == "outside.py")
        self.assertIsNone(skipped.source_path)
        self.assertEqual(skipped.details["analysis_status"], "skipped")
        self.assertTrue(any(issue.code == "source_skipped" and issue.severity == "error" for issue in graph.issues))
        self.assertIn("directory_skipped", self.codes(graph))

    def test_oversized_sources_are_not_read_or_assigned_fabricated_hashes(self):
        self.write("large.py", "#" * 100)
        with patch("backend.static_analysis.MAX_FILE_BYTES", 20):
            graph, snapshots = self.analyze()
        self.assertEqual(graph.sources, [])
        self.assertEqual(snapshots, {})
        self.assertEqual(graph.nodes[0].details["analysis_status"], "skipped")
        self.assertIn("source_skipped", self.codes(graph))

    def test_python_imports_calls_cycles_and_external_libraries(self):
        self.write("a.py", "import b as worker\nimport json\nworker.run()\n")
        self.write("b.py", "from a import entry\ndef run():\n    entry()\n")
        graph, _ = self.analyze()
        relations = self.relations(graph)
        self.assertIn(("a.py", "b.py", "imports"), relations)
        self.assertIn(("a.py", "b.py", "calls"), relations)
        self.assertIn(("b.py", "a.py", "imports"), relations)
        self.assertIn(("b.py", "a.py", "calls"), relations)
        self.assertEqual(len(graph.nodes), 2)
        self.assertTrue(all(edge.evidence and edge.evidence[0].line_start for edge in graph.edges))

    def test_relative_import_and_same_name_ambiguity_are_not_guessed(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/worker.py", "def run(): pass\n")
        self.write("worker.py", "def run(): pass\n")
        self.write("pkg/main.py", "from .worker import run\nrun()\nimport worker\nworker.run()\n")
        graph, _ = self.analyze()
        relations = self.relations(graph)
        self.assertIn(("pkg/main.py", "pkg/worker.py", "calls"), relations)
        self.assertNotIn(("pkg/main.py", "worker.py", "calls"), relations)
        self.assertIn("ambiguous_local_import", self.codes(graph))

    def test_shadowed_and_rebound_bindings_do_not_produce_false_calls(self):
        self.write("worker.py", "def run(): pass\n")
        self.write("shadow.py", "import worker\ndef invoke(worker):\n    worker.run()\n")
        self.write("rebound.py", "import worker\nworker = object()\nworker.run()\n")
        self.write("second.py", "import worker as w\nimport json as w\nw.run()\n")
        graph, _ = self.analyze()
        self.assertFalse(any(edge.kind == "calls" for edge in graph.edges))
        self.assertIn("rebound_import", self.codes(graph))

    def test_equivalent_fallback_and_repeated_imports_keep_the_same_callable(self):
        # - Package and direct-launch imports both refer to the same local file.
        # - Repeating the same external import does not rebind it to another value.
        # - The resulting call still requires a directly defined local callable.
        self.write("pkg/__init__.py", "")
        self.write("pkg/worker.py", "def run(): pass\n")
        self.write("pkg/main.py", "try:\n    from .worker import run\nexcept ImportError:\n    from worker import run\nimport json\nimport json\nrun()\n")
        graph, _ = self.analyze()
        self.assertNotIn("rebound_import", self.codes(graph))
        calls = [edge for edge in graph.edges if edge.kind == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "confirmed")
        self.assertIn(("pkg/main.py", "pkg/worker.py", "calls"), self.relations(graph))

    def test_different_fallbacks_and_assignments_still_block_guessed_calls(self):
        self.write("worker.py", "def run(): pass\n")
        self.write("other.py", "def run(): pass\n")
        self.write("fallback.py", "try:\n    from worker import run\nexcept ImportError:\n    from other import run\nrun()\n")
        self.write("reassigned.py", "import worker\nworker=other\nimport worker\nworker.run()\n")
        graph, _ = self.analyze()
        self.assertIn("rebound_import", self.codes(graph))
        self.assertFalse(any(edge.kind == "calls" for edge in graph.edges))

    def test_annotation_only_names_respect_module_and_function_binding_rules(self):
        # - Module annotations leave an existing import value unchanged.
        # - A function's annotation-only local hides the module import, unless
        #   that function itself imports the name and supplies its actual value.
        self.write("worker.py", "def run(): pass\n")
        self.write("main.py", "import worker\nworker: object\nworker.run()\ndef shadowed():\n    worker: object\n    worker.run()\ndef local_import():\n    import worker\n    worker: object\n    worker.run()\n")
        graph, _ = self.analyze()
        self.assertNotIn("rebound_import", self.codes(graph))
        calls = [edge for edge in graph.edges if edge.kind == "calls"]
        self.assertEqual({ev.line_start for edge in calls for ev in edge.evidence}, {3, 10})
        self.assertTrue(any("local_import" in (ev.note or "") for edge in calls for ev in edge.evidence))

    def test_comprehension_targets_do_not_leak_or_create_false_import_calls(self):
        self.write("worker.py", "def run(): pass\n")
        self.write("main.py", "import worker\nitems=[worker.run() for worker in values]\nitems={worker.run() for worker in values}\nitems={worker: worker.run() for worker in values}\nitems=(worker.run() for worker in values)\nworker.run()\n")
        graph, _ = self.analyze()
        self.assertNotIn("rebound_import", self.codes(graph))
        calls = [edge for edge in graph.edges if edge.kind == "calls"]
        self.assertEqual({ev.line_start for edge in calls for ev in edge.evidence}, {6})

    def test_comprehension_named_expression_rebinding_remains_conservative(self):
        self.write("worker.py", "def run(): pass\n")
        self.write("main.py", "import worker\nitems=[value for value in values if (worker := value)]\nworker.run()\n")
        graph, _ = self.analyze()
        self.assertIn("rebound_import", self.codes(graph))
        self.assertFalse(any(edge.kind == "calls" for edge in graph.edges))

    def test_lambda_bodies_resolve_imports_but_respect_shadowed_parameters(self):
        self.write("worker.py", "def run(): pass\n")
        self.write("main.py", "import worker\nrun=lambda: worker.run()\nshadow=lambda worker: worker.run()\ndefault=lambda worker=worker.run(): worker.run()\nkey=lambda pair: abs(pair[0]-pair[1])\n")
        graph, _ = self.analyze()
        self.assertNotIn("lambda_calls_unresolved", self.codes(graph))
        calls = [edge for edge in graph.edges if edge.kind == "calls"]
        self.assertEqual({ev.line_start for edge in calls for ev in edge.evidence}, {2, 4})
        self.assertTrue(any("<lambda>" in (ev.note or "") for edge in calls for ev in edge.evidence))

    def test_lambda_io_retains_targeted_dynamic_path_warning(self):
        self.write("main.py", "read=lambda path: open(path)\n")
        graph, _ = self.analyze()
        self.assertEqual(self.codes(graph), {"dynamic_file_path"})
        self.assertFalse(any(edge.kind == "reads" for edge in graph.edges))

    def test_class_lambdas_and_comprehensions_skip_class_local_imports(self):
        # - Defaults and a comprehension's first iterable run in the class body.
        # - Deferred lambda/comprehension bodies instead resolve free names in
        #   the module; a class-local import is not captured as a closure value.
        self.write("outer.py", "def run(): pass\n")
        self.write("inner.py", "def run(): pass\n")
        self.write("main.py", "import outer as worker\nclass Example:\n    import inner as worker\n    callback=lambda: worker.run()\n    default=lambda result=worker.run(): worker.run()\n    values=[worker.run() for item in worker.run()]\n")
        graph, _ = self.analyze()
        nodes = {node.id: node for node in graph.nodes}
        locations = {}
        for edge in graph.edges:
            if edge.kind == "calls":
                locations.setdefault(nodes[edge.target].source_path, []).extend(
                    (item.line_start, item.note) for item in edge.evidence)
        self.assertEqual({line for line, _ in locations["outer.py"]}, {4, 5, 6})
        self.assertEqual({line for line, _ in locations["inner.py"]}, {5, 6})
        self.assertTrue(all("<lambda>" not in note and "<comprehension>" not in note
                            for _, note in locations["inner.py"]))

    def test_nested_class_lambda_captures_enclosing_function_import(self):
        self.write("outer.py", "def run(): pass\n")
        self.write("inner.py", "def run(): pass\n")
        self.write("main.py", "def factory():\n    import outer as worker\n    class First:\n        import inner as worker\n        class Second:\n            callback=lambda: worker.run()\n")
        graph, _ = self.analyze()
        relations = self.relations(graph)
        self.assertIn(("main.py", "outer.py", "calls"), relations)
        self.assertNotIn(("main.py", "inner.py", "calls"), relations)

    def test_lambda_inside_class_comprehension_still_respects_iteration_binding(self):
        self.write("outer.py", "def run(): pass\n")
        self.write("main.py", "import outer as worker\nclass Example:\n    callbacks=[lambda: worker.run() for worker in values]\n")
        graph, _ = self.analyze()
        self.assertFalse(any(edge.kind == "calls" for edge in graph.edges))

    def test_local_open_is_not_mistaken_for_builtin_io_and_reexports_are_proposed(self):
        self.write("custom.py", "from remote_library import run\ndef open(path): pass\n")
        self.write("main.py", "from custom import open, run\nopen('not-a-file.csv')\nrun()\n")
        graph, _ = self.analyze()
        self.assertFalse(any(node.kind == "file" for node in graph.nodes))
        self.assertIn("imported_callable_unresolved", self.codes(graph))
        # Two observations of one module call share one edge; direct evidence is
        # retained alongside the uncertain exported-call observation.
        calls = [edge for edge in graph.edges if edge.kind == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0].evidence), 2)

    def test_nonstandard_import_roots_are_reported_without_fabricating_links(self):
        self.write("src/library.py", "def run(): pass\n")
        self.write("main.py", "import library\nlibrary.run()\n")
        graph, _ = self.analyze()
        self.assertIn("unresolved_import_root", self.codes(graph))
        self.assertFalse(any(edge.kind in {"imports", "calls"} for edge in graph.edges))

    def test_runtime_relative_resources_are_scoped_until_cwd_is_explicit(self):
        self.write("writer.py", "open('shared.csv', 'w')\n")
        self.write("reader.py", "open('shared.csv')\n")
        unresolved, _ = self.analyze()
        self.assertEqual(len([node for node in unresolved.nodes if node.kind == "file"]), 2)
        self.assertTrue(all(edge.status == "proposed" for edge in unresolved.edges))
        resolved, _ = self.analyze(working_directory=str(self.root))
        resources = [node for node in resolved.nodes if node.kind == "file"]
        self.assertEqual(len(resources), 1)
        file_id = resources[0].id
        self.assertTrue(any(edge.source == file_id and edge.kind == "reads" for edge in resolved.edges))
        self.assertTrue(any(edge.target == file_id and edge.kind == "writes" for edge in resolved.edges))
        self.assertTrue(all(edge.status == "confirmed" for edge in resolved.edges))

    def test_explicit_file_anchors_and_filename_punctuation_are_preserved(self):
        self.write("folder/reader.py", "from pathlib import Path\nbase=Path(__file__).parent.parent\n(base / 'data-a.csv').read_text()\n(base / 'data_a.csv').read_text()\n(base / 'DATA-a.csv').read_text()\n(base / 'データ.csv').read_text()\n")
        self.write("writer.py", "from pathlib import Path\n(Path(__file__).parent / 'data-a.csv').write_text('x')\n")
        graph, _ = self.analyze()
        resources = [node for node in graph.nodes if node.kind == "file"]
        self.assertEqual(len(resources), 4)
        paths = {node.details["normalized_path"] for node in resources}
        self.assertEqual(paths, {str(self.root / name) for name in ("data-a.csv", "data_a.csv", "DATA-a.csv", "データ.csv")})
        self.assertTrue(all(edge.status == "confirmed" for edge in graph.edges))

    def test_pandas_reader_writer_and_open_readwrite_mode_have_correct_direction(self):
        self.write("etl.py", "import pandas as pd\ndf=pd.read_csv('input.csv')\ndf.to_parquet('output.parquet')\nopen('state.bin', 'r+b')\n")
        graph, _ = self.analyze(working_directory=str(self.root))
        relations = self.relations(graph)
        self.assertIn(("input.csv", "etl.py", "reads"), relations)
        self.assertIn(("etl.py", "output.parquet", "writes"), relations)
        self.assertIn(("state.bin", "etl.py", "reads"), relations)
        self.assertIn(("etl.py", "state.bin", "writes"), relations)

    def test_dynamic_paths_modes_kwargs_and_cwd_changes_require_review(self):
        self.write("main.py", "import os\nopen(name)\nopen('mode.csv', mode=mode)\nopen('kwargs.csv', **settings)\nos.chdir(location)\nopen('later.csv')\n")
        graph, _ = self.analyze(working_directory=str(self.root))
        codes = self.codes(graph)
        self.assertTrue({"dynamic_file_path", "dynamic_file_mode", "dynamic_call_kwargs", "runtime_cwd_mutation"}.issubset(codes))
        files = [node for node in graph.nodes if node.kind == "file"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].details["resolution"], "runtime_cwd_unknown")
        self.assertTrue(any(issue.evidence and issue.evidence[0].line_start == 2 for issue in graph.issues))

    def test_dynamic_path_receivers_and_writer_destinations_are_not_silently_ignored(self):
        self.write("main.py", "from pathlib import Path\nimport pandas as pd\nPath(location).read_text()\ndf=pd.DataFrame()\ndf.to_csv(destination)\ndf.to_sql('table', connection, schema=dynamic_schema)\n")
        graph, _ = self.analyze()
        self.assertTrue({"dynamic_path_receiver", "dynamic_file_path", "dynamic_sql_table"}.issubset(self.codes(graph)))
        self.assertFalse(any(node.kind in {"file", "table"} for node in graph.nodes))

    def test_invalid_encoding_never_produces_relationships_from_corrupted_text(self):
        raw = b"open('bad\xff.csv')\n"
        self.write("bad.py", raw)
        graph, snapshots = self.analyze()
        self.assertEqual(graph.sources[0].sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(graph.sources[0].status, "failed")
        self.assertIn("\ufffd", snapshots["bad.py"])
        self.assertEqual(graph.edges, [])
        self.assertTrue(any(issue.code == "source_encoding_loss" and issue.severity == "error" for issue in graph.issues))

    def test_literal_subprocess_target_is_resolved_only_with_runtime_context(self):
        self.write("child.py", "pass\n")
        self.write("main.py", "import subprocess\nimport sys\nfrom pathlib import Path\nsubprocess.run([sys.executable, str(Path(__file__).parent / 'child.py')])\nsubprocess.run(['python', other])\n")
        graph, _ = self.analyze()
        self.assertIn(("main.py", "child.py", "calls"), self.relations(graph))
        self.assertIn("dynamic_launch", self.codes(graph))
        self.write("main.py", "import subprocess\nsubprocess.run(['python', 'child.py'])\n")
        graph, _ = self.analyze()
        self.assertNotIn(("main.py", "child.py", "calls"), {
            (next(n for n in graph.nodes if n.id == edge.source).source_path,
             next(n for n in graph.nodes if n.id == edge.target).source_path, edge.kind)
            for edge in graph.edges
        })
        self.assertIn("launch_target_unresolved", self.codes(graph))
        graph, _ = self.analyze(working_directory=str(self.root))
        self.assertIn(("main.py", "child.py", "calls"), self.relations(graph))

    def test_windows_launches_resolve_only_against_a_matching_windows_project_root(self):
        self.write("main.py", "pass\n")
        self.write("jobs/child.py", "pass\n")
        cases = [
            (PureWindowsPath("C:/Project"), "jobs\\child.py", True),
            (PureWindowsPath("C:/Project"), "c:/PROJECT/JOBS/CHILD.PY", True),
            (PureWindowsPath("C:/Project"), "D:/Project/jobs/child.py", False),
            (PureWindowsPath("//server/share/Project"), "jobs\\child.py", True),
            (PurePosixPath("/workspace"), "C:/workspace/jobs/child.py", False),
        ]
        for root, invocation, resolved in cases:
            with self.subTest(root=str(root), invocation=invocation):
                # Pure path flavors exercise Windows and UNC semantics on every
                # test host, without needing or accessing a foreign filesystem.
                analysis = _Analysis(self.root, None, None, None, "Test", None)
                analysis.discover()
                analysis.root = root
                analysis.cwd = root.as_posix()
                analysis.launch("main.py", invocation, analysis.evidence("main.py", "test"))
                graph = analysis.finish()
                edge = graph.edges[0]
                target = next(node for node in graph.nodes if node.id == edge.target)
                self.assertEqual(target.kind, "script" if resolved else "file")
                self.assertEqual(edge.status, "confirmed" if resolved else "proposed")
                if resolved:
                    self.assertEqual(target.source_path, "jobs/child.py")

    def test_loop_assignments_and_imports_are_not_assumed_to_exist_after_exit(self):
        self.write("worker.py", "def run(): pass\n")
        self.write("main.py", "outside='stable.csv'\nfor item in choices:\n    filename='loop.csv'\n    import worker as for_worker\nopen(filename)\nfor_worker.run()\nwhile condition:\n    other='while.csv'\n    import worker as while_worker\nopen(other)\nwhile_worker.run()\nopen(outside)\n")
        graph, _ = self.analyze(working_directory=str(self.root))
        self.assertFalse(any(edge.kind == "calls" for edge in graph.edges))
        self.assertEqual({node.label for node in graph.nodes if node.kind == "file"}, {"stable.csv"})
        self.assertIn("loop_binding_unresolved", self.codes(graph))
        unresolved = [issue for issue in graph.issues if issue.code == "dynamic_file_path"]
        self.assertEqual({issue.evidence[0].line_start for issue in unresolved}, {5, 10})

    def test_ordinary_loops_do_not_claim_dependency_resolution_failed(self):
        # - Iteration itself is valid source syntax and not a missing dependency.
        # - A filename assigned in a loop still stays unknown after the loop,
        #   producing a useful diagnostic at the file operation that needs it.
        self.write("ordinary.py", "count=0\nfor item in values:\n    count += 1\nwhile count:\n    count -= 1\n")
        self.write("dynamic.py", "for item in values:\n    filename='possible.csv'\nopen(filename)\n")
        graph, _ = self.analyze(working_directory=str(self.root))
        statuses = {source.path: source.status for source in graph.sources}
        self.assertEqual(statuses, {"ordinary.py": "parsed", "dynamic.py": "partial"})
        self.assertNotIn("loop_binding_unresolved", self.codes(graph))
        self.assertEqual(self.codes(graph), {"dynamic_file_path"})
        self.assertFalse(any(node.kind == "file" for node in graph.nodes))

    def test_sql_ctes_are_not_tables_but_real_same_named_qualified_tables_are(self):
        self.write("query.sql", "WITH t AS (SELECT * FROM raw.input) INSERT INTO output.final SELECT * FROM t JOIN raw.t ON 1=1;\n")
        graph, _ = self.analyze(database_namespace="warehouse")
        self.assertEqual({node.label for node in graph.nodes if node.kind == "table"}, {"raw.input", "output.final", "raw.t"})
        relations = self.relations(graph)
        self.assertIn(("query.sql", "output.final", "writes"), relations)
        self.assertNotIn(("output.final", "query.sql", "reads"), relations)
        self.assertIn(("raw.t", "query.sql", "reads"), relations)

    def test_nonrecursive_cte_can_read_physical_table_with_its_own_name(self):
        self.write("query.sql", "WITH x AS (SELECT * FROM x) SELECT * FROM x;\n")
        graph, _ = self.analyze(database_namespace="warehouse")
        self.assertEqual(len([node for node in graph.nodes if node.kind == "table"]), 1)
        self.assertEqual(len(graph.edges), 1)

    def test_sql_update_delete_aliases_and_foreign_keys_do_not_invent_resources(self):
        self.write("query.sql", "UPDATE t SET value=s.value FROM dbo.target t JOIN dbo.source s ON t.id=s.id;\nDELETE t FROM dbo.target t JOIN dbo.source s ON t.id=s.id;\nCREATE TABLE created(id INT REFERENCES foreign_table(id));")
        graph, _ = self.analyze(database_namespace="warehouse", sql_dialect="tsql")
        self.assertEqual({node.label for node in graph.nodes if node.kind == "table"}, {"dbo.target", "dbo.source", "created"})
        relations = self.relations(graph)
        self.assertIn(("query.sql", "dbo.target", "writes"), relations)
        self.assertNotIn(("query.sql", "dbo.source", "writes"), relations)
        self.assertIn("sql_schema_reference", self.codes(graph))

    def test_sql_table_identity_preserves_qualification_quoting_and_namespaces(self):
        self.write("a.sql", 'SELECT * FROM "a.b" JOIN a.b ON 1=1;')
        self.write("b.sql", 'SELECT * FROM "a.b";')
        graph, _ = self.analyze()
        self.assertEqual(len([node for node in graph.nodes if node.kind == "table"]), 3)
        self.assertIn("database_namespace_unresolved", self.codes(graph))
        graph, _ = self.analyze(database_namespace="host/db")
        self.assertEqual(len([node for node in graph.nodes if node.kind == "table"]), 2)
        other, _ = self.analyze(database_namespace="other-host/db")
        self.assertTrue({node.id for node in graph.nodes if node.kind == "table"}.isdisjoint(
            {node.id for node in other.nodes if node.kind == "table"}))

    def test_sql_temporary_tables_do_not_merge_between_sessions(self):
        for name in ("a.sql", "b.sql"):
            self.write(name, "CREATE TEMP TABLE tmp AS SELECT * FROM shared; SELECT * FROM tmp;")
        graph, _ = self.analyze(database_namespace="warehouse")
        tables = [node for node in graph.nodes if node.kind == "table"]
        self.assertEqual(len(tables), 3)
        self.assertEqual(len([node for node in tables if node.details["session_local"]]), 2)

    def test_unsupported_sql_and_parse_failures_are_visible(self):
        self.write("unsupported.sql", "EXEC some_procedure; SELECT * FROM known;")
        self.write("broken.sql", "SELECT * FROM (")
        graph, snapshots = self.analyze(database_namespace="warehouse", sql_dialect="tsql")
        self.assertEqual(set(snapshots), {"broken.sql", "unsupported.sql"})
        self.assertIn("unsupported_sql_statement", self.codes(graph))
        self.assertIn("sql_parse_error", self.codes(graph))
        self.assertEqual(next(source for source in graph.sources if source.path == "broken.sql").status, "failed")
        self.assertIn(("known", "unsupported.sql", "reads"), self.relations(graph))

    def test_alteryx_explicit_connections_io_and_missing_tools(self):
        self.write("flow.yxmd", '''<AlteryxDocument><Nodes>
          <Node ToolID="1"><GuiSettings Plugin="AlteryxBasePluginsGui.DbFileInput.DbFileInput"/><Properties><Configuration><File>%Engine.WorkflowDirectory%/in.csv</File></Configuration></Properties></Node>
          <Node ToolID="2"><GuiSettings Plugin="AlteryxBasePluginsGui.DbFileOutput.DbFileOutput"/><Properties><Configuration><File>%Engine.WorkflowDirectory%/out.csv</File></Configuration></Properties></Node>
          </Nodes><Connections>
          <Connection><Origin ToolID="1" Connection="Output"/><Destination ToolID="2" Connection="Input"/></Connection>
          <Connection><Origin ToolID="2"/><Destination ToolID="999"/></Connection>
          </Connections></AlteryxDocument>''')
        graph, _ = self.analyze()
        tool_nodes = {node.details["tool_id"]: node for node in graph.nodes if node.kind == "process"}
        self.assertEqual(set(tool_nodes), {"1", "2"})
        wires = [edge for edge in graph.edges if edge.kind == "control_flow"]
        self.assertEqual(len(wires), 1)
        self.assertEqual((wires[0].source, wires[0].target), (tool_nodes["1"].id, tool_nodes["2"].id))
        self.assertTrue(any(edge.kind == "reads" and edge.target == tool_nodes["1"].id for edge in graph.edges))
        self.assertTrue(any(edge.kind == "writes" and edge.source == tool_nodes["2"].id for edge in graph.edges))
        self.assertIn("dangling_alteryx_connection", self.codes(graph))

    def test_xml_entities_are_rejected_without_reading_external_content(self):
        secret = self.write("secret.txt", "DO NOT READ THIS VALUE")
        self.write("unsafe.yxmd", f'<!DOCTYPE root [<!ENTITY content SYSTEM "{secret.as_uri()}">]><AlteryxDocument>&content;</AlteryxDocument>')
        graph, _ = self.analyze()
        self.assertEqual(graph.sources[0].status, "failed")
        self.assertIn("alteryx_parse_error", self.codes(graph))
        self.assertNotIn("DO NOT READ THIS VALUE", graph.model_dump_json())

    def test_batch_script_directory_anchor_and_unknown_variables(self):
        self.write("child.py", "pass\n")
        self.write("main.bat", '@echo off\npython "%~dp0\\child.py"\ncall "%SCRIPTS%\\other.bat"\n')
        graph, _ = self.analyze()
        self.assertIn(("main.bat", "child.py", "calls"), self.relations(graph))
        self.assertIn("dynamic_batch_variable", self.codes(graph))
        self.assertFalse(any(node.label.endswith("other.bat") for node in graph.nodes))

    def test_batch_dynamic_cwd_does_not_reuse_initial_cwd(self):
        self.write("child.py", "pass\n")
        self.write("main.bat", "cd %SOMEWHERE%\npython child.py\n")
        graph, _ = self.analyze(working_directory=str(self.root))
        calls = [edge for edge in graph.edges if edge.kind == "calls"]
        self.assertEqual(len(calls), 1)
        target = next(node for node in graph.nodes if node.id == calls[0].target)
        self.assertEqual(target.kind, "file")
        self.assertEqual(target.details["resolution"], "runtime_cwd_unknown")


if __name__ == "__main__":
    unittest.main()
