"""Review files must preserve server provenance and all intentional topology edits."""

from __future__ import annotations

import base64
import copy
import unittest
from unittest.mock import patch
from urllib.parse import quote
import xml.etree.ElementTree as ET
import zlib

from backend.drawio import export_drawio, import_drawio
from backend.graph_models import Evidence, GraphDocument, GraphEdge, GraphNode, Position, SourceFile, topology_signature


def example_graph() -> GraphDocument:
    return GraphDocument(
        id="graph_review", revision=7, title="Data workflow", project_root="/project",
        source_digest="c" * 64,
        sources=[SourceFile(path="first.py", sha256="a" * 64, script_type="python", size_bytes=20)],
        nodes=[
            GraphNode(id="n_first", label="First <step> & checks", kind="script", source_path="first.py", script_type="python", position=Position(x=-12.5, y=15), details={"protected": "server summary"}),
            GraphNode(id="n_second", label="Second", kind="process", position=Position(x=460, y=15)),
            GraphNode(id="n_third", label="Third", kind="table", resource_key="db:third", position=Position(x=460, y=190)),
        ],
        edges=[
            GraphEdge(id="e_reads", source="n_first", target="n_second", kind="reads", condition="if ready", evidence=[Evidence(source_path="first.py", line_start=2, line_end=2, excerpt="read_input()", extractor="python_ast")]),
            GraphEdge(id="e_calls", source="n_first", target="n_second", kind="calls", label="invoke", origin="llm", status="proposed", evidence=[Evidence(source_path="first.py", line_start=3, excerpt="run()", extractor="llm_candidate")]),
            GraphEdge(id="e_return", source="n_second", target="n_first", kind="control_flow"),
            GraphEdge(id="e_loop", source="n_second", target="n_second", kind="control_flow", label="retry"),
        ],
        analysis_options={"protected": True},
    )


def parse_export(graph: GraphDocument) -> ET.Element:
    return ET.fromstring(export_drawio(graph))


def serialize(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def graph_root(root: ET.Element) -> ET.Element:
    result = root.find("./diagram/mxGraphModel/root")
    assert result is not None
    return result


def find_entry(root: ET.Element, cell_id: str) -> ET.Element:
    result = graph_root(root).find(f"./*[@id='{cell_id}']")
    assert result is not None
    return result


def mxcell(entry: ET.Element) -> ET.Element:
    if entry.tag == "mxCell":
        return entry
    result = entry.find("mxCell")
    assert result is not None
    return result


def add_node(root: ET.Element, node_id: str, label: str = "New step") -> ET.Element:
    cell = ET.SubElement(graph_root(root), "mxCell", {"id": node_id, "value": label, "vertex": "1", "parent": "1", "style": "rounded=1;whiteSpace=wrap;html=1;"})
    ET.SubElement(cell, "mxGeometry", {"x": "0", "y": "40.25", "width": "300", "height": "82", "as": "geometry"})
    return cell


def add_edge(root: ET.Element, edge_id: str, source: str, target: str, **properties: str) -> ET.Element:
    cell = ET.SubElement(graph_root(root), "mxCell", {"id": edge_id, "edge": "1", "parent": "1", "source": source, "target": target, "style": "edgeStyle=orthogonalEdgeStyle;html=1;", **properties})
    ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
    return cell


def compress_export(root: ET.Element, *, raw_xml: str | None = None) -> str:
    page = root.find("diagram")
    assert page is not None
    model = page.find("mxGraphModel")
    assert model is not None
    xml = raw_xml if raw_xml is not None else serialize(model)
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    encoded = quote(xml, safe="~()*!.'-").encode("utf-8")
    compressed = compressor.compress(encoded) + compressor.flush()
    page.remove(model)
    page.text = base64.b64encode(compressed).decode("ascii")
    root.attrib.pop("compressed", None)
    return serialize(root)


class DrawioRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = example_graph()

    def test_roundtrip_preserves_parallel_edges_cycles_proposals_and_evidence(self) -> None:
        xml = export_drawio(self.graph)
        restored = import_drawio(self.graph, xml)
        self.assertEqual(restored.model_dump(), self.graph.model_dump())
        self.assertEqual(topology_signature(restored), topology_signature(self.graph))
        root = ET.fromstring(xml)
        self.assertIn("html=0", find_entry(root, "n_first").get("style", ""))
        self.assertIn("dashed=1", mxcell(find_entry(root, "e_calls")).get("style", ""))
        self.assertNotIn("read_input", xml)
        self.assertNotIn("server summary", xml)

    def test_filename_only_cards_keep_distinct_source_identities_on_roundtrip(self) -> None:
        self.graph.nodes[0].label = "very/long/source/path/first.py"
        root = parse_export(self.graph)
        self.assertEqual(find_entry(root, "n_first").get("value"), "first.py")
        restored = import_drawio(self.graph, serialize(root))
        self.assertEqual(restored.model_dump(), self.graph.model_dump())
        self.assertEqual(restored.nodes[0].source_path, "first.py")
        self.assertEqual(restored.nodes[0].id, "n_first")

    def test_reconnect_delete_and_add_are_authoritative_without_fabricated_evidence(self) -> None:
        before = self.graph.model_dump()
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_reads")).set("target", "n_third")
        graph_root(root).remove(find_entry(root, "e_return"))
        add_edge(root, "user_connector", "n_third", "n_first", value="reads")
        reviewed = import_drawio(self.graph, serialize(root))
        edges = {edge.id: edge for edge in reviewed.edges}
        self.assertNotIn("e_return", edges)
        self.assertEqual(edges["e_reads"].target, "n_third")
        self.assertEqual(edges["e_reads"].evidence, [])
        self.assertIsNone(edges["e_reads"].condition)
        self.assertEqual((edges["e_reads"].origin, edges["e_reads"].status), ("user", "confirmed"))
        self.assertIn("n_second", edges["e_reads"].review_note)
        self.assertIn("prior revision", edges["e_reads"].review_note)
        self.assertEqual(edges["e_calls"], self.graph.edges[1])
        self.assertEqual(edges["user_connector"].kind, "unknown")
        self.assertEqual(edges["user_connector"].evidence, [])
        self.assertEqual(edges["user_connector"].origin, "user")
        self.assertEqual(self.graph.model_dump(), before, "Import must not mutate server revision history")
        self.assertEqual(reviewed.revision, self.graph.revision, "The persistence layer owns revision increments")

    def test_deleted_nodes_and_incident_edges_do_not_reappear(self) -> None:
        root = parse_export(self.graph)
        graph_root(root).remove(find_entry(root, "n_first"))
        for edge_id in ("e_reads", "e_calls", "e_return"):
            graph_root(root).remove(find_entry(root, edge_id))
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertNotIn("n_first", {node.id for node in reviewed.nodes})
        self.assertEqual([edge.id for edge in reviewed.edges], ["e_loop"])
        self.assertEqual(reviewed.sources, self.graph.sources)
        self.assertEqual(import_drawio(reviewed, export_drawio(reviewed)), reviewed)

    def test_empty_review_is_an_intentional_empty_graph(self) -> None:
        root = parse_export(self.graph)
        cells = graph_root(root)
        for cell in list(cells):
            if cell.get("id") not in {"0", "1"}:
                cells.remove(cell)
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertEqual(reviewed.nodes, [])
        self.assertEqual(reviewed.edges, [])
        self.assertEqual(reviewed.sources, self.graph.sources)

    def test_new_nodes_and_copied_source_nodes_have_no_source_metadata(self) -> None:
        root = parse_export(self.graph)
        copied = copy.deepcopy(find_entry(root, "n_first"))
        copied.set("id", "123-new-copy")
        copied.set("source_path", "first.py")
        copied.set("script_type", "python")
        copied.set("details", '{"protected": "forged"}')
        graph_root(root).append(copied)
        add_edge(root, "45", "123-new-copy", "n_second", daKind="calls", value="Run")
        reviewed = import_drawio(self.graph, serialize(root))
        new_node = reviewed.nodes[-1]
        new_edge = reviewed.edges[-1]
        self.assertTrue(new_node.id.startswith("review_node_"))
        self.assertTrue(new_edge.id.startswith("review_edge_"))
        self.assertEqual(new_node.kind, "process")
        self.assertIsNone(new_node.source_path)
        self.assertIsNone(new_node.script_type)
        self.assertIsNone(new_node.resource_key)
        self.assertEqual(new_node.details, {})
        self.assertEqual(new_edge.source, new_node.id)
        self.assertEqual(new_edge.kind, "calls")
        self.assertEqual(new_edge.evidence, [])
        self.assertEqual(import_drawio(reviewed, export_drawio(reviewed)), reviewed)

    def test_position_and_label_changes_cannot_overwrite_server_metadata(self) -> None:
        root = parse_export(self.graph)
        node = find_entry(root, "n_first")
        node.set("value", "Renamed script")
        node.set("source_path", "../../malicious.py")
        node.set("script_type", "sql")
        node.set("kind", "unknown")
        node.set("details", "forged")
        node.find("mxGeometry").set("x", "-70.125")
        node.find("mxGeometry").set("y", "0")
        edge = find_entry(root, "e_reads")
        edge.set("evidence", "forged")
        edge.set("origin", "llm")
        edge.set("status", "proposed")
        edge.set("condition", "forged")
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertEqual(reviewed.nodes[0].position, Position(x=-70.125, y=0))
        self.assertEqual(reviewed.nodes[0].label, "Renamed script")
        self.assertEqual(reviewed.nodes[0].source_path, "first.py")
        self.assertEqual(reviewed.nodes[0].details, self.graph.nodes[0].details)
        self.assertEqual(reviewed.nodes[0].kind, "script")
        self.assertEqual(reviewed.edges[0], self.graph.edges[0])
        self.assertEqual(reviewed.analysis_options, self.graph.analysis_options)

    def test_connector_labels_do_not_infer_or_retype_semantics(self) -> None:
        root = parse_export(self.graph)
        find_entry(root, "e_reads").set("label", "writes")
        reviewed = import_drawio(self.graph, serialize(root))
        edge = reviewed.edges[0]
        self.assertEqual(edge.kind, "reads")
        self.assertEqual(edge.label, "writes")
        self.assertEqual(edge.evidence, self.graph.edges[0].evidence)
        self.assertEqual(edge.condition, self.graph.edges[0].condition)
        self.assertEqual(edge.origin, "user")
        self.assertEqual(edge.status, "confirmed")

    def test_cosmetic_label_change_does_not_confirm_a_proposed_edge(self) -> None:
        root = parse_export(self.graph)
        find_entry(root, "e_calls").set("label", "Clarified candidate label")
        reviewed = import_drawio(self.graph, serialize(root))
        edge = next(edge for edge in reviewed.edges if edge.id == "e_calls")
        self.assertEqual(edge.label, "Clarified candidate label")
        self.assertEqual(edge.status, "proposed")
        self.assertEqual(edge.evidence, self.graph.edges[1].evidence)

    def test_explicit_status_confirmation_preserves_original_evidence(self) -> None:
        root = parse_export(self.graph)
        find_entry(root, "e_calls").set("daStatus", "confirmed")
        reviewed = import_drawio(self.graph, serialize(root))
        edge = next(edge for edge in reviewed.edges if edge.id == "e_calls")
        self.assertEqual(edge.status, "confirmed")
        self.assertEqual(edge.origin, "user")
        self.assertEqual(edge.evidence, self.graph.edges[1].evidence)
        self.assertEqual(edge.label, "invoke")
        self.assertIn("explicitly changed", edge.review_note)

    def test_substantive_correction_confirms_a_previously_proposed_edge(self) -> None:
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_calls")).set("target", "n_third")
        reviewed = import_drawio(self.graph, serialize(root))
        edge = next(edge for edge in reviewed.edges if edge.id == "e_calls")
        self.assertEqual(edge.target, "n_third")
        self.assertEqual(edge.status, "confirmed")
        self.assertEqual(edge.evidence, [])

    def test_explicit_status_downgrade_is_retained(self) -> None:
        root = parse_export(self.graph)
        find_entry(root, "e_reads").set("daStatus", "proposed")
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertEqual(reviewed.edges[0].status, "proposed")
        self.assertEqual(reviewed.edges[0].evidence, self.graph.edges[0].evidence)

    def test_explicit_kind_edit_clears_old_evidence_and_condition(self) -> None:
        root = parse_export(self.graph)
        find_entry(root, "e_reads").set("daKind", "writes")
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertEqual(reviewed.edges[0].kind, "writes")
        self.assertEqual(reviewed.edges[0].evidence, [])
        self.assertIsNone(reviewed.edges[0].condition)
        self.assertEqual(reviewed.edges[0].origin, "user")

    def test_userobject_wrappers_and_normal_editor_save_keep_identity(self) -> None:
        root = parse_export(self.graph)
        model = root.find("./diagram/mxGraphModel")
        model.attrib.pop("daGraphId")
        model.attrib.pop("daRevision")
        old = find_entry(root, "n_first")
        wrapper = ET.Element("UserObject", {"id": old.attrib.pop("id"), "label": old.attrib.pop("value"), "source_path": "not-trusted.py"})
        wrapper.append(old)
        graph_root(root).remove(old)
        graph_root(root).insert(2, wrapper)
        # Saving may also choose UserObject rather than object for cell values.
        find_entry(root, "e_reads").tag = "UserObject"
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertEqual(reviewed, self.graph)

    def test_formatted_new_labels_are_converted_to_plain_text(self) -> None:
        root = parse_export(self.graph)
        add_node(root, "user_node", "<div><b>Check &amp; validate</b></div><div>Next step</div>")
        add_edge(root, "user_edge", "n_first", "user_node", value="<i>yes</i>")
        reviewed = import_drawio(self.graph, serialize(root))
        self.assertEqual(reviewed.nodes[-1].label, "Check & validate\nNext step")
        self.assertEqual(reviewed.edges[-1].label, "yes")

    def test_compressed_diagrams_net_payload_roundtrips(self) -> None:
        xml = compress_export(parse_export(self.graph))
        self.assertEqual(import_drawio(self.graph, xml), self.graph)

    def test_raw_mxgraphmodel_export_also_roundtrips(self) -> None:
        model = parse_export(self.graph).find("./diagram/mxGraphModel")
        self.assertEqual(import_drawio(self.graph, serialize(model)), self.graph)


class DrawioValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = example_graph()

    def assert_rejected(self, root: ET.Element, message: str) -> None:
        with self.assertRaisesRegex(ValueError, message):
            import_drawio(self.graph, serialize(root))

    def test_wrong_graph_and_stale_revision_are_rejected(self) -> None:
        for property_name, value, message in (("daGraphId", "another_graph", "different graph"), ("daRevision", "6", "stale"), ("daRevision", "7.0", "revision")):
            with self.subTest(property=property_name, value=value):
                root = parse_export(self.graph)
                root.find("./diagram/mxGraphModel").set(property_name, value)
                self.assert_rejected(root, message)

    def test_missing_and_conflicting_metadata_are_rejected(self) -> None:
        root = parse_export(self.graph)
        find_entry(root, "1").set("daRevision", "8")
        self.assert_rejected(root, "revision")
        root = parse_export(self.graph)
        for element in root.iter():
            element.attrib.pop("daGraphId", None)
            element.attrib.pop("daRevision", None)
        self.assert_rejected(root, "metadata is missing")
        root = parse_export(self.graph)
        find_entry(root, "1").attrib.pop("daRevision")
        self.assert_rejected(root, "incomplete")

    def test_duplicate_ids_and_conflicting_wrappers_are_rejected(self) -> None:
        root = parse_export(self.graph)
        graph_root(root).append(copy.deepcopy(find_entry(root, "n_first")))
        self.assert_rejected(root, "Duplicate")
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_reads")).set("id", "different_id")
        self.assert_rejected(root, "conflicting IDs")
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_reads")).set("daKind", "calls")
        self.assert_rejected(root, "conflicting daKind")

    def test_dangling_or_floating_connectors_are_rejected(self) -> None:
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_reads")).set("target", "absent")
        self.assert_rejected(root, "dangling endpoint")
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_reads")).attrib.pop("source")
        self.assert_rejected(root, "dangling endpoint")

    def test_multiple_pages_extra_layers_groups_and_attached_labels_are_rejected(self) -> None:
        root = parse_export(self.graph)
        root.append(copy.deepcopy(root.find("diagram")))
        self.assert_rejected(root, "one diagram page")
        root = parse_export(self.graph)
        ET.SubElement(graph_root(root), "mxCell", {"id": "other_layer", "parent": "0"})
        self.assert_rejected(root, "default layer")
        root = parse_export(self.graph)
        find_entry(root, "n_first").set("style", "group;")
        self.assert_rejected(root, "Ungroup")
        root = parse_export(self.graph)
        label = add_node(root, "attached_label", "Extra edge label")
        label.set("parent", "e_reads")
        self.assert_rejected(root, "attached edge labels")

    def test_malformed_geometry_numeric_values_are_rejected(self) -> None:
        for value in ("NaN", "Infinity", "-inf", "1e999", "1_000", "100px", "1000001", ""):
            with self.subTest(value=value):
                root = parse_export(self.graph)
                find_entry(root, "n_first").find("mxGeometry").set("x", value)
                self.assert_rejected(root, "numeric")
        root = parse_export(self.graph)
        geom = mxcell(find_entry(root, "e_reads")).find("mxGeometry")
        points = ET.SubElement(geom, "Array", {"as": "points"})
        ET.SubElement(points, "mxPoint", {"x": "NaN", "y": "10"})
        self.assert_rejected(root, "numeric")

    def test_external_resources_and_active_html_are_rejected(self) -> None:
        for style in ("html=0;image=https://evil.example/image.svg;", "html=0;fillColor=url(https://evil.example);", "shape=image;image=data:image/svg+xml,evil;"):
            with self.subTest(style=style):
                root = parse_export(self.graph)
                find_entry(root, "n_first").set("style", style)
                self.assert_rejected(root, "resources")
        root = parse_export(self.graph)
        find_entry(root, "e_reads").set("link", "https://evil.example/")
        self.assert_rejected(root, "external links")
        # These are real top-level settings serialized by Editor.getGraphXml,
        # not just image= styles on individual cells.
        for name, value in (("backgroundImage", '{"src":"https://evil.example/bg.svg"}'), ("extFonts", "RemoteFont^https://evil.example/font.css")):
            with self.subTest(attribute=name):
                root = parse_export(self.graph)
                root.find("./diagram/mxGraphModel").set(name, value)
                self.assert_rejected(root, "resources")
        for label in ('<img src="https://evil.example/">', '<script>alert(1)</script>', '<span onclick="alert(1)">x</span>'):
            with self.subTest(label=label):
                root = parse_export(self.graph)
                add_node(root, "new_label", label)
                self.assert_rejected(root, "HTML")

    def test_dtd_entities_processing_instructions_and_namespaces_are_rejected(self) -> None:
        for xml in (
            '<!DOCTYPE mxfile [<!ENTITY leak SYSTEM "file:///etc/passwd">]><mxfile>&leak;</mxfile>',
            '<!DOCTYPE mxfile SYSTEM "https://evil.example/dtd"><mxfile/>',
            '<?xml-stylesheet type="text/xsl" href="https://evil.example/xsl"?><mxfile/>',
            '<mxfile xmlns:x="http://www.w3.org/2001/XInclude"><x:include href="https://evil.example"/></mxfile>',
        ):
            with self.subTest(xml=xml):
                with self.assertRaisesRegex(ValueError, "DTD|instructions|Namespaced"):
                    import_drawio(self.graph, xml)
        xml = compress_export(parse_export(self.graph), raw_xml='<!DOCTYPE mxGraphModel [<!ENTITY x "bad">]><mxGraphModel/>')
        with self.assertRaisesRegex(ValueError, "DTD"):
            import_drawio(self.graph, xml)

    def test_oversized_and_compression_bomb_payloads_are_bounded(self) -> None:
        with patch("backend.drawio.MAX_XML_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "size limit"):
                import_drawio(self.graph, "<mxfile>" + " " * 101 + "</mxfile>")
        xml = compress_export(parse_export(self.graph), raw_xml="<mxGraphModel>" + " " * 200_000 + "</mxGraphModel>")
        with patch("backend.drawio.MAX_XML_BYTES", 4096):
            with self.assertRaisesRegex(ValueError, "expanded diagram"):
                import_drawio(self.graph, xml)

    def test_unknown_elements_kinds_and_visual_arrow_reversal_are_rejected(self) -> None:
        root = parse_export(self.graph)
        ET.SubElement(graph_root(root), "UnknownShape")
        self.assert_rejected(root, "Unsupported")
        root = parse_export(self.graph)
        find_entry(root, "e_reads").set("daKind", "executes_maybe")
        self.assert_rejected(root, "unsupported daKind")
        root = parse_export(self.graph)
        find_entry(root, "e_reads").set("daStatus", "verified_by_llm")
        self.assert_rejected(root, "unsupported daStatus")
        root = parse_export(self.graph)
        mxcell(find_entry(root, "e_reads")).set("style", "startArrow=classic;endArrow=none;html=0;")
        self.assert_rejected(root, "arrow at its target")


if __name__ == "__main__":
    unittest.main()
