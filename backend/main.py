"""Provide a command-line entry point for the reviewed workflow.

- ``analyze`` performs offline extraction and stops at a saved, editable draft.
- ``generate`` reads one explicit reviewed revision and creates its final report.
- ``serve`` starts the same durable local application used by the browser launcher.
- JSON responses are written safely on legacy Windows consoles as well as macOS.
- Every command uses the same service and validation layer as the HTTP API, so the
  command line cannot bypass revision checks or graph integrity rules.

Run ``python -m backend.main --help`` from the project directory for usage details.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.drawio import export_drawio
from backend.graph_edits import EditRequest
from backend.graph_rendering import render_graph_svg
from backend.workflow_service import (AnalysisRequest, GenerateRequest, ImportRequest, SuggestRequest,
                                      WorkflowService, default_service, review_report)
from backend.workflow_store import WorkflowStore, json_text


def write_output(text: str) -> None:
    """
    Emit command output as UTF-8, including when Windows redirects it to a file.
    - Graph JSON, SVG and draw.io content must retain Unicode names exactly.
    - Bypassing a legacy text encoding avoids corrupting those formats with
      replacement characters or non-JSON escape sequences for emoji.
    - In-memory test streams without a binary buffer still receive normal text.
    """
    output = text + "\n"
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.flush()
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)


def parser() -> argparse.ArgumentParser:
    """Build the three explicit commands and their validated user-facing options."""

    root = argparse.ArgumentParser(description = "Analyze scripts, review an editable draft, then generate an interactive flowchart.")
    root.add_argument("--store", type = Path, help = "Private draft database directory (default: backend/.workflow_store).")
    commands = root.add_subparsers(dest = "command", required = True)
    analyze = commands.add_parser("analyze", help = "Create a draft without source execution or LLM calls.")
    analyze.add_argument("script_folder")
    analyze.add_argument("da_document_folder")
    analyze.add_argument("--title", help = "Optional project name; defaults to the selected source folder's name.")
    analyze.add_argument("--working-directory", help = "Explicit runtime CWD for resolving relative file paths.")
    analyze.add_argument("--sql-dialect")
    analyze.add_argument("--database-namespace", help = "Explicit shared database/connection identity.")
    listing = commands.add_parser("list", help = "List saved drafts.")
    listing.add_argument("--output-folder")
    show = commands.add_parser("show", help = "Print the canonical graph JSON.")
    show.add_argument("draft_id")
    show.add_argument("--revision", type = int)
    export = commands.add_parser("export", help = "Export a graph for visual editing or inspection.")
    export.add_argument("draft_id")
    export.add_argument("--revision", type = int)
    export.add_argument("--format", choices = ["drawio", "json", "svg"], default = "drawio")
    export.add_argument("--output", type = Path)
    importing = commands.add_parser("import", help = "Import a corrected .drawio as a new revision.")
    importing.add_argument("draft_id")
    importing.add_argument("file", type = Path)
    importing.add_argument("--revision", type = int, required = True)
    edit = commands.add_parser("edit", help = "Apply a JSON EditRequest (expected_revision + operations).")
    edit.add_argument("draft_id")
    edit.add_argument("file", type = Path)
    history = commands.add_parser("history", help = "Show revision history and edit actions.")
    history.add_argument("draft_id")
    for name, description in [("generate", "Generate from a saved, reviewed revision."),
                              ("suggest", "Ask the model for review-only missing connections.")]:
        command = commands.add_parser(name, help = description)
        command.add_argument("draft_id")
        command.add_argument("--revision", type = int, required = True)
        command.add_argument("--model", choices = ["OpenAI", "Ollama"], help = "Defaults to the draft's saved provider.")
        command.add_argument("--max-concurrency", type = int)
        if name == "generate":
            command.add_argument("--llm", action = "store_true", help = "Send saved source text to the configured model for richer summaries.")
            command.add_argument("--language")
            command.add_argument("--allow-proposed", action = "store_true")
            command.add_argument("--acknowledge-incomplete", action = "store_true")
    return root


async def cli(arguments = None) -> int:
    """Run the selected command and return a conventional process exit status."""

    # - Windows consoles and redirected output may still use legacy encodings.
    # - Keep JSON/data escapes readable instead of failing after a successful
    #   saved analysis merely because its project name contains Unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors = "backslashreplace")
            except (OSError, ValueError):
                pass
    args = parser().parse_args(arguments)
    service = WorkflowService(WorkflowStore(args.store)) if args.store else default_service()
    log = lambda message: print(message, file = sys.stderr)
    try:
        if args.command == "analyze":
            graph = await service.analyze(AnalysisRequest(
                script_folder = args.script_folder, da_document_folder = args.da_document_folder,
                title = args.title, working_directory = args.working_directory,
                sql_dialect = args.sql_dialect, database_namespace = args.database_namespace,
            ), logger = log)
            result = {"draft_id": graph.id, "revision": graph.revision,
                      "artifacts": {name: str(path) for name, path in service.save_draft_exports(graph).items()},
                      "review": review_report(graph)}
        elif args.command == "list":
            result = service.store.list_drafts(args.output_folder)
        elif args.command == "show":
            result = service.store.load(args.draft_id, args.revision).model_dump()
        elif args.command == "history":
            result = service.store.history(args.draft_id)
        elif args.command == "export":
            graph = service.store.load(args.draft_id, args.revision)
            text = {"drawio": lambda: export_drawio(graph), "json": lambda: graph.model_dump_json(indent = 2),
                    "svg": lambda: render_graph_svg(graph)}[args.format]()
            if args.output:
                args.output.expanduser().parent.mkdir(parents = True, exist_ok = True)
                args.output.expanduser().write_text(text, encoding = "utf-8")
                result = {"export": str(args.output.expanduser().resolve()), "revision": graph.revision}
            else:
                write_output(text)
                return 0
        elif args.command == "import":
            graph = service.import_diagram(args.draft_id, ImportRequest(expected_revision = args.revision, xml = args.file.read_text(encoding = "utf-8")))
            result = {"draft_id": graph.id, "revision": graph.revision, "review": review_report(graph)}
        elif args.command == "edit":
            graph = service.edit(args.draft_id, EditRequest.model_validate_json(args.file.read_text(encoding = "utf-8")))
            result = {"draft_id": graph.id, "revision": graph.revision, "review": review_report(graph)}
        elif args.command == "suggest":
            model_options = {key: getattr(args, key) for key in ("model", "max_concurrency") if getattr(args, key) is not None}
            graph = await service.suggest(args.draft_id, SuggestRequest(expected_revision = args.revision, **model_options), logger = log)
            result = {"draft_id": graph.id, "revision": graph.revision, "review": review_report(graph)}
        else:
            model_options = {key: getattr(args, key) for key in ("model", "language", "max_concurrency") if getattr(args, key) is not None}
            result = await service.generate(args.draft_id, GenerateRequest(
                expected_revision = args.revision, use_llm = args.llm, **model_options, allow_proposed = args.allow_proposed,
                acknowledge_incomplete = args.acknowledge_incomplete,
            ), logger = log)
        write_output(json_text(result))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}), file = sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(cli()))
