"""Protect storage, review, enrichment, and generation invariants.

- Build graphs and source snapshots directly so a failure identifies workflow or
  integrity behavior instead of a language-extraction heuristic.
- Use fake structured-output providers to exercise caching, fallback, cancellation,
  prompt boundaries, and topology protection without a live model or sign-in.
- Verify optimistic revisions, atomic publication, source ownership, and artifact
  recovery using temporary stores and output folders only.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.graph_edits import EditRequest, apply_edits
from backend.graph_models import Evidence, GraphDocument, GraphEdge, GraphIssue, GraphNode, NarrativeSummary, SourceFile, topology_signature
from backend.project_identity import flowchart_filename
from backend.workflow_service import GenerateRequest, ReviewRequired, SuggestRequest, WorkflowService, _write_directory
from backend.workflow_store import RevisionConflict, WorkflowStore


SOURCE = 'read_data("input.csv")\nrun_next()\n'


class FakeSummaryChain:
    def __init__(self, result = None):
        self.payloads = []
        self.result = result

    async def ainvoke(self, messages):
        payload = json.loads(messages[-1][1])
        self.payloads.append(payload)
        if self.result is not None:
            return self.result
        return NarrativeSummary(high_level = "Snapshot narrative", detailed = f"Recorded source: {payload['reviewed_context']['source_path']}")


class BlockingSummaryChain(FakeSummaryChain):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ainvoke(self, messages):
        self.started.set()
        await self.release.wait()
        return await super().ainvoke(messages)


class FakeSuggestionChain:
    def __init__(self, edges, block = False):
        self.edges = edges
        self.payloads = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def ainvoke(self, messages):
        self.payloads.append(json.loads(messages[-1][1]))
        self.started.set()
        await self.release.wait()
        return {"edges": self.edges, "unclear_items": []}


def suggestion(source = "script_main", target = "next_step", kind = "calls", line_start = 2, line_end = 2, excerpt = "run_next()"):
    return {"source": source, "target": target, "kind": kind, "explanation": "Possible relationship found in the quoted source.",
            "line_start": line_start, "line_end": line_end, "excerpt": excerpt}


class WorkflowFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "main.py").write_text(SOURCE, encoding = "utf-8")
        self.output = self.root / "documents"
        self.store = WorkflowStore(self.root / "store")
        self.service = WorkflowService(self.store)
        self.graph = GraphDocument(
            id = "draft_fixture", project_root = str(self.project), source_digest = hashlib.sha256(SOURCE.encode()).hexdigest(),
            sources = [SourceFile(path = "main.py", sha256 = hashlib.sha256(SOURCE.encode()).hexdigest(), script_type = "python", size_bytes = len(SOURCE.encode()))],
            nodes = [
                GraphNode(id = "script_main", kind = "script", label = "main.py", source_path = "main.py", script_type = "python"),
                GraphNode(id = "input_file", kind = "file", label = "input.csv", resource_key = "file:input.csv"),
                GraphNode(id = "next_step", kind = "process", label = "Next step"),
            ],
            edges = [GraphEdge(id = "read_edge", source = "input_file", target = "script_main", kind = "reads",
                             evidence = [Evidence(source_path = "main.py", line_start = 1, line_end = 1, excerpt = 'read_data("input.csv")', extractor = "fixture")])],
        )
        self.store.create(self.graph, {"main.py": SOURCE}, self.output)

    def edit_request(self, revision = 1, **operation):
        return EditRequest.model_validate({"expected_revision": revision, "operations": [operation]})

    def store_edit(self, request):
        return self.store.update(self.graph.id, request.expected_revision, lambda graph: apply_edits(graph, request.operations), {"action": "test_edit"})


class WorkflowStoreInvariantTests(WorkflowFixture, unittest.TestCase):
    def test_bad_edit_batch_rolls_back_all_changes_history_and_suppression(self):
        request = EditRequest.model_validate({"expected_revision": 1, "operations": [
            {"op": "update_node", "id": "script_main", "label": "Should roll back"},
            {"op": "remove_edge", "id": "read_edge"},
            {"op": "add_edge", "edge": {"id": "bad_edge", "source": "script_main", "target": "missing", "kind": "calls"}},
        ]})
        with self.assertRaises(ValueError):
            self.store_edit(request)
        self.assertEqual(self.store.load(self.graph.id), self.graph)
        self.assertEqual(len(self.store.history(self.graph.id)), 1)
        self.assertEqual(self.store.suppressed_edges(self.graph.id), set())

    def test_compare_and_swap_rejects_a_stale_writer_without_running_transform(self):
        updated = self.store_edit(self.edit_request(op = "update_node", id = "script_main", label = "First edit"))
        called = []
        with self.assertRaises(RevisionConflict):
            self.store.update(self.graph.id, 1, lambda graph: called.append(graph), {"action": "stale"})
        self.assertEqual(called, [])
        self.assertEqual(self.store.load(self.graph.id), updated)
        self.assertEqual(self.store.load(self.graph.id, 1), self.graph)

    def test_concurrent_edits_have_exactly_one_winner(self):
        barrier = threading.Barrier(2)

        def update(label):
            barrier.wait(timeout = 3)
            try:
                graph = self.store_edit(self.edit_request(op = "update_node", id = "script_main", label = label))
                return "saved", graph
            except RevisionConflict:
                return "conflict", None

        with ThreadPoolExecutor(max_workers = 2) as workers:
            results = list(workers.map(update, ["First", "Second"]))
        self.assertEqual(sorted(result[0] for result in results), ["conflict", "saved"])
        winner = next(result[1] for result in results if result[0] == "saved")
        self.assertEqual(self.store.load(self.graph.id), winner)
        self.assertEqual(len(self.store.history(self.graph.id)), 2)

    def test_edits_cannot_replace_source_identity(self):
        for field, value in (("project_root", "/different/project"), ("source_digest", "f" * 64), ("sources", [])):
            def replace_source(graph):
                setattr(graph, field, value)
                return graph
            with self.subTest(field = field), self.assertRaises(ValueError):
                self.store.update(self.graph.id, 1, replace_source, {"action": "bad_source_change"})
        self.assertEqual(self.store.load(self.graph.id), self.graph)

    def test_snapshot_integrity_check_detects_corruption(self):
        with self.store.connection() as db:
            db.execute("UPDATE sources SET content=? WHERE draft_id=?", ("changed snapshot", self.graph.id))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.snapshots(self.graph.id)

    def test_a_new_draft_requires_known_and_complete_source_snapshots(self):
        other = self.graph.model_copy(update = {"id": "another_draft"}, deep = True)
        with self.assertRaises(ValueError):
            self.store.create(other, {}, self.output)
        with self.assertRaises(ValueError):
            self.store.create(other, {"main.py": SOURCE, "unrelated.py": "other content"}, self.output)
        self.assertEqual(len(self.store.list_drafts()), 1)

    def test_reconnection_does_not_reuse_old_evidence_and_records_suppression(self):
        changed = self.store_edit(self.edit_request(op = "update_edge", id = "read_edge", source = "script_main", target = "next_step", kind = "calls"))
        relationship = changed.edges[0]
        self.assertEqual(relationship.origin, "user")
        self.assertEqual(relationship.status, "confirmed")
        self.assertEqual(relationship.evidence, [])
        self.assertIn(("input_file", "script_main", "reads"), self.store.suppressed_edges(self.graph.id))
        self.assertEqual(self.store.load(self.graph.id, 1).edges[0].evidence, self.graph.edges[0].evidence)

    def test_cosmetic_edit_does_not_confirm_a_proposed_relationship(self):
        proposed = self.graph.model_copy(deep = True)
        proposed.edges[0].status = "proposed"
        proposed.edges[0].origin = "llm"
        request = self.edit_request(op = "update_edge", id = "read_edge", label = "A clearer relationship label")
        edited = apply_edits(proposed, request.operations)
        self.assertEqual(edited.edges[0].status, "proposed")
        explicit = self.edit_request(op = "update_edge", id = "read_edge", status = "confirmed")
        confirmed = apply_edits(edited, explicit.operations)
        self.assertEqual(confirmed.edges[0].status, "confirmed")

    def test_failed_artifact_write_leaves_no_partial_directory(self):
        destination = self.root / "artifacts" / "generation"
        write_text = Path.write_text

        def fail_second(path, content, *args, **kwargs):
            if path.name == "second.json":
                raise OSError("Simulated disk failure")
            return write_text(path, content, *args, **kwargs)

        with patch.object(Path, "write_text", fail_second), self.assertRaises(OSError):
            _write_directory(destination, {"first.json": "{}", "second.json": "{}"})
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])

    def test_artifact_publication_cannot_overwrite_existing_output(self):
        destination = self.root / "artifacts"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("Existing artifact", encoding = "utf-8")
        with self.assertRaises(FileExistsError):
            _write_directory(destination, {"keep.txt": "Should not overwrite"})
        self.assertEqual(sentinel.read_text(), "Existing artifact")
        self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_export_failure_reports_the_committed_edit_and_can_be_retried(self):
        with patch("backend.workflow_service._write_directory", side_effect = OSError("Simulated export failure")):
            edited = self.service.edit(self.graph.id, self.edit_request(op = "remove_edge", id = "read_edge"))
        self.assertEqual(edited.revision, 2)
        self.assertEqual(self.store.load(self.graph.id), edited)
        self.assertTrue(self.service.describe(edited)["export_warning"])
        self.assertTrue(WorkflowService(self.store).describe(edited)["export_warning"], "Recovery warning must survive a service restart")
        recovered = self.service.save_draft_exports(edited)
        self.assertTrue(recovered)
        self.assertTrue(all(path.is_file() for path in recovered.values()))
        self.assertIsNone(self.service.describe(edited)["export_warning"])
        self.assertEqual(self.store.load(self.graph.id), edited, "Retrying a projection must not create another graph revision")


class WorkflowGenerationInvariantTests(WorkflowFixture, unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_frozen_sources_and_preserves_reviewed_graph(self):
        (self.project / "main.py").write_text("Different source on disk now\n", encoding = "utf-8")
        chain = FakeSummaryChain()
        with patch("backend.workflow_service.analyze_project", side_effect = AssertionError("Generation must not analyze again")):
            manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1, use_llm = True), summary_chain = chain)
        self.assertEqual([line["text"] for line in chain.payloads[0]["source_lines"]], SOURCE.splitlines())
        saved = GraphDocument.model_validate_json(self.service.artifact_path(self.graph.id, manifest["generation_id"], "workflow_graph.json").read_text())
        self.assertEqual(saved, self.graph)
        self.assertEqual(self.store.load(self.graph.id), self.graph)

    async def test_summary_cache_survives_layout_changes_but_not_relationship_changes(self):
        chain = FakeSummaryChain()
        await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1, use_llm = True), summary_chain = chain)
        await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1, use_llm = True), summary_chain = chain)
        self.assertEqual(len(chain.payloads), 1)
        moved = self.service.edit(self.graph.id, self.edit_request(op = "update_node", id = "script_main", position = {"x": 450, "y": 225}))
        await self.service.generate(self.graph.id, GenerateRequest(expected_revision = moved.revision, use_llm = True), summary_chain = chain)
        self.assertEqual(len(chain.payloads), 1, "Presentation changes should not discard a valid narrative")
        changed = self.service.edit(self.graph.id, self.edit_request(revision = moved.revision, op = "remove_edge", id = "read_edge"))
        await self.service.generate(self.graph.id, GenerateRequest(expected_revision = changed.revision, use_llm = True), summary_chain = chain)
        self.assertEqual(len(chain.payloads), 2, "Removed relationships must not survive in a cached summary")
        self.assertEqual(chain.payloads[-1]["reviewed_context"]["relationships"], [])

    async def test_summary_cache_separates_provider_identity_and_output_language(self):
        chain = FakeSummaryChain()
        for identity, language in (("model_a", "English"), ("model_b", "English"), ("model_b", "Japanese"), ("model_b", "Japanese")):
            await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1, use_llm = True, language = language),
                                        summary_chain = chain, provider_identity = {"model": identity})
        self.assertEqual(len(chain.payloads), 3)

    async def test_stale_generation_cannot_publish_or_overwrite_a_user_edit(self):
        chain = BlockingSummaryChain()
        generating = asyncio.create_task(self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1, use_llm = True), summary_chain = chain))
        await asyncio.wait_for(chain.started.wait(), timeout = 3)
        edited = self.service.edit(self.graph.id, self.edit_request(op = "remove_edge", id = "read_edge"))
        chain.release.set()
        with self.assertRaises(RevisionConflict):
            await asyncio.wait_for(generating, timeout = 5)
        self.assertEqual(self.store.load(self.graph.id), edited)
        self.assertIsNone(self.store.latest_generation(self.graph.id, revision = 1))
        generation_root = self.store.artifact_root(self.graph.id) / "generations"
        self.assertEqual(list(generation_root.iterdir()), [])

    async def test_deleted_relationship_is_not_recreated_by_optional_suggestions(self):
        edited = self.service.edit(self.graph.id, self.edit_request(op = "remove_edge", id = "read_edge"))
        chain = FakeSuggestionChain([suggestion("input_file", "script_main", "reads", 1, 1, 'read_data("input.csv")')])
        suggested = await self.service.suggest(self.graph.id, SuggestRequest(expected_revision = edited.revision), chain = chain)
        self.assertEqual(suggested.edges, [])
        self.assertEqual(topology_signature(suggested), topology_signature(edited))

    async def test_suggestions_require_real_local_evidence_and_remain_proposed(self):
        chain = FakeSuggestionChain([
            suggestion(),
            suggestion(source = "missing_node"),
            suggestion(source = "next_step", target = "script_main", excerpt = "invented source"),
            suggestion(source = "input_file", target = "next_step"),
        ])
        suggested = await self.service.suggest(self.graph.id, SuggestRequest(expected_revision = 1), chain = chain)
        self.assertEqual(len(suggested.edges), 2)
        original = next(edge for edge in suggested.edges if edge.id == "read_edge")
        added = next(edge for edge in suggested.edges if edge.id != "read_edge")
        self.assertEqual(original, self.graph.edges[0])
        self.assertEqual((added.source, added.target, added.kind, added.status, added.origin), ("script_main", "next_step", "calls", "proposed", "llm"))
        self.assertTrue(any(issue.code == "invalid_llm_suggestion" for issue in suggested.issues))
        with self.assertRaises(ReviewRequired):
            await self.service.generate(self.graph.id, GenerateRequest(expected_revision = suggested.revision))
        manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = suggested.revision, allow_proposed = True))
        self.assertTrue(manifest["has_proposed_edges"])
        self.assertEqual(self.store.load(self.graph.id), suggested)

    async def test_suggestions_cannot_replace_edits_made_while_the_model_was_running(self):
        chain = FakeSuggestionChain([suggestion()], block = True)
        suggesting = asyncio.create_task(self.service.suggest(self.graph.id, SuggestRequest(expected_revision = 1), chain = chain))
        await asyncio.wait_for(chain.started.wait(), timeout = 3)
        edited = self.service.edit(self.graph.id, self.edit_request(op = "update_node", id = "script_main", label = "User-reviewed main"))
        chain.release.set()
        with self.assertRaises(RevisionConflict):
            await asyncio.wait_for(suggesting, timeout = 5)
        self.assertEqual(self.store.load(self.graph.id), edited)

    async def test_offline_generation_does_not_initialize_a_model(self):
        with patch("backend.graph_enrichment.create_provider", side_effect = AssertionError("No model should be initialized")):
            manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1))
        self.assertEqual(manifest["summary_status_counts"], {"deterministic": 1})

    async def test_public_output_contains_only_one_project_named_html_file(self):
        manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1))
        output = self.output.resolve() / "output"
        files = sorted(path.name for path in output.iterdir())

        filename = flowchart_filename(self.graph.title)
        self.assertEqual(files, [filename])
        self.assertEqual(Path(manifest["output_directory"]), output)
        self.assertEqual(Path(manifest["output_file"]), output / filename)
        self.assertIn("Project overview", (output / filename).read_text(encoding = "utf-8"))
        self.assertFalse((self.output / "outputs").exists())

    async def test_failed_analysis_requires_explicit_incomplete_acknowledgment(self):
        failed = self.graph.model_copy(deep = True)
        failed.id = "failed_analysis"
        failed.sources[0].status = "failed"
        failed.issues = [GraphIssue(id = "parse_failure", severity = "error", code = "parse_failed", message = "Could not analyze the source.")]
        self.store.create(failed, {"main.py": SOURCE}, self.output)
        with self.assertRaises(ReviewRequired):
            await self.service.generate(failed.id, GenerateRequest(expected_revision = 1))
        manifest = await self.service.generate(failed.id, GenerateRequest(expected_revision = 1, acknowledge_incomplete = True))
        self.assertTrue(manifest["has_analysis_errors"])
        self.assertEqual(self.store.load(failed.id), failed)

    async def test_malformed_model_output_falls_back_without_changing_connections(self):
        chain = FakeSummaryChain({"high_level": "Attempted topology output", "detailed": "Description", "edges": []})
        manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1, use_llm = True), summary_chain = chain)
        self.assertEqual(manifest["summary_status_counts"], {"fallback": 1})
        self.assertEqual(self.store.load(self.graph.id), self.graph)
        self.assertEqual(len(chain.payloads), 2)

    async def test_database_failure_after_render_does_not_leave_a_published_generation(self):
        with patch.object(self.store, "record_generation", side_effect = OSError("Simulated database failure")), self.assertRaises(OSError):
            await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1))
        self.assertIsNone(self.store.latest_generation(self.graph.id))
        self.assertEqual(list((self.store.artifact_root(self.graph.id) / "generations").iterdir()), [])
        self.assertEqual(self.store.load(self.graph.id), self.graph)

    async def test_cancellation_during_file_write_cleans_up_after_the_worker_finishes(self):
        write_text = Path.write_text
        for cancellations in (1, 3):
            with self.subTest(cancellation_requests = cancellations):
                started, release, finished = threading.Event(), threading.Event(), threading.Event()

                def pause_after_first_file(path, content, *args, **kwargs):
                    result = write_text(path, content, *args, **kwargs)
                    if path.name == "workflow_flowchart.html":
                        # The worker has written a real artifact, but has not
                        # published its directory or registered a generation.
                        started.set()
                        if not release.wait(timeout = 5):
                            raise TimeoutError("Test did not release the artifact writer")
                    return result

                def observed_write(destination, files):
                    try:
                        _write_directory(destination, files)
                    finally:
                        finished.set()

                with patch("backend.workflow_service._write_directory", observed_write), patch.object(Path, "write_text", pause_after_first_file):
                    generating = asyncio.create_task(self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1)))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 3), "Generation did not reach the artifact writer")
                        for _ in range(cancellations):
                            generating.cancel()
                            await asyncio.sleep(0)
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(generating, timeout = 5)
                    finally:
                        release.set()
                        # Inspect only after the background write has actually
                        # finished; checking immediately misses orphan races.
                        self.assertTrue(await asyncio.to_thread(finished.wait, 5))
                        if not generating.done():
                            generating.cancel()
                            try:
                                await asyncio.wait_for(generating, timeout = 5)
                            except asyncio.CancelledError:
                                pass
                self.assertIsNone(self.store.latest_generation(self.graph.id))
                self.assertEqual(list((self.store.artifact_root(self.graph.id) / "generations").iterdir()), [])
                self.assertEqual(self.store.load(self.graph.id), self.graph)

    async def test_generated_artifact_tampering_is_detected(self):
        manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1))
        path = self.service.artifact_path(self.graph.id, manifest["generation_id"], "workflow_graph.json")
        path.write_text("{}", encoding = "utf-8")
        with self.assertRaises(ValueError):
            self.service.artifact_path(self.graph.id, manifest["generation_id"], "workflow_graph.json")

    async def test_generated_manifest_tampering_is_detected(self):
        manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1))
        path = self.service.artifact_path(self.graph.id, manifest["generation_id"], "generation_manifest.json")
        path.write_text('{"revision": 999}', encoding = "utf-8")
        with self.assertRaises(ValueError):
            self.service.artifact_path(self.graph.id, manifest["generation_id"], "generation_manifest.json")

    async def test_old_generation_remains_accessible_but_not_current_after_an_edit(self):
        manifest = await self.service.generate(self.graph.id, GenerateRequest(expected_revision = 1))
        edited = self.service.edit(self.graph.id, self.edit_request(op = "remove_edge", id = "read_edge"))
        self.assertIsNone(self.service.describe(edited)["outputs"]["flowchart"])
        self.assertIsNotNone(self.service.describe(self.store.load(self.graph.id, 1))["outputs"]["flowchart"])
        old = self.service.artifact_path(self.graph.id, manifest["generation_id"], "workflow_graph.json")
        self.assertEqual(GraphDocument.model_validate_json(old.read_text()), self.graph)


if __name__ == "__main__":
    unittest.main()
