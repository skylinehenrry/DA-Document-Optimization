"""Human-readable diagnostics for a saved, versioned dependency graph.

- Keep extraction evidence and graph topology untouched; this module only builds
  a presentation of information already recorded in the graph.
- Separate failed analysis from successful parsing with unresolved dependencies.
  The persisted ``partial`` status must never be described as a syntax failure.
- Group repeated limitations so the review screen can show a short overview and
  let the user expand individual source locations when they need the evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .graph_models import GraphDocument


# - These codes describe an input/analysis failure, rather than an uncertain
#   relationship. A failed embedded SQL expression can still belong to a Python
#   source that parsed successfully; source coverage uses SourceFile.status.
# - Keep the categories independent of severity: a warning can describe a
#   malformed batch line, while an error in an optional review step need not
#   mean that a source file failed to parse.
_FAILURE_CODES = frozenset({
    "source_analysis_failed", "source_encoding_loss", "source_skipped",
    "discovery_error", "sql_parse_error", "sql_parser_unavailable",
    "alteryx_parse_error", "alteryx_tool_limit", "batch_parse_error",
})


# - Each entry provides a plain-language title, explanation and useful next step.
# - Individual occurrences retain the exact extractor message and evidence; the
#   overview does not replace, truncate or reinterpret those records.
_DESCRIPTIONS: dict[str, tuple[str, str, str]] = {
    "source_analysis_failed": (
        "Source analysis failed",
        "A source could not be analyzed. Its relationships may be absent.",
        "Open the recorded source location, check the reported error, then analyze again.",
    ),
    "source_encoding_loss": (
        "Source text could not be decoded",
        "The source encoding could not be read reliably, so no dependencies were guessed from damaged text.",
        "Save the source using its declared encoding or UTF-8, then analyze again.",
    ),
    "source_skipped": (
        "Source files were skipped",
        "A source could not be safely read, or exceeded an analysis limit.",
        "Check its recorded reason, permissions, size and whether it is inside the selected project.",
    ),
    "discovery_error": (
        "A directory could not be inspected",
        "The analyzer could not list part of the project; additional source files may be missing.",
        "Check directory permissions and availability, then analyze again.",
    ),
    "directory_skipped": (
        "Generated and dependency folders excluded",
        "Cache folders, installed libraries and generated outputs were intentionally excluded.",
        "No action is needed unless one of these folders contains source you intended to analyze.",
    ),
    "directory_symlink_skipped": (
        "Linked folders excluded",
        "Directory symlinks were not followed, so analysis stays within the selected project.",
        "Analyze the linked project separately if it belongs in your review.",
    ),
    "loop_binding_unresolved": (
        "Loop-dependent values need review",
        "A dependency binding changes inside a loop and may not have one known value after it exits.",
        "Check the loop and its downstream calls or file paths; add any missing relationships to the draft.",
    ),
    "rebound_import": (
        "Imported names are reassigned",
        "An imported name can refer to a different value in this scope, so its call target is not assumed.",
        "Check the recorded assignments and confirm which module is called at runtime.",
    ),
    "rebound_callable": (
        "Callable attributes are reassigned",
        "A callable attribute is replaced in the source; the original imported target may no longer apply.",
        "Review the assignment and reconnect the draft only when the actual target is known.",
    ),
    "imported_callable_unresolved": (
        "Imported call targets need confirmation",
        "A call through a local import may use an inherited method, decorator or re-export. Its target is proposed for review.",
        "Check the callable definition or inherited implementation and confirm or remove the proposed relationship.",
    ),
    "dynamic_path_receiver": (
        "File objects or paths are determined at runtime",
        "A file operation was found, but the object or path it uses cannot be resolved statically.",
        "Check the supplied path and add the input or output relationship if it belongs in the map.",
    ),
    "dynamic_file_path": (
        "File paths are determined at runtime",
        "A file operation uses a value that the analyzer cannot safely reduce to a filename.",
        "Review the runtime path or configuration and add the corresponding input or output to the draft.",
    ),
    "relative_path_unresolved": (
        "Relative paths need a working directory",
        "The same relative filename may refer to different files when scripts run from different directories.",
        "Supply a working directory only if it applies to the workflow, or review these proposed file relationships manually.",
    ),
    "runtime_cwd_mutation": (
        "The working directory can change",
        "A source can change its working directory, making subsequent relative paths uncertain.",
        "Check directory changes and confirm the affected file and launch targets.",
    ),
    "unresolved_database_call": (
        "Database execution needs review",
        "A database execution call was found, but the connection and embedded SQL dependencies are not resolved.",
        "Review the SQL and actual database connection; add missing table relationships to the draft.",
    ),
    "database_namespace_unresolved": (
        "Database connections need context",
        "Tables with the same name may belong to different databases. Their identities remain separate by source.",
        "Supply a shared database namespace only when the scripts use the same database, or adjust the draft manually.",
    ),
    "dynamic_sql": (
        "SQL text is constructed at runtime",
        "The SQL expression is not a supported literal, so table names were not guessed.",
        "Review how the SQL is assembled and add the confirmed table reads and writes.",
    ),
    "dynamic_sql_table": (
        "Table identities need review",
        "A table name, schema or table-valued expression could not be resolved safely.",
        "Check the SQL and runtime parameters before connecting the relevant table.",
    ),
    "sql_parse_error": (
        "SQL could not be parsed",
        "SQL text could not be parsed with the selected dialect. Other parts of its source may still be analyzed.",
        "Check the SQL syntax and dialect, then analyze again or supply the missing relationships manually.",
    ),
    "sql_parser_unavailable": (
        "SQL parser is unavailable",
        "The SQL analysis dependency is not installed in the application's Python environment.",
        "Install the application's required packages and restart it before analyzing again.",
    ),
    "unsupported_sql_statement": (
        "Some SQL statements need manual review",
        "A statement is outside the extractor's supported read/write patterns, such as a stored procedure or vendor command.",
        "Review its actual table dependencies and add them to the editable draft.",
    ),
    "sql_session_context": (
        "SQL session context changes",
        "A database or session setting can change how later table names are interpreted.",
        "Review the active database/schema at each affected statement before merging table nodes.",
    ),
    "lambda_calls_unresolved": (
        "Deferred callable needs review",
        "This saved analysis did not resolve calls inside a lambda expression.",
        "Reanalyze with the current analyzer, which visits lambda bodies with their own argument scope.",
    ),
    "dynamic_python_construct": (
        "Python resolves dependencies dynamically",
        "Reflection, dynamic imports or runtime execution can introduce relationships that static analysis cannot establish.",
        "Inspect the recorded expression and add only relationships you can confirm from the program's behavior.",
    ),
    "dynamic_callable": (
        "A callable is selected dynamically",
        "The invoked callable is selected from an expression instead of one statically known import.",
        "Review the possible targets and add the applicable relationships to the draft.",
    ),
    "dynamic_call_kwargs": (
        "Expanded arguments can change a dependency",
        "Arguments supplied through **kwargs can change file access or launch behavior.",
        "Check the supplied arguments before deciding the file or invocation relationship.",
    ),
    "dynamic_file_mode": (
        "File access direction is uncertain",
        "The file mode is dynamic or unsupported, so the analyzer cannot safely choose read versus write.",
        "Review the access mode and connect the file in the correct direction.",
    ),
    "unresolved_writer_receiver": (
        "A possible data writer needs review",
        "The method name resembles a data writer, but its receiver is not known to be a supported data frame.",
        "Check the object's type and add a file relationship only if the call actually writes data.",
    ),
    "ambiguous_local_import": (
        "Multiple sources match an import",
        "More than one local source can satisfy an import under the supported search paths.",
        "Check the runtime import path and connect the intended source manually.",
    ),
    "unresolved_import_root": (
        "An import search path is unknown",
        "A local source could match an import, but its runtime package root is not known.",
        "Check package configuration and the runtime search path before adding the import relationship.",
    ),
    "relative_import_unresolved": (
        "A relative import could not be matched",
        "The import could not be matched safely within the analyzed package hierarchy.",
        "Check that the selected project includes the package and review the intended import target.",
    ),
    "wildcard_import": (
        "Wildcard imports obscure call targets",
        "Importing every exported name prevents the analyzer from identifying call bindings safely.",
        "Review the exported names and add any missing cross-file calls.",
    ),
    "launch_target_unresolved": (
        "A launched source could not be matched",
        "A command target is known only as a path, not as one analyzed source file.",
        "Check the working directory and target path, then confirm or reconnect the proposed launch.",
    ),
    "batch_control_flow_unresolved": (
        "Batch branches and loops need review",
        "Batch jumps, conditional commands or continuations are not converted into execution order.",
        "Check which programs can run in each branch and add their confirmed relationships.",
    ),
    "batch_environment_unresolved": (
        "Batch environment settings are not evaluated",
        "Environment assignments were recorded but are never executed during analysis.",
        "Review variable-based commands if they affect the workflow's files or launches.",
    ),
    "batch_subroutine_unresolved": (
        "Batch subroutines are outside the map's scope",
        "Internal batch subroutine flow is not expanded in this project dependency map.",
        "Review the subroutine for additional cross-file dependencies if needed.",
    ),
    "alteryx_dynamic_tool": (
        "An Alteryx tool can add runtime dependencies",
        "Embedded code, command tools and dynamic input/output tools can add dependencies beyond explicit XML connections.",
        "Review that tool's configuration and add the external scripts, files or tables it uses.",
    ),
    "alteryx_database_connection": (
        "An Alteryx database connection needs review",
        "Database driver and connection context are not inferred from Alteryx configuration.",
        "Confirm the actual database and tables; do not paste connection credentials into the graph.",
    ),
    "alteryx_parse_error": (
        "An Alteryx workflow could not be read",
        "The workflow was not valid, safely parseable XML; its tool connections were not guessed.",
        "Check the workflow file in Alteryx and export a valid XML workflow before analyzing again.",
    ),
    "alteryx_tool_limit": (
        "An Alteryx workflow exceeds the analysis limit",
        "The workflow contains more tools than the safe analysis limit permits.",
        "Analyze smaller workflows or review the oversized workflow separately.",
    ),
}


def graph_diagnostics(graph: GraphDocument) -> dict[str, Any]:
    """Project a graph's diagnostics for the app, JSON report and final HTML.

    - Count source failures from persisted source statuses, never from the mere
      presence of warnings. ``partial`` means extraction has review limitations.
    - Include skipped source nodes in discovery coverage without inventing source
      snapshots, hashes or successful parsing records for files never read.
    - Keep every issue occurrence with its original evidence. Grouping only
      changes presentation and does not promote proposed edges to confirmed.
    - Return plain JSON-compatible values and deterministic ordering, allowing
      callers to serialize the same report for browser and offline use.
    """
    nodes = {node.id: node for node in graph.nodes}
    grouped: dict[str, list[Any]] = defaultdict(list)
    source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for issue in graph.issues:
        paths = {item.source_path for item in issue.evidence}
        for node_id in issue.node_ids:
            node = nodes.get(node_id)
            if node is not None:
                path = node.source_path or node.details.get("relative_path")
                if isinstance(path, str):
                    paths.add(path)
        for path in paths:
            source_counts[path][issue.severity] += 1
        first_evidence = issue.evidence[0] if issue.evidence else None
        grouped[issue.code].append({
            "id": issue.id,
            "message": issue.message,
            "severity": issue.severity,
            "source_path": first_evidence.source_path if first_evidence else next(iter(sorted(paths)), None),
            "line_start": first_evidence.line_start if first_evidence else None,
            "line_end": first_evidence.line_end if first_evidence else None,
            "evidence": [item.model_dump(mode = "json") for item in issue.evidence],
            "node_ids": list(issue.node_ids),
            "edge_ids": list(issue.edge_ids),
        })

    severity_rank = {"error": 0, "warning": 1, "info": 2}
    groups = []
    for code, occurrences in grouped.items():
        severity = min((item["severity"] for item in occurrences), key = severity_rank.__getitem__)
        title, description, action = _DESCRIPTIONS.get(code, (
            code.replace("_", " ").capitalize(),
            "The analyzer recorded a limitation that may affect dependency coverage.",
            "Review the recorded source locations and confirm any affected relationships in the draft.",
        ))
        category = "parse_failure" if code in _FAILURE_CODES else "info" if severity == "info" else "review"
        occurrences.sort(key = lambda item: (item["source_path"] or "", item["line_start"] or 0, item["id"]))
        groups.append({
            "code": code, "title": title, "category": category, "severity": severity,
            "count": len(occurrences), "description": description,
            "suggested_action": action, "occurrences": occurrences,
        })
    groups.sort(key = lambda item: (severity_rank[item["severity"]], -item["count"], item["code"]))

    source_statuses = Counter(source.status for source in graph.sources)
    known_paths = {source.path for source in graph.sources}
    skipped = {
        str(node.details.get("relative_path") or node.label): node
        for node in graph.nodes
        if node.kind == "script" and node.details.get("analysis_status") == "skipped"
        and (node.details.get("relative_path") or node.label) not in known_paths
    }
    edge_statuses = Counter(edge.status for edge in graph.edges)
    coverage = {
        "total_sources": len(graph.sources) + len(skipped),
        "analyzed_sources": source_statuses["parsed"] + source_statuses["partial"],
        "complete_sources": source_statuses["parsed"],
        "review_sources": source_statuses["partial"],
        "failed_sources": source_statuses["failed"],
        "skipped_sources": len(skipped),
        "confirmed_relationships": edge_statuses["confirmed"],
        "proposed_relationships": edge_statuses["proposed"],
    }
    sources = [{
        "path": source.path, "script_type": source.script_type, "status": source.status,
        "description": {
            "parsed": "Analyzed with no flagged limitations",
            "partial": "Analyzed; some dependencies need review",
            "failed": "Source analysis failed",
        }[source.status],
        "counts": {key: source_counts[source.path][key] for key in ("error", "warning", "info")},
    } for source in graph.sources]
    sources.extend({
        "path": path, "script_type": node.script_type, "status": "skipped",
        "description": "Source was not read", "reason": node.details.get("reason"),
        "counts": {key: source_counts[path][key] for key in ("error", "warning", "info")},
    } for path, node in skipped.items())
    sources.sort(key = lambda item: item["path"])

    issue_statuses = Counter(issue.severity for issue in graph.issues)
    counts = {"total": len(graph.issues), **{key: issue_statuses[key] for key in ("error", "warning", "info")}}
    statements = [f"{coverage['analyzed_sources']} of {coverage['total_sources']} source files analyzed."]
    if coverage["failed_sources"] or coverage["skipped_sources"]:
        statements.append(f"{coverage['failed_sources']} failed analysis; {coverage['skipped_sources']} were skipped.")
    elif coverage["total_sources"]:
        statements.append("No source files failed analysis.")
    if coverage["review_sources"]:
        statements.append(f"{coverage['review_sources']} analyzed files contain dependencies or constructs that need review.")
    if coverage["proposed_relationships"]:
        statements.append(f"{coverage['proposed_relationships']} proposed relationships need confirmation.")
    return {
        "coverage": coverage, "summary": " ".join(statements), "groups": groups,
        "counts": counts, "sources": sources,
        "has_review_items": bool(counts["error"] or counts["warning"] or coverage["proposed_relationships"]
                                 or coverage["review_sources"] or coverage["failed_sources"] or coverage["skipped_sources"]),
        "scope_note": "This is a static project dependency map. Successful analysis does not prove complete runtime control flow or that every branch executes.",
    }
