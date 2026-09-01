"""Extract conservative project dependencies offline; this is not a full CFG.

- Never import or execute project code and never ask a model to guess topology.
- Treat ``confirmed`` as source-syntax evidence, not proof that a branch executes
  or that the containing program succeeds at runtime.
- Cover Python imports and literal IO/calls, SQL table IO, batch launches, and
  explicit Alteryx tool wiring with exact source evidence.
- Leave reflection, arbitrary data flow, shell expansion, stored procedures, and
  application-specific APIs visible as review limitations rather than guessed links.
- Scope relative runtime paths to their source unless a working directory or an
  explicit source/workflow-directory anchor supplies shared identity.
- Scope database identities similarly when no database namespace is supplied.
- Convert missing context into a diagnostic and, where useful, a proposed edge;
  never merge equally named resources by accident.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
import shlex
import sys
import tokenize
from typing import Any
from uuid import uuid4

try:
    from .graph_models import Evidence, GraphDocument, GraphEdge, GraphIssue, GraphNode, SourceFile, stable_id
except ImportError:  # Direct launch of backend/main.py remains supported.
    from graph_models import Evidence, GraphDocument, GraphEdge, GraphIssue, GraphNode, SourceFile, stable_id


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PROJECT_BYTES = 100 * 1024 * 1024
MAX_SOURCE_FILES = 5000
MAX_AST_NODES = 200000
MAX_XML_TOOLS = 10000
SCRIPT_TYPES = {".py": "python", ".sql": "sql", ".bat": "bat", ".yxmd": "alteryx", ".yxwz": "alteryx", ".yxmc": "alteryx"}
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "node_modules",
    "outputs", "output", "generated", "dist", "build", ".tox", ".nox", ".next",
})
_UNKNOWN = object()
_DEFAULT_CWD = object()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii = False, separators = (",", ":"), sort_keys = True)


def _is_absolute(value: str) -> bool:
    drive, tail = ntpath.splitdrive(value)
    return value.startswith("/") or bool(drive and tail.startswith(("/", "\\"))) or value.startswith("\\\\")


def _normalize_path(value: str) -> str:
    # Do not slugify, case-fold, Unicode-normalize, or drop filename punctuation.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return value
    if ntpath.splitdrive(value)[0] or value.startswith("\\\\"):
        return ntpath.normpath(value)
    return posixpath.normpath(value)


def _join_path(base: str, tail: str) -> str:
    if ntpath.splitdrive(base)[0]:
        # On Windows, a rooted path without a drive inherits the current drive.
        return ntpath.normpath(ntpath.join(base, tail))
    if _is_absolute(tail):
        return _normalize_path(tail)
    return _normalize_path(posixpath.join(base, tail))


def _project_relative_source(value: str, root: Path) -> str | None:
    """Match the root's path flavor without interpreting a foreign drive locally."""
    if not _is_absolute(value):
        return None
    root_text = root.as_posix()
    root_drive, _ = ntpath.splitdrive(root_text)
    value_drive, _ = ntpath.splitdrive(value)
    try:
        if root_drive:
            candidate = PureWindowsPath(value if value_drive else root_drive + value)
            return candidate.relative_to(PureWindowsPath(root_text)).as_posix()
        if value_drive:
            return None
        return PurePosixPath(value).relative_to(PurePosixPath(root_text)).as_posix()
    except ValueError:
        return None


@dataclass(frozen = True)
class _PathValue:
    text: str


@dataclass(frozen = True)
class _ImportReference:
    source_path: str
    symbol: str | None = None


def _path_text(value: Any) -> str | None:
    if isinstance(value, _PathValue):
        return value.text
    return value if isinstance(value, str) and value else None


class _Analysis:
    def __init__(self, root: Path, working_directory: str | None, sql_dialect: str | None,
                 database_namespace: str | None, title: str, logger: Any) -> None:
        self.root, self.title, self.logger = root, title, logger
        self.cwd = (_normalize_path(working_directory) if _is_absolute(working_directory)
                    else _join_path(root.as_posix(), working_directory)) if working_directory else None
        self.dialect = sql_dialect
        self.namespace = database_namespace
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.issues: dict[str, GraphIssue] = {}
        self.sources: dict[str, SourceFile] = {}
        self.snapshots: dict[str, str] = {}
        self.script_ids: dict[str, str] = {}
        self.callables: dict[str, set[str]] = {}
        self.module_candidates: dict[str, set[str]] = {}

    def defined_callables(self, rel: str) -> set[str]:
        """Names defined directly by a source, without following re-exports."""
        if rel in self.callables:
            return self.callables[rel]
        names: set[str] = set()
        try:
            tree = ast.parse(self.snapshots[rel], filename = rel)

            def collect(body: list[ast.stmt], prefix: str = "") -> None:
                bindings = _Bindings()
                for statement in body:
                    bindings.visit(statement)
                for statement in body:
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if bindings.writes[statement.name] == 1 and not statement.decorator_list:
                            names.add(prefix + statement.name)
                            if isinstance(statement, ast.ClassDef):
                                collect(statement.body, prefix + statement.name + ".")
            collect(tree.body)
        except (SyntaxError, ValueError, RecursionError, KeyError):
            pass
        self.callables[rel] = names
        return names

    def evidence(self, rel: str, extractor: str, node: ast.AST | None = None,
                 line: int | None = None, note: str | None = None) -> Evidence:
        start = line or getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None) if node is not None else start
        lines = self.snapshots.get(rel, "").splitlines()
        excerpt = "\n".join(lines[(start or 1) - 1:min(end or start or 1, (start or 1) + 3)])[:1000]
        return Evidence(source_path = rel, line_start = start, line_end = end, excerpt = excerpt,
                        extractor = extractor, note = note)

    def issue(self, code: str, message: str, rel: str | None = None,
              evidence: Evidence | None = None, severity: str = "warning",
              node_ids: list[str] | None = None) -> None:
        issue_id = stable_id("issue", code, rel or "", message,
                             str(evidence.line_start if evidence else ""))
        self.issues[issue_id] = GraphIssue(
            id = issue_id, code = code, message = message, severity = severity,
            node_ids = node_ids or ([self.script_ids[rel]] if rel in self.script_ids else []),
            evidence = [evidence] if evidence else [],
        )
        if rel in self.sources and severity != "info" and self.sources[rel].status == "parsed":
            self.sources[rel].status = "partial"

    def edge(self, source: str, target: str, kind: str, evidence: Evidence,
             status: str = "confirmed", condition: str | None = None) -> None:
        edge_id = stable_id("edge", source, target, kind, condition or "")
        if edge_id in self.edges:
            edge = self.edges[edge_id]
            if evidence not in edge.evidence:
                edge.evidence.append(evidence)
            # A less certain observation must not overwrite stronger evidence.
            if status == "confirmed":
                edge.status = "confirmed"
        else:
            self.edges[edge_id] = GraphEdge(id = edge_id, source = source, target = target, kind = kind,
                                            evidence = [evidence], status = status, condition = condition)

    def discover(self) -> None:
        candidates: list[tuple[str, Path]] = []
        total_bytes = 0

        def walk_error(error: OSError) -> None:
            self.issue("discovery_error", f"Could not inspect directory: {error}", severity = "error")

        for directory, dirs, files in os.walk(self.root, followlinks = False, onerror = walk_error):
            retained = []
            for name in sorted(dirs):
                path = Path(directory) / name
                rel = path.relative_to(self.root).as_posix()
                if name.casefold() in EXCLUDED_DIRECTORIES:
                    self.issue("directory_skipped", f"Excluded generated, dependency or cache directory: {rel}", severity = "info")
                elif path.is_symlink():
                    self.issue("directory_symlink_skipped", f"Directory symlink was not followed: {rel}", severity = "info")
                else:
                    retained.append(name)
            dirs[:] = retained
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix.lower() not in SCRIPT_TYPES:
                    continue
                rel = path.relative_to(self.root).as_posix()
                candidates.append((rel, path))

        for index, (rel, path) in enumerate(sorted(candidates)):
            kind = SCRIPT_TYPES[path.suffix.lower()]
            script_id = stable_id("script", rel)
            self.script_ids[rel] = script_id
            node = GraphNode(id = script_id, label = rel[:1000], kind = "script", script_type = kind,
                             details = {"relative_path": rel})
            self.nodes[script_id] = node
            try:
                # Preserve a visible skipped node when the shared source-path
                # contract cannot safely represent a filesystem name.
                if ":" in rel or "\\" in rel:
                    raise ValueError("source path contains unsupported colon or backslash characters")
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("symlink target is outside the selected project")
                if not path.is_file():
                    raise ValueError("source is not a regular file")
                size = path.stat().st_size
                if index >= MAX_SOURCE_FILES:
                    raise ValueError(f"source-file limit ({MAX_SOURCE_FILES}) reached")
                if size > MAX_FILE_BYTES:
                    raise ValueError(f"file exceeds the {MAX_FILE_BYTES}-byte analysis limit")
                if total_bytes + size > MAX_PROJECT_BYTES:
                    raise ValueError(f"project source-byte limit ({MAX_PROJECT_BYTES}) reached")
                with path.open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError("source grew beyond the file-size limit while reading")
                total_bytes += len(raw)
            except (OSError, ValueError) as exc:
                node.details.update({"analysis_status": "skipped", "reason": str(exc)})
                self.issue("source_skipped", f"Skipped {rel}: {exc}", rel, severity = "error")
                continue

            encoding, lossy = "utf-8", False
            try:
                if kind == "python":
                    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                    encoding = "utf-16"
                elif raw.startswith(b"\xef\xbb\xbf"):
                    encoding = "utf-8-sig"
                text = raw.decode(encoding)
            except (UnicodeError, SyntaxError, LookupError):
                encoding, lossy = "utf-8", True
                text = raw.decode("utf-8", errors = "replace")
            self.sources[rel] = SourceFile(path = rel, sha256 = hashlib.sha256(raw).hexdigest(),
                                          script_type = kind, size_bytes = len(raw), encoding = encoding)
            self.snapshots[rel] = text
            node.source_path = rel
            if lossy:
                self.sources[rel].status = "failed"
                self.issue("source_encoding_loss", "Source was not valid in its declared encoding; the snapshot contains replacement characters and is not used to infer dependencies.", rel, severity = "error")
        for rel, source in self.sources.items():
            if source.script_type != "python":
                continue
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for offset in range(len(parts)):
                self.module_candidates.setdefault(".".join(parts[offset:]), set()).add(rel)

    def file_resource(self, rel: str, value: str, evidence: Evidence,
                      cwd: Any = _DEFAULT_CWD) -> tuple[str, str]:
        path = _normalize_path(value)
        context = self.cwd if cwd is _DEFAULT_CWD else cwd
        uri = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path))
        if not _is_absolute(path) and not uri and context:
            path = _join_path(context, path)
        unresolved = not _is_absolute(path) and not uri
        key = "file:" + _json({"path": path, "scope": rel if unresolved else "absolute"})
        node_id = stable_id("file", key)
        if node_id not in self.nodes:
            self.nodes[node_id] = GraphNode(id = node_id, kind = "file", label = value[:1000], resource_key = key,
                details = {"normalized_path": path, "resolution": "runtime_cwd_unknown" if unresolved else "absolute_or_uri",
                         **({"scope_source": rel} if unresolved else {})})
        if unresolved:
            self.issue("relative_path_unresolved",
                       f"Runtime working directory is unknown for {value!r}; this resource is scoped to {rel}.", rel, evidence)
        return node_id, "proposed" if unresolved else "confirmed"

    def io(self, rel: str, path: str, kind: str, evidence: Evidence,
           owner: str | None = None, cwd: Any = _DEFAULT_CWD, proposed: bool = False) -> None:
        resource_id, status = self.file_resource(rel, path, evidence, cwd)
        script_id = owner or self.script_ids[rel]
        source, target = (resource_id, script_id) if kind == "reads" else (script_id, resource_id)
        self.edge(source, target, kind, evidence, "proposed" if proposed else status)

    def table_io(self, rel: str, parts: list[dict[str, Any]], kind: str, evidence: Evidence,
                 owner: str | None = None, context: str = "", session_local: bool = False) -> None:
        scope = {"namespace": self.namespace} if self.namespace else {"source": rel}
        if session_local:
            scope["session_source"] = rel
        key = "table:" + _json({**scope, "identifiers": parts, "session_context": context})
        table_id = stable_id("table", key)
        label = ".".join(('"' + p["name"].replace('"', '""') + '"') if p["quoted"] else p["name"] for p in parts)
        if session_local:
            label += " (temporary)"
        if table_id not in self.nodes:
            self.nodes[table_id] = GraphNode(id = table_id, kind = "table", label = label[:1000], resource_key = key,
                details = {"identifiers": parts, "database_namespace": self.namespace,
                         "scope_source": rel if not self.namespace or session_local else None,
                         "session_context": context, "session_local": session_local})
        if not self.namespace:
            self.issue("database_namespace_unresolved", "Database connection namespace is unknown; table identities are scoped to this source, including qualified names.", rel, evidence)
        source, target = (table_id, owner or self.script_ids[rel]) if kind == "reads" else (owner or self.script_ids[rel], table_id)
        self.edge(source, target, kind, evidence, "confirmed" if self.namespace else "proposed")

    def launch(self, rel: str, script_path: str, evidence: Evidence, cwd: Any = _DEFAULT_CWD,
               proposed: bool = False, owner: str | None = None) -> None:
        normalized = _normalize_path(script_path)
        context = self.cwd if cwd is _DEFAULT_CWD else cwd
        if not _is_absolute(normalized) and context:
            normalized = _join_path(context, normalized)
        target = None
        candidate = _project_relative_source(normalized, self.root)
        if candidate is not None:
            target = self.script_ids.get(candidate)
            if target is None and ntpath.splitdrive(self.root.as_posix())[0]:
                # Preserve the discovered source's real spelling and stable ID.
                # Never arbitrarily choose if a case-sensitive directory contains
                # multiple candidates that Windows path comparison considers equal.
                matches = [source_id for path, source_id in self.script_ids.items()
                           if PureWindowsPath(path) == PureWindowsPath(candidate)]
                if len(matches) == 1:
                    target = matches[0]
        if target:
            self.edge(owner or self.script_ids[rel], target, "calls", evidence, "proposed" if proposed else "confirmed")
        else:
            resource_id, status = self.file_resource(rel, script_path, evidence, cwd)
            self.edge(owner or self.script_ids[rel], resource_id, "calls", evidence, "proposed")
            self.issue("launch_target_unresolved", f"Launch target {script_path!r} was not resolved to an analyzed project source; no script-to-script connection was guessed.", rel, evidence)

    def command(self, rel: str, args: list[str], evidence: Evidence, cwd: Any = _DEFAULT_CWD,
                shell: bool = False, owner: str | None = None) -> None:
        if not args:
            return
        if shell and any(re.search(r"[|&<>`$]", item) for item in args):
            self.issue("dynamic_shell_command", "Shell expansion or command composition needs manual review.", rel, evidence)
            return
        program = ntpath.basename(args[0]).lower()
        if program in {"call", "exec"} and len(args) > 1:
            return self.command(rel, args[1:], evidence, cwd, shell, owner)
        if program in {"cmd", "cmd.exe"} and len(args) > 2 and args[1].lower() in {"/c", "/k"}:
            return self.command(rel, args[2:], evidence, cwd, True, owner)
        target = None
        if re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", program):
            tail = args[1:]
            while tail and tail[0] in {"-u", "-B", "-O", "-OO", "-s", "-S", "-E", "-I", "-3", "--"}:
                tail = tail[1:]
            if tail and not tail[0].startswith("-"):
                target = tail[0]
            else:
                self.issue("dynamic_python_launch", "Python inline code, module launch, or unsupported interpreter options need manual review.", rel, evidence)
        elif program in {"sqlcmd", "sqlcmd.exe", "psql", "psql.exe", "isql", "isql.exe"}:
            for flag in ("-i", "-f", "--file"):
                if flag in args and args.index(flag) + 1 < len(args):
                    target = args[args.index(flag) + 1]
                    break
            if target is None:
                self.issue("dynamic_sql_launch", "SQL client invocation has no supported literal input file.", rel, evidence)
        elif program in {"alteryxenginecmd", "alteryxenginecmd.exe"} and len(args) > 1:
            target = args[1]
        elif Path(args[0]).suffix.lower() in SCRIPT_TYPES:
            target = args[0]
        elif any(Path(arg).suffix.lower() in SCRIPT_TYPES for arg in args[1:]):
            self.issue("unsupported_script_launcher", "A command mentions source files but its invocation semantics are unsupported.", rel, evidence)
        if target is not None:
            self.launch(rel, target, evidence, cwd, proposed = shell, owner = owner)

    def finish(self) -> GraphDocument:
        digest = hashlib.sha256()
        for rel, source in sorted(self.sources.items()):
            digest.update(_json([rel, source.sha256]).encode("utf-8"))
            digest.update(b"\n")
        source_digest = digest.hexdigest()
        return GraphDocument(
            id = f"graph_{uuid4().hex}", title = self.title,
            project_root = self.root.as_posix(), source_digest = source_digest,
            sources = [self.sources[key] for key in sorted(self.sources)],
            nodes = sorted(self.nodes.values(), key = lambda n: n.id),
            edges = sorted(self.edges.values(), key = lambda e: e.id),
            issues = sorted(self.issues.values(), key = lambda i: i.id),
            analysis_options = {"working_directory": self.cwd, "sql_dialect": self.dialect,
                              "database_namespace": self.namespace, "extractor_version": "1.1",
                              "scope": "static_project_dependencies_not_full_control_flow",
                              "python_import_search": "project root and importing source directory; conflicting and nonstandard import roots require review",
                              "confirmed_means": "syntactic dependency; execution and reachability are not guaranteed",
                              "max_file_bytes": MAX_FILE_BYTES, "max_project_bytes": MAX_PROJECT_BYTES,
                              "excluded_directories": sorted(EXCLUDED_DIRECTORIES)},
        )


class _Bindings(ast.NodeVisitor):
    """Collect bindings without leaking names from nested lexical scopes.

    - Keep annotation-only declarations separate from assignments: at module or
      class level they do not replace an existing imported value.
    - Remember import declarations so equivalent repeated/fallback imports can
      be recognized by their actual source identity instead of their count.
    - Comprehension targets live in their own scope; named expressions in their
      expressions still count as bindings in the enclosing scope.
    """
    def __init__(self) -> None:
        self.writes: Counter[str] = Counter()
        self.imports: Counter[str] = Counter()
        self.annotations: Counter[str] = Counter()
        self.import_declarations: dict[str, list[tuple[ast.Import | ast.ImportFrom, ast.alias]]] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.imports[bound] += 1
            self.import_declarations.setdefault(bound, []).append((node, alias))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name
            self.imports[bound] += 1
            self.import_declarations.setdefault(bound, []).append((node, alias))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.writes[node.id] += 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.writes[node.name] += 1

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.annotations[node.target.id] += 1
        if node.value is not None:
            self.visit(node.target)
            self.visit(node.value)

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        for generator in node.generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,):
            self.visit(expression)

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension


class _Python(ast.NodeVisitor):
    READERS = {"read_csv", "read_table", "read_excel", "read_json", "read_parquet", "read_feather", "read_hdf", "read_pickle", "read_orc", "read_fwf"}
    WRITERS = {"to_csv", "to_excel", "to_json", "to_parquet", "to_feather", "to_hdf", "to_pickle", "to_orc"}
    LAUNCHERS = {"subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_call", "subprocess.check_output", "os.system", "os.popen", "os.execv", "os.execvp"}

    def __init__(self, analysis: _Analysis, rel: str, tree: ast.Module) -> None:
        self.a, self.rel = analysis, rel
        self.aliases: dict[str, str] = {}
        self.local_imports: dict[str, _ImportReference] = {}
        self.constants: dict[str, Any] = {}
        self.frames: set[str] = set()
        bindings = _Bindings()
        for statement in tree.body:
            bindings.visit(statement)
        self.writes = bindings.writes
        self.shadowed: set[str] = set(bindings.writes) | self._conflicting_imports(bindings)
        self.conditions: list[str] = []
        self.scopes: list[str] = []
        self.scope_parents: list[tuple[bool, tuple[Any, ...]]] = []
        self.mutated_attributes = {self.dotted(item) for item in ast.walk(tree)
                                   if isinstance(item, ast.Attribute) and isinstance(item.ctx, (ast.Store, ast.Del))}
        self.cwd = analysis.cwd
        chdir_aliases = {alias.asname or alias.name for item in ast.walk(tree)
                         if isinstance(item, ast.ImportFrom) and item.module == "os"
                         for alias in item.names if alias.name == "chdir"}
        cwd_calls = [item for item in ast.walk(tree) if isinstance(item, ast.Call) and
                     ((isinstance(item.func, ast.Attribute) and item.func.attr == "chdir") or
                      (isinstance(item.func, ast.Name) and item.func.id in chdir_aliases))]
        if cwd_calls:
            self.cwd = None
            analysis.issue("runtime_cwd_mutation", "This source can change the working directory; relative IO is source-scoped even when an initial working directory was supplied.",
                           rel, analysis.evidence(rel, "python_ast", cwd_calls[0]))

    def ev(self, node: ast.AST, note: str | None = None) -> Evidence:
        scope = "Python scope: " + (".".join(self.scopes) or "<module>")
        return self.a.evidence(self.rel, "python_ast", node, note = f"{scope}. {note}" if note else scope)

    def warning(self, code: str, message: str, node: ast.AST) -> None:
        self.a.issue(code, message, self.rel, self.ev(node))

    @staticmethod
    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = _Python.dotted(node.value)
            return f"{base}.{node.attr}" if base else ""
        return ""

    def canonical(self, node: ast.AST) -> str:
        dotted = self.dotted(node)
        first, *rest = dotted.split(".")
        if first in self.aliases:
            return ".".join([self.aliases[first], *rest])
        if first in {prefix.split(".")[0] for prefix in self.local_imports}:
            return ""
        if first in {"open", "str", "exec", "eval", "__import__", "getattr"} and first not in self.shadowed:
            return dotted
        return ""

    def literal(self, node: ast.AST | None) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "__file__" and node.id not in self.shadowed:
                return (self.a.root / self.rel).as_posix()
            return self.constants.get(node.id, _UNKNOWN)
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self.literal(item) for item in node.elts]
            return values if all(item is not _UNKNOWN for item in values) else _UNKNOWN
        if isinstance(node, ast.Attribute):
            if self.canonical(node) == "sys.executable":
                return "python"
            base = self.literal(node.value)
            if isinstance(base, _PathValue) and node.attr == "parent":
                return _PathValue(_normalize_path(_join_path(base.text, "..")))
        if isinstance(node, ast.BinOp):
            left, right = self.literal(node.left), self.literal(node.right)
            if isinstance(node.op, ast.Div) and isinstance(left, _PathValue) and isinstance(right, str):
                return _PathValue(_join_path(left.text, right))
            if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
                return left + right
        if isinstance(node, ast.Call):
            name = self.canonical(node.func)
            args = [self.literal(arg) for arg in node.args]
            if node.keywords:
                return _UNKNOWN
            if name in {"str", "builtins.str"} and len(args) == 1 and _path_text(args[0]) is not None:
                return _path_text(args[0])
            if name in {"pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath", "pathlib.PureWindowsPath"}:
                if args and all(_path_text(arg) is not None for arg in args):
                    result = _path_text(args[0])
                    for value in args[1:]:
                        result = _join_path(result, _path_text(value))
                    return _PathValue(result)
            if name in {"os.path.join", "posixpath.join", "ntpath.join"} and args and all(isinstance(arg, str) for arg in args):
                result = args[0]
                for arg in args[1:]:
                    result = _join_path(result, arg)
                return result
            if name in {"os.path.dirname", "posixpath.dirname", "ntpath.dirname"} and len(args) == 1 and isinstance(args[0], str):
                return _normalize_path(_join_path(args[0], ".."))
            if isinstance(node.func, ast.Attribute):
                base = self.literal(node.func.value)
                if isinstance(base, _PathValue):
                    if node.func.attr in {"resolve", "absolute"} and not args:
                        return _PathValue(_join_path(self.cwd, base.text) if self.cwd else _normalize_path(base.text))
                    if node.func.attr == "joinpath" and all(isinstance(arg, str) for arg in args):
                        value = base.text
                        for arg in args:
                            value = _join_path(value, arg)
                        return _PathValue(value)
                    if node.func.attr == "with_name" and len(args) == 1 and isinstance(args[0], str):
                        return _PathValue(_join_path(_join_path(base.text, ".."), args[0]))
        return _UNKNOWN

    def arg(self, call: ast.Call, index: int, *names: str) -> Any:
        for keyword in call.keywords:
            if keyword.arg in names:
                return self.literal(keyword.value)
        return self.literal(call.args[index]) if len(call.args) > index else _UNKNOWN

    def _conflicting_imports(self, bindings: _Bindings) -> set[str]:
        """Distinguish a repeated import from a binding to a different source.

        - Resolve import identities using the same conservative source lookup as
          normal imports; no modules are imported or executed by this check.
        - A package-relative import and its direct-launch fallback may point to
          the same source and symbol. Repeating that binding is not reassignment.
        - Ambiguous or different targets remain shadowed. Assignments and local
          argument shadowing are handled separately and remain conservative.
        """
        conflicting = set()
        for name, declarations in bindings.import_declarations.items():
            if len(declarations) < 2:
                continue
            identities = set()
            for node, alias in declarations:
                if isinstance(node, ast.Import):
                    target = self.find_module(alias.name)
                    identity = ("local", target, None) if target else ("external", alias.name, None)
                    # - `import package.module` binds the package; its qualified
                    #   lookup differs from `import package.module as package`.
                    identity += ("qualified" if not alias.asname and "." in alias.name else "direct",)
                else:
                    base = node.module or ""
                    target = self.find_module(base, node.level)
                    member = self.find_module(".".join(filter(None, [base, alias.name])), node.level)
                    identity = (("local", member or target, None if member else alias.name)
                                if member or target else ("external", "." * node.level + base, alias.name))
                    identity += ("direct",)
                identities.add(identity)
            if len(identities) > 1:
                conflicting.add(name)
        return conflicting

    def find_module(self, module: str, level: int = 0, node: ast.AST | None = None) -> str | None:
        self._last_import_ambiguous = False
        if not level and module.split(".")[0] in sys.builtin_module_names:
            return None
        parts = module.split(".") if module else []
        parent = Path(self.rel).parent
        if level:
            if level > len(parent.parts):
                if node is not None:
                    self.warning("relative_import_unresolved", "Relative import escapes the known package hierarchy.", node)
                return None
            for _ in range(level - 1):
                parent = parent.parent
            bases = [parent]
        else:
            bases = [Path("."), parent]
        candidates = set()
        for base in bases:
            path = base.joinpath(*parts)
            for candidate in (path.with_suffix(".py") if parts else path / "__init__.py", path / "__init__.py"):
                rel = candidate.as_posix()
                if rel in self.a.sources:
                    candidates.add(rel)
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1 and node is not None:
            self._last_import_ambiguous = True
            self.warning("ambiguous_local_import", f"Import {module!r} has multiple local candidates; runtime import paths must be reviewed.", node)
        elif not candidates and not level and module in self.a.module_candidates and module.split(".")[0] not in getattr(sys, "stdlib_module_names", ()):
            self._last_import_ambiguous = True
            if node is not None:
                self.warning("unresolved_import_root", f"Local sources could match {module!r}, but their runtime import root is unknown; no import path was guessed.", node)
        return None

    def import_edge(self, target: str, node: ast.AST) -> None:
        if target != self.rel:
            self.a.edge(self.a.script_ids[self.rel], self.a.script_ids[target], "imports", self.ev(node),
                        condition = "; ".join(self.conditions) or None)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = self.find_module(alias.name, node = node)
            if self._last_import_ambiguous:
                continue
            bound = alias.asname or alias.name.split(".")[0]
            if target:
                self.import_edge(target, node)
            if bound in self.shadowed:
                self.warning("rebound_import", f"Imported name {bound!r} is rebound in this scope; its calls are not resolved.", node)
                continue
            if target:
                self.local_imports[alias.asname or alias.name] = _ImportReference(target)
            else:
                self.aliases[bound] = alias.name if alias.asname else bound

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        target = self.find_module(base, node.level, node)
        if self._last_import_ambiguous:
            return
        if target:
            self.import_edge(target, node)
        for alias in node.names:
            if alias.name == "*":
                self.warning("wildcard_import", "Wildcard-imported call bindings cannot be resolved safely.", node)
                continue
            member = self.find_module(".".join(filter(None, [base, alias.name])), node.level, node)
            if member:
                self.import_edge(member, node)
            bound = alias.asname or alias.name
            if bound in self.shadowed:
                self.warning("rebound_import", f"Imported name {bound!r} is rebound in this scope; its calls are not resolved.", node)
                continue
            if member or target:
                self.local_imports[bound] = _ImportReference(member or target, None if member else alias.name)
            elif not node.level:
                self.aliases[bound] = ".".join(filter(None, [base, alias.name]))
            else:
                self.warning("relative_import_unresolved", f"Relative import {base!r} could not be matched to an analyzed source.", node)

    def _scope(self, node: ast.AST, body: list[ast.AST], arguments: ast.arguments | None = None,
               bound_names: set[str] | None = None) -> None:
        """Visit a nested lexical scope without leaking its temporary bindings.

        - Function, lambda and comprehension parameters hide outer imports and
          constants even when the parameter is not subsequently assigned.
        - Annotation-only function locals also hide outer names, but annotations
          do not invalidate a value explicitly imported in that same scope.
        - Class namespaces are not closure scopes. A method, lambda, nested
          class or comprehension body must resolve free names in the surrounding
          function/module, while defaults and the first iterable still use the
          immediate class namespace before this method is called.
        - Keep the scope name in evidence so a reviewer can locate repeated calls
          inside different functions without inspecting an ungrouped warning dump.
        """
        saved = self.aliases, self.local_imports, self.constants, self.frames, self.writes, self.shadowed, self.cwd
        enclosing = self.scope_parents[-1][1] if self.scope_parents and self.scope_parents[-1][0] else saved
        bindings = _Bindings()
        for statement in body:
            bindings.visit(statement)
        bound = set(bindings.writes) | set(bindings.imports) | (bound_names or set())
        shadowed = set(bindings.writes) | self._conflicting_imports(bindings) | (bound_names or set())
        if arguments:
            declared = set(bindings.annotations) - set(bindings.imports)
            bound.update(declared)
            shadowed.update(declared)
            bound.update(arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs])
            bound.update(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg)
            shadowed.update(arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs])
            shadowed.update(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg)
        self.aliases = {key: value for key, value in enclosing[0].items() if key.split(".")[0] not in bound}
        self.local_imports = {key: value for key, value in enclosing[1].items() if key.split(".")[0] not in bound}
        self.constants = {key: value for key, value in enclosing[2].items() if key not in bound}
        self.frames = enclosing[3] - bound
        self.writes, self.shadowed = bindings.writes, (enclosing[5] - set(bindings.imports)) | shadowed
        self.cwd = enclosing[6]
        self.scopes.append(getattr(node, "name", "<lambda>" if isinstance(node, ast.Lambda) else "<comprehension>"))
        self.scope_parents.append((isinstance(node, ast.ClassDef), enclosing))
        for statement in body:
            self.visit(statement)
        self.scope_parents.pop()
        self.scopes.pop()
        self.aliases, self.local_imports, self.constants, self.frames, self.writes, self.shadowed, self.cwd = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for expression in [*node.decorator_list, *node.args.defaults, *[x for x in node.args.kw_defaults if x]]:
            self.visit(expression)
        self._scope(node, node.body, node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [*node.decorator_list, *node.bases]:
            self.visit(expression)
        self._scope(node, node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # - Defaults are evaluated in the outer scope, just like normal functions.
        # - Analyze the body under its own argument bindings. The existence of a
        #   lambda alone is not evidence that a dependency is unresolved.
        for expression in [*node.args.defaults, *[item for item in node.args.kw_defaults if item]]:
            self.visit(expression)
        self._scope(node, [node.body], node.args)

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        # - Python evaluates the first iterable in the enclosing scope, then
        #   binds iteration targets inside a separate comprehension scope.
        # - Preserve this distinction so a target called `worker` neither hides
        #   a later `worker.run()` nor creates a false import call inside the loop.
        self.visit(node.generators[0].iter)
        bound = {item.id for generator in node.generators for item in ast.walk(generator.target)
                 if isinstance(item, ast.Name)}
        expressions = []
        for index, generator in enumerate(node.generators):
            if index:
                expressions.append(generator.iter)
            expressions.extend(generator.ifs)
        expressions.extend((node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,))
        self._scope(node, expressions, bound_names = bound)

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        before = self.aliases.copy(), self.local_imports.copy(), self.constants.copy(), self.frames.copy(), self.cwd
        test = ast.get_source_segment(self.a.snapshots[self.rel], node.test) or "conditional branch"
        self.conditions.append(test[:500])
        for statement in node.body:
            self.visit(statement)
        self.conditions.pop()
        body_state = self.aliases, self.local_imports, self.constants, self.frames, self.cwd
        self.aliases, self.local_imports, self.constants, self.frames, self.cwd = before
        self.conditions.append(f"else: {test[:500]}")
        for statement in node.orelse:
            self.visit(statement)
        self.conditions.pop()
        self.aliases = {key: value for key, value in self.aliases.items() if body_state[0].get(key) == value}
        self.local_imports = {key: value for key, value in self.local_imports.items() if body_state[1].get(key) == value}
        self.constants = {key: value for key, value in self.constants.items() if body_state[2].get(key, _UNKNOWN) == value}
        self.frames &= body_state[3]
        self.cwd = self.cwd if self.cwd == body_state[4] else None

    def _loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        """A loop can execute zero times; its last textual binding is not an exit value."""
        expression = node.test if isinstance(node, ast.While) else node.iter
        self.visit(expression)
        bindings = _Bindings()
        for statement in [*node.body, *node.orelse]:
            bindings.visit(statement)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            bindings.visit(node.target)
        affected = set(bindings.writes) | set(bindings.imports)
        before = self.aliases.copy(), self.local_imports.copy(), self.constants.copy(), self.frames.copy(), self.cwd

        def reset_unaffected() -> None:
            self.aliases = {key: value for key, value in before[0].items() if key.split(".")[0] not in affected}
            self.local_imports = {key: value for key, value in before[1].items() if key.split(".")[0] not in affected}
            self.constants = {key: value for key, value in before[2].items() if key not in affected}
            self.frames = before[3] - affected
            self.cwd = before[4]

        condition = ast.get_source_segment(self.a.snapshots[self.rel], expression) or "loop condition"
        reset_unaffected()
        self.conditions.append(f"{type(node).__name__.lower()}: {condition[:500]}")
        for statement in node.body:
            self.visit(statement)
        self.conditions.pop()
        body_cwd = self.cwd
        # A zero-iteration loop reaches its else without executing any body
        # assignments or imports, so do not carry body bindings into this branch.
        reset_unaffected()
        self.conditions.append("loop else (without break)")
        for statement in node.orelse:
            self.visit(statement)
        self.conditions.pop()
        else_cwd = self.cwd
        reset_unaffected()
        if body_cwd != before[4] or else_cwd != before[4]:
            self.cwd = None
        # - Drop uncertain bindings on every loop, including ordinary counters,
        #   but only flag a loop when an import/known dependency binding is lost.
        # - Dynamic filenames and commands already produce a targeted diagnostic
        #   at their actual use. Reporting every loop as a source-analysis problem
        #   obscures those useful warnings without making extraction any safer.
        dependency_bindings = set(bindings.imports) | (
            affected & ({name.split(".")[0] for name in [*before[0], *before[1]]}
                        | before[3] | {name for name, value in before[2].items() if isinstance(value, _PathValue)})
        )
        if dependency_bindings:
            names = ", ".join(repr(name) for name in sorted(dependency_bindings))
            self.warning("loop_binding_unresolved", f"Dependency binding(s) {names} are not treated as known after this loop; iteration, break and zero-iteration paths require review.", node)

    visit_For = _loop
    visit_AsyncFor = _loop
    visit_While = _loop

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = self.literal(node.value)
        frame = isinstance(node.value, ast.Call) and self.canonical(node.value.func) in {
            "pandas.DataFrame", *[f"pandas.{name}" for name in self.READERS],
            "pandas.read_sql", "pandas.read_sql_query", "pandas.read_sql_table",
        }
        for target in node.targets:
            for item in ast.walk(target):
                if isinstance(item, ast.Name):
                    self.constants.pop(item.id, None)
                    self.frames.discard(item.id)
                    if isinstance(target, ast.Name) and self.writes[item.id] == 1 and not self.conditions:
                        if value is not _UNKNOWN:
                            self.constants[item.id] = value
                        if frame:
                            self.frames.add(item.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            proxy = ast.Assign(targets = [node.target], value = node.value)
            ast.copy_location(proxy, node)
            self.visit_Assign(proxy)

    def _file_io(self, call: ast.Call, path: Any, kinds: list[str]) -> None:
        value = _path_text(path)
        if value is None:
            self.warning("dynamic_file_path", "File IO path is not a supported static literal; no resource was guessed.", call)
            return
        for kind in kinds:
            self.a.io(self.rel, value, kind, self.ev(call), cwd = self.cwd)

    def visit_Call(self, node: ast.Call) -> None:
        canonical, dotted = self.canonical(node.func), self.dotted(node.func)
        matches = [(prefix, target) for prefix, target in self.local_imports.items()
                   if dotted == prefix or dotted.startswith(prefix + ".")]
        if matches:
            prefix, reference = max(matches, key = lambda item: len(item[0]))
            target = reference.source_path
            member = ".".join(filter(None, [reference.symbol, dotted[len(prefix):].lstrip(".")]))
            if dotted in self.mutated_attributes:
                self.warning("rebound_callable", f"Callable {dotted!r} is assigned dynamically; no call target was guessed.", node)
            elif target != self.rel:
                confirmed = member in self.a.defined_callables(target)
                note = ("Direct reference to a locally defined callable; runtime reachability is not asserted."
                        if confirmed else "Call through a local import; re-exports, decorators and dynamic attributes are not resolved.")
                self.a.edge(self.a.script_ids[self.rel], self.a.script_ids[target], "calls", self.ev(node, note),
                            status = "confirmed" if confirmed else "proposed", condition = "; ".join(self.conditions) or None)
                if not confirmed:
                    self.warning("imported_callable_unresolved", f"Callable {dotted!r} is not directly defined in {target}; its module-level relationship is proposed for review.", node)
        path_receiver = self.literal(node.func.value) if isinstance(node.func, ast.Attribute) else _UNKNOWN
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        is_path = isinstance(path_receiver, _PathValue)
        known_io = canonical in self.LAUNCHERS or canonical in {"open", "builtins.open", "io.open"} or canonical.startswith("pandas.") or is_path
        if known_io and any(kw.arg is None for kw in node.keywords):
            self.warning("dynamic_call_kwargs", "Expanded keyword arguments can change IO or invocation semantics; this call requires review.", node)
            self.generic_visit(node)
            return
        if canonical in {"open", "builtins.open", "io.open"} or (is_path and method == "open"):
            value = path_receiver if is_path else self.arg(node, 0, "file")
            mode_index = 0 if is_path else 1
            mode = self.arg(node, mode_index, "mode")
            if mode is _UNKNOWN and len(node.args) <= mode_index and not any(kw.arg in {"mode", None} for kw in node.keywords):
                mode = "r"
            if not isinstance(mode, str) or not re.fullmatch(r"[rwaxbt+]+", mode):
                self.warning("dynamic_file_mode", "File access mode is dynamic or unsupported; read/write direction was not guessed.", node)
            else:
                kinds = (["reads"] if "r" in mode or "+" in mode else []) + (["writes"] if any(c in mode for c in "wax+") else [])
                self._file_io(node, value, kinds)
        elif is_path and method in {"read_text", "read_bytes", "write_text", "write_bytes"}:
            self._file_io(node, path_receiver, ["reads" if method.startswith("read") else "writes"])
        elif method in {"read_text", "read_bytes", "write_text", "write_bytes", "open"}:
            self.warning("dynamic_path_receiver", "Path or IO receiver is not statically resolvable; no file resource was guessed.", node)
        elif canonical in {f"pandas.{name}" for name in self.READERS}:
            self._file_io(node, self.arg(node, 0, "filepath_or_buffer", "path", "io", "path_or_buf", "path_or_buffer"), ["reads"])
        elif method in self.WRITERS:
            receiver = node.func.value
            known_frame = isinstance(receiver, ast.Name) and receiver.id in self.frames
            if isinstance(receiver, ast.Call):
                known_frame = self.canonical(receiver.func) in {"pandas.DataFrame", *[f"pandas.{name}" for name in self.READERS]}
            if known_frame:
                path = self.arg(node, 0, "path_or_buf", "path", "excel_writer")
                # Some pandas writers return text when no output path is supplied.
                supplied = bool(node.args) or any(kw.arg in {"path_or_buf", "path", "excel_writer"} for kw in node.keywords)
                if supplied and path is not None:
                    self._file_io(node, path, ["writes"])
            else:
                self.warning("unresolved_writer_receiver", f"Receiver of {method} is not statically known to be a pandas frame.", node)
        elif canonical in {"pandas.read_sql", "pandas.read_sql_query"}:
            sql = self.arg(node, 0, "sql")
            if isinstance(sql, str):
                _sql(self.a, self.rel, sql, line_offset = node.lineno - 1)
            else:
                self.warning("dynamic_sql", "SQL text is not a static literal; no tables were guessed.", node)
        elif canonical == "pandas.read_sql_table" or method == "to_sql":
            known_frame = method != "to_sql" or (isinstance(node.func.value, ast.Name) and node.func.value.id in self.frames)
            name, schema = self.arg(node, 0, "name", "table_name"), self.arg(node, 2, "schema")
            schema_supplied = len(node.args) > 2 or any(kw.arg == "schema" for kw in node.keywords)
            if isinstance(name, str) and known_frame and (not schema_supplied or schema is None or isinstance(schema, str)):
                parts = [{"name": item, "quoted": True} for item in ([schema] if isinstance(schema, str) else []) + [name]]
                self.a.table_io(self.rel, parts, "writes" if method == "to_sql" else "reads", self.ev(node))
            else:
                self.warning("dynamic_sql_table", "Table name, schema or writer receiver needs manual review.", node)
        elif canonical in self.LAUNCHERS:
            command = self.arg(node, 1 if canonical in {"os.execv", "os.execvp"} else 0, "args", "command")
            has_cwd = any(kw.arg == "cwd" for kw in node.keywords)
            cwd_value = self.arg(node, -1, "cwd") if has_cwd else None
            cwd = _path_text(cwd_value) if has_cwd and cwd_value is not None else self.cwd
            if cwd and not _is_absolute(cwd):
                if self.cwd:
                    cwd = _join_path(self.cwd, cwd)
                else:
                    self.warning("dynamic_launch_cwd", "Relative launch cwd depends on an unknown parent working directory.", node)
                    self.generic_visit(node)
                    return
            if cwd_value is not None and cwd is None:
                self.warning("dynamic_launch_cwd", "Launch working directory is dynamic; target cannot be resolved safely.", node)
            elif command is _UNKNOWN:
                self.warning("dynamic_launch", "Script launch arguments are dynamic; no target was guessed.", node)
            else:
                shell_value = self.arg(node, -1, "shell") if any(kw.arg == "shell" for kw in node.keywords) else False
                shell = canonical in {"os.system", "os.popen"} or shell_value is True
                if shell_value is _UNKNOWN:
                    self.warning("dynamic_shell_mode", "Shell mode is dynamic; launch semantics require review.", node)
                elif isinstance(command, str):
                    if shell:
                        try:
                            self.a.command(self.rel, shlex.split(command), self.ev(node), cwd, True)
                        except ValueError:
                            self.warning("invalid_shell_command", "Quoted shell command could not be parsed.", node)
                    else:
                        self.a.command(self.rel, [command], self.ev(node), cwd)
                elif isinstance(command, list) and all(_path_text(arg) is not None for arg in command):
                    if shell:
                        self.warning("shell_sequence_unsupported", "Sequence arguments with shell=True have platform-dependent semantics.", node)
                    else:
                        self.a.command(self.rel, [_path_text(arg) for arg in command], self.ev(node), cwd)
                else:
                    self.warning("dynamic_launch", "Launch arguments are not supported static strings.", node)
        elif canonical in {"exec", "eval", "__import__", "importlib.import_module", "runpy.run_path", "runpy.run_module", "os.chdir", "getattr"}:
            self.warning("dynamic_python_construct", f"{canonical} can change execution or dependency resolution; manual review is required.", node)
            if canonical == "os.chdir":
                # A call in a function may run at any point; do not evaluate chdir.
                self.cwd = None
        elif method in {"execute", "executemany", "executescript"}:
            self.warning("unresolved_database_call", "Database execution receiver and connection context are unknown; embedded SQL needs review.", node)
        elif isinstance(node.func, (ast.Subscript, ast.Call)):
            self.warning("dynamic_callable", "Callable is selected dynamically; no target was guessed.", node)
        self.generic_visit(node)


def _sql(analysis: _Analysis, rel: str, source: str, *, line_offset: int = 0, owner: str | None = None) -> None:
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.scope import Scope, traverse_scope
    except ImportError:
        analysis.issue("sql_parser_unavailable", "Install sqlglot to analyze SQL. Source is retained without guessed dependencies.", rel, severity = "error")
        analysis.sources[rel].status = "failed"
        return
    try:
        statements = sqlglot.parse(source, read = analysis.dialect, error_level = sqlglot.ErrorLevel.RAISE)
    except Exception as exc:
        evidence = analysis.evidence(rel, "sqlglot", line = line_offset + 1)
        analysis.issue("sql_parse_error", f"SQL parsing failed: {type(exc).__name__}: {str(exc)[:700]}", rel, evidence, "error")
        if line_offset == 0 and analysis.sources[rel].script_type == "sql":
            analysis.sources[rel].status = "failed"
        return
    context = ""
    temporary_tables: set[str] = set()
    for statement in statements:
        if statement is None:
            continue
        evidence = analysis.evidence(rel, "sqlglot", line = line_offset + 1)
        if isinstance(statement, (exp.Use, exp.Set)):
            context += "\n" + statement.sql()
            analysis.issue("sql_session_context", "SQL changes database/session context. Subsequent table identities retain this context and require review.", rel, evidence)
            continue
        supported = isinstance(statement, (exp.Query, exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.TruncateTable))
        if isinstance(statement, (exp.Create, exp.Drop, exp.Alter)):
            supported = str(statement.args.get("kind", "")).upper() in {"TABLE", "VIEW"}
        if not supported:
            analysis.issue("unsupported_sql_statement", f"SQL statement type {type(statement).__name__} is not covered; no relationships were inferred from it.", rel, evidence)
            continue
        excluded: set[int] = set()
        scoped_tables: set[int] = set()
        try:
            for scope in traverse_scope(statement):
                for table in scope.tables:
                    scoped_tables.add(id(table))
                    if not table.db and not table.catalog and isinstance(scope.sources.get(table.alias_or_name), Scope):
                        excluded.add(id(table))
        except Exception as exc:
            analysis.issue("sql_scope_unresolved", f"SQL alias/CTE scope could not be established ({type(exc).__name__}); this statement needs review.", rel, evidence)
            continue
        tables = list(statement.find_all(exp.Table))
        root_ctes = {cte.alias_or_name for cte in (statement.args.get("with_").expressions if statement.args.get("with_") else [])}
        for table in tables:
            if id(table) not in scoped_tables and not table.db and not table.catalog and table.name in root_ctes:
                excluded.add(id(table))
        writes: set[int] = set()
        reads_too: set[int] = set()
        target = statement.args.get("this")
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table) and not isinstance(statement, exp.Query):
            # T-SQL UPDATE alias ... FROM physical_table AS alias.
            aliases = [table for table in tables if table is not target and table.alias == target.name]
            if not target.db and not target.catalog and len(aliases) == 1:
                excluded.add(id(target))
                target = aliases[0]
            if id(target) not in excluded:
                writes.add(id(target))
                if isinstance(statement, (exp.Update, exp.Delete, exp.Merge)):
                    reads_too.add(id(target))
        if isinstance(statement, exp.Delete) and statement.args.get("tables"):
            # MySQL/T-SQL DELETE alias FROM table AS alias must not create an
            # extra resource for the alias, or write every joined table.
            writes.clear()
            reads_too.clear()
            delete_names = statement.args["tables"]
            for alias_table in delete_names:
                excluded.add(id(alias_table))
                candidates = [table for table in tables if table not in delete_names and
                              (table.alias == alias_table.name or (table.name == alias_table.name and table.db == alias_table.db))]
                if len(candidates) == 1:
                    writes.add(id(candidates[0]))
                    reads_too.add(id(candidates[0]))
                else:
                    analysis.issue("sql_delete_target_unresolved", "DELETE target alias is ambiguous; its write relationship was omitted.", rel, evidence)
        for into in statement.find_all(exp.Into):
            if isinstance(into.this, exp.Table):
                writes.add(id(into.this))
        if isinstance(statement, exp.TruncateTable):
            writes.update(id(table) for table in statement.expressions if isinstance(table, exp.Table))
        if isinstance(statement, exp.Create) and isinstance(target, exp.Table) and statement.find(exp.TemporaryProperty):
            temporary_tables.add(_json([part.name for part in target.parts]))
        for table in tables:
            if id(table) in excluded:
                continue
            if table.find_ancestor(exp.Reference):
                analysis.issue("sql_schema_reference", "A schema/foreign-key reference is not represented as a data-read relationship.", rel, evidence, severity = "info")
                continue
            if not table.name or not isinstance(table.this, exp.Identifier):
                analysis.issue("dynamic_sql_table", "Table-valued function or dynamic table expression requires review.", rel, evidence)
                continue
            parts = [{"name": part.name, "quoted": bool(part.args.get("quoted"))} for part in table.parts if isinstance(part, exp.Identifier)]
            if len(parts) != len(table.parts):
                analysis.issue("dynamic_sql_table", "Computed table identifier requires review.", rel, evidence)
                continue
            line = table.this.meta.get("line", 1) + line_offset
            session_local = bool(table.this.args.get("temporary")) or _json([part.name for part in table.parts]) in temporary_tables
            table_evidence = analysis.evidence(rel, "sqlglot", line = line,
                note = "Parsed table reference; CTE and local aliases are excluded. SQL parsing does not validate runtime execution.")
            if id(table) in writes:
                analysis.table_io(rel, parts, "writes", table_evidence, owner, context, session_local)
            if id(table) not in writes or id(table) in reads_too:
                analysis.table_io(rel, parts, "reads", table_evidence, owner, context, session_local)


def _batch(analysis: _Analysis, rel: str, source: str) -> None:
    cwd = analysis.cwd
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = raw.strip().lstrip("@")
        if not line or line.lower().startswith(("rem ", "::", "echo ")) or line.startswith(":"):
            continue
        evidence = analysis.evidence(rel, "batch_literals", line = lineno)
        if line.endswith("^") or re.match(r"(?i)^(?:if|for|goto)\b", line):
            if re.search(r"(?i)\b(?:cd|chdir|pushd|popd)\b", line):
                cwd = None
            analysis.issue("batch_control_flow_unresolved", "Batch conditional, loop, jump or continuation requires manual review; no execution order was inferred.", rel, evidence)
            continue
        if re.match(r"(?i)^(?:set|setlocal|endlocal)\b", line):
            analysis.issue("batch_environment_unresolved", "Batch environment mutations are not evaluated; variable-based dependencies require review.", rel, evidence, severity = "info")
            continue
        line = re.sub(r"(?i)%~dp0", lambda _: (analysis.root / rel).parent.as_posix() + "/", line)
        if re.search(r"%[^%]*%|%[0-9*]|![^!]+!", line):
            if re.match(r"(?i)^(?:cd|chdir|pushd|popd)\b", line):
                cwd = None
            analysis.issue("dynamic_batch_variable", "Batch launch contains unresolved variables or arguments.", rel, evidence)
            continue
        try:
            lexer = shlex.shlex(line, posix = False, punctuation_chars = "&|<>")
            lexer.whitespace_split, lexer.commenters = True, ""
            tokens = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token for token in lexer]
            # Batch always treats backslashes as separators, even when analyzed
            # on macOS/Linux; Python POSIX filenames retain literal backslashes.
            tokens = [token.replace("\\", "/") if not ntpath.splitdrive(token)[0] else token for token in tokens]
        except ValueError:
            analysis.issue("batch_parse_error", "Batch command quoting could not be parsed.", rel, evidence)
            continue
        if any(token and set(token) <= set("&|<>") for token in tokens):
            analysis.issue("batch_compound_command", "Batch command composition or redirection needs manual review.", rel, evidence)
            continue
        if tokens and tokens[0].lower() in {"cd", "chdir", "pushd", "popd"}:
            values = [token for token in tokens[1:] if token.lower() != "/d"]
            if tokens[0].lower() in {"cd", "chdir"} and len(values) == 1 and (_is_absolute(values[0]) or cwd):
                cwd = _normalize_path(values[0]) if _is_absolute(values[0]) else _join_path(cwd, values[0])
            else:
                cwd = None
                analysis.issue("batch_cwd_unresolved", "Batch working-directory change cannot be resolved statically.", rel, evidence)
            continue
        if tokens and tokens[0].lower() == "call" and len(tokens) > 1 and tokens[1].startswith(":"):
            analysis.issue("batch_subroutine_unresolved", "Internal batch subroutine control flow is not represented by the project dependency graph.", rel, evidence, severity = "info")
            continue
        analysis.command(rel, tokens, evidence, cwd)
        if tokens and tokens[0].lower() == "call":
            cwd = None
            analysis.issue("batch_call_runtime_context", "A called batch program can change its caller's working directory; later relative targets need review.", rel, evidence)


def _alteryx(analysis: _Analysis, rel: str, source: str) -> None:
    try:
        from defusedxml import ElementTree
        root = ElementTree.fromstring(source, forbid_dtd = True, forbid_entities = True, forbid_external = True)
    except Exception as exc:
        analysis.sources[rel].status = "failed"
        analysis.issue("alteryx_parse_error", f"Alteryx XML was not safely parseable: {type(exc).__name__}: {str(exc)[:500]}", rel, severity = "error")
        return

    def tag(element: Any) -> str:
        return element.tag.rsplit("}", 1)[-1]

    elements = [element for element in root.iter() if tag(element) == "Node" and "ToolID" in element.attrib]
    if len(elements) > MAX_XML_TOOLS:
        analysis.sources[rel].status = "failed"
        analysis.issue("alteryx_tool_limit", "Alteryx file exceeds the supported tool-count limit; tool graph was skipped.", rel, severity = "error")
        return
    counts = Counter(element.attrib["ToolID"] for element in elements)
    tool_ids: dict[str, str] = {}
    evidence = analysis.evidence(rel, "alteryx_xml", note = "Explicit XML tool configuration/connection; source line is unavailable from the safe XML parser.")
    for element in elements:
        tool_id = element.attrib["ToolID"]
        if counts[tool_id] > 1:
            analysis.issue("duplicate_alteryx_tool", f"Duplicate ToolID {tool_id}; ambiguous tool connections were omitted.", rel, evidence)
            continue
        node_id = stable_id("tool", rel, tool_id)
        tool_ids[tool_id] = node_id
        gui = next((child for child in element if tag(child) == "GuiSettings"), None)
        plugin = gui.attrib.get("Plugin", "") if gui is not None else ""
        annotation = next((child.text for child in element.iter() if tag(child) == "AnnotationText" and child.text), "")
        analysis.nodes[node_id] = GraphNode(id = node_id, kind = "process", source_path = rel,
            label = (annotation or plugin.rsplit(".", 1)[-1] or f"Tool {tool_id}")[:1000],
            details = {"parent_script_id": analysis.script_ids[rel], "tool_id": tool_id, "plugin": plugin})
        lowered = plugin.casefold()
        direction = "reads" if "dbfileinput" in lowered else "writes" if "dbfileoutput" in lowered else None
        if direction:
            for file in (child for child in element.iter() if tag(child) == "File" and child.text):
                value = file.text.strip()
                if re.match(r"(?i)^(?:odbc:|odb:|oledb:|oci:|db:)", value):
                    analysis.issue("alteryx_database_connection", "Alteryx database connection requires explicit namespace/driver review; connection credentials are not copied into resource labels.", rel,
                                   evidence.model_copy(update = {"excerpt": ""}))
                    continue
                value = re.sub(r"(?i)%Engine.WorkflowDirectory%", lambda _: (analysis.root / rel).parent.as_posix(), value)
                if re.search(r"%[^%]+%", value):
                    analysis.issue("dynamic_alteryx_path", "Alteryx IO path contains an unresolved workflow variable.", rel, evidence)
                    continue
                # Excel/database sheet selectors are not part of the physical filename.
                path = value.split("|||", 1)[0]
                if not ntpath.splitdrive(path)[0]:
                    path = path.replace("\\", "/")
                if path:
                    analysis.io(rel, path, direction, evidence, owner = node_id)
        for engine in (child for child in element if tag(child) == "EngineSettings"):
            macro = engine.attrib.get("Macro")
            if macro:
                analysis.launch(rel, macro, evidence, owner = node_id)
        if any(name in lowered for name in ("python", "rtool", "runcommand", "dynamicinput", "dynamicoutput")):
            analysis.issue("alteryx_dynamic_tool", f"Tool {tool_id} can introduce dependencies beyond explicit XML wiring; review its configuration.", rel, evidence)
    for connection in (element for element in root.iter() if tag(element) == "Connection"):
        origin = next((child for child in connection if tag(child) == "Origin"), None)
        destination = next((child for child in connection if tag(child) == "Destination"), None)
        if origin is None or destination is None:
            continue
        source_id, target_id = tool_ids.get(origin.attrib.get("ToolID", "")), tool_ids.get(destination.attrib.get("ToolID", ""))
        if not source_id or not target_id:
            analysis.issue("dangling_alteryx_connection", "XML connection refers to a missing or ambiguous ToolID; it was omitted.", rel, evidence)
            continue
        ports = f"{origin.attrib.get('Connection', '')} → {destination.attrib.get('Connection', '')}"
        analysis.edge(source_id, target_id, "control_flow", evidence, condition = ports)


def analyze_project(script_folder: str | Path, *, working_directory: str | None = None,
                    sql_dialect: str | None = None, database_namespace: str | None = None,
                    title: str | None = None, logger: Any = None) -> tuple[GraphDocument, dict[str, str]]:
    """Analyze bounded source snapshots without running source code or an LLM.

    ``working_directory`` is runtime context, not the directory of every script;
    relative values are interpreted against the selected project root.  Only
    supply ``database_namespace`` when analyzed scripts share that connection
    namespace.  Unreadable/oversized/escaping files remain visible as skipped
    script nodes, without fabricating hashes or snapshot content.
    """
    root = Path(script_folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Script folder is not a directory: {root}")
    # - Resolve the optional project name here so API, CLI and direct analysis
    #   callers all share the same source-folder default on every platform.
    # - Never update titles on previously saved graphs when a new analysis starts.
    from .project_identity import project_title
    selected_folder = Path(script_folder).expanduser().absolute()
    analysis = _Analysis(root, working_directory, sql_dialect, database_namespace, project_title(selected_folder, title), logger)
    analysis.discover()
    for rel, source in sorted(analysis.sources.items()):
        if logger is not None:
            logger(f"Analyzing {rel} with static {source.script_type} extraction.")
        text = analysis.snapshots[rel]
        if source.status == "failed":
            analysis.nodes[analysis.script_ids[rel]].details["analysis_status"] = "failed"
            continue
        try:
            if source.script_type == "python":
                tree = ast.parse(text, filename = rel)
                if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
                    raise ValueError("Python AST exceeds the supported analysis-size limit")
                _Python(analysis, rel, tree).visit(tree)
            elif source.script_type == "sql":
                _sql(analysis, rel, text)
            elif source.script_type == "bat":
                _batch(analysis, rel, text)
            elif source.script_type == "alteryx":
                _alteryx(analysis, rel, text)
        except Exception as exc:
            source.status = "failed"
            evidence = analysis.evidence(rel, f"{source.script_type}_parser", line = getattr(exc, "lineno", None))
            analysis.issue("source_analysis_failed", f"Could not fully analyze {rel}: {type(exc).__name__}: {str(exc)[:700]}", rel, evidence, "error")
        analysis.nodes[analysis.script_ids[rel]].details["analysis_status"] = source.status
    return analysis.finish(), analysis.snapshots
