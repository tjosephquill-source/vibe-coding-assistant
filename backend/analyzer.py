"""
Static analysis engine — parses Python, JavaScript/TypeScript, and HTML
source files and extracts classes, functions, and their relationships
into a graph structure.
"""

import ast
import re
import os
import json
import hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from collections import defaultdict


# ── Supported file types ────────────────────────────────────────────

_SOURCE_GLOBS = ("*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.html", "*.htm")
_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}
_HTML_EXTENSIONS = {".html", ".htm"}
_SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", "dist", "build",
    ".next", ".nuxt", "coverage", ".nyc_output", "vendor",
    "bower_components", ".tox", ".mypy_cache", ".pytest_cache",
    "venv", ".venv", "env", ".env", ".idea", ".vscode",
    ".eggs", "egg-info", ".bundle", ".cache", ".parcel-cache",
    "target", "out", "bin", "obj", "lib", ".gradle", ".mvn",
    ".terraform", ".serverless",
}


# ── Cache directory ─────────────────────────────────────────────────

_VCA_CACHE_DIR = Path(__file__).parent.parent / ".vca_cache"


def _ensure_cache_dir() -> Path:
    _VCA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _VCA_CACHE_DIR


def _source_fingerprint(root_dir: str) -> str:
    """Hash all source file paths + contents under root_dir for cache invalidation."""
    h = hashlib.sha256()
    src_files = sorted(set(
        f for g in _SOURCE_GLOBS for f in Path(root_dir).rglob(g)
    ))
    for src_file in src_files:
        rel = os.path.relpath(str(src_file), root_dir)
        if any(part.startswith(".") or part in _SKIP_DIRS for part in Path(rel).parts):
            continue
        h.update(rel.encode())
        try:
            h.update(src_file.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:20]


def _cache_path(root_dir: str, abstract: bool) -> Path:
    """Return the cache JSON file path for a given analysis config."""
    dir_hash = hashlib.md5(os.path.abspath(root_dir).encode()).hexdigest()[:10]
    tag = "abs_heuristic" if abstract else "full"
    return _ensure_cache_dir() / f"{dir_hash}_{tag}.json"


def _load_from_cache(root_dir: str, abstract: bool) -> Optional[dict]:
    """Try to load a cached analysis result. Returns None on miss or stale."""
    path = _cache_path(root_dir, abstract)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("_fingerprint") == _source_fingerprint(root_dir):
            return data.get("graph")
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def _save_to_cache(root_dir: str, abstract: bool, graph_dict: dict) -> None:
    """Persist an analysis result to disk."""
    path = _cache_path(root_dir, abstract)
    payload = {
        "_fingerprint": _source_fingerprint(root_dir),
        "graph": graph_dict,
    }
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── Graph data model ────────────────────────────────────────────────

class NodeKind(str, Enum):
    CLASS = "class"
    METHOD = "method"
    ISLAND_CHAIN = "island_chain"
    GROUP = "group"


class EdgeKind(str, Enum):
    INHERITS = "inherits"
    CALLS = "calls"
    IMPORTS = "imports"
    CONTAINS = "contains"           # class → method
    INSTANTIATES = "instantiates"
    USES_TYPE = "uses_type"         # type annotation / parameter type


@dataclass
class Node:
    id: str
    kind: NodeKind
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    methods: list[str] = field(default_factory=list)
    variables: list[dict] = field(default_factory=list)    # [{"name": ..., "type": ...}, ...]
    members: list[str] = field(default_factory=list)      # for island chain nodes (class names)
    member_methods: list[str] = field(default_factory=list) # for island chain nodes (method/function names)
    member_ids: list[str] = field(default_factory=list)   # for island chain drilldown
    bases: list[str] = field(default_factory=list)
    docstring: Optional[str] = None
    display_name: Optional[str] = None                     # set when name is ambiguous
    depth: int = 0                                         # hierarchy depth level


@dataclass
class Edge:
    source: str       # node id
    target: str       # node id
    kind: EdgeKind
    label: Optional[str] = None


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    # Nodes/edges from intermediate hierarchy levels (for multi-depth drill-down)
    intermediate_nodes: list[Node] = field(default_factory=list)
    intermediate_edges: list[Edge] = field(default_factory=list)

    def add_node(self, node: Node) -> None:
        if not any(n.id == node.id for n in self.nodes):
            self.nodes.append(node)

    def add_edge(self, edge: Edge) -> None:
        # avoid exact duplicates
        for e in self.edges:
            if e.source == edge.source and e.target == edge.target and e.kind == edge.kind:
                return
        self.edges.append(edge)

    def to_dict(self) -> dict:
        d = {
            "nodes": [_node_to_dict(n) for n in self.nodes],
            "edges": [_edge_to_dict(e) for e in self.edges],
        }
        if self.intermediate_nodes:
            d["intermediate_nodes"] = [_node_to_dict(n) for n in self.intermediate_nodes]
        if self.intermediate_edges:
            d["intermediate_edges"] = [_edge_to_dict(e) for e in self.intermediate_edges]
        _enrich_languages(d)
        return d


_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html",
}


def _enrich_languages(graph_dict: dict) -> None:
    """Add a ``languages`` list to every node dict.

    For leaf nodes the language is derived from the file extension.
    For container nodes (island chain / group) the languages are the
    union of their members' languages, resolved recursively.
    """
    by_id: dict[str, dict] = {}
    for nd in graph_dict.get("nodes", []):
        by_id[nd["id"]] = nd
    for nd in graph_dict.get("intermediate_nodes", []):
        by_id[nd["id"]] = nd

    cache: dict[str, set[str]] = {}

    def _langs(node_id: str) -> set[str]:
        if node_id in cache:
            return cache[node_id]
        nd = by_id.get(node_id)
        if not nd:
            cache[node_id] = set()
            return cache[node_id]
        fp = nd.get("file_path", "")
        ext = os.path.splitext(fp)[1].lower() if fp else ""
        lang = _EXT_TO_LANG.get(ext)
        if lang:
            cache[node_id] = {lang}
            return cache[node_id]
        # Aggregate from members (island chain / group nodes)
        result: set[str] = set()
        for mid in nd.get("member_ids") or []:
            result.update(_langs(mid))
        cache[node_id] = result
        return result

    for nd in graph_dict.get("nodes", []):
        nd["languages"] = sorted(_langs(nd["id"]))
    for nd in graph_dict.get("intermediate_nodes", []):
        nd["languages"] = sorted(_langs(nd["id"]))


def _node_to_dict(n: Node) -> dict:
    d = asdict(n)
    d["kind"] = n.kind.value
    return d


def _edge_to_dict(e: Edge) -> dict:
    d = asdict(e)
    d["kind"] = e.kind.value
    return d


def _make_id(file_path: str, name: str) -> str:
    raw = f"{file_path}::{name}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── AST visitor ─────────────────────────────────────────────────────


def _extract_class_variables(class_node: ast.ClassDef) -> list[dict]:
    """Extract class-level and instance-level variables with their types.

    Handles:
      - Annotated class attributes:  ``name: str``
      - Assigned class attributes:   ``name = value``
      - Instance attributes in __init__:  ``self.name = value``
      - Annotated __init__ params:   ``def __init__(self, name: str)``
    Returns a de-duplicated list of ``{"name": ..., "type": ...}`` dicts.
    """
    seen: dict[str, str] = {}  # name → type (last wins)

    for item in class_node.body:
        # ── Class-level annotated assignments:  name: str [= ...] ──
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            var_name = item.target.id
            var_type = ast.unparse(item.annotation) if item.annotation else "unknown"
            seen[var_name] = var_type

        # ── Class-level plain assignments:  name = value ──
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    var_name = target.id
                    if var_name not in seen:
                        seen[var_name] = _infer_type_from_value(item.value)

        # ── Instance variables from __init__ ──
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            # First, collect annotated parameter types
            param_types: dict[str, str] = {}
            for arg in item.args.args:
                if arg.arg == "self":
                    continue
                if arg.annotation:
                    param_types[arg.arg] = ast.unparse(arg.annotation)

            for stmt in ast.walk(item):
                # self.name: type = ...
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Attribute):
                    if isinstance(stmt.target.value, ast.Name) and stmt.target.value.id == "self":
                        var_name = stmt.target.attr
                        var_type = ast.unparse(stmt.annotation) if stmt.annotation else "unknown"
                        seen[var_name] = var_type

                # self.name = value
                elif isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                            var_name = target.attr
                            if var_name not in seen:
                                # Try to infer from matching param annotation
                                if isinstance(stmt.value, ast.Name) and stmt.value.id in param_types:
                                    seen[var_name] = param_types[stmt.value.id]
                                else:
                                    seen[var_name] = _infer_type_from_value(stmt.value)

    return [{"name": n, "type": t} for n, t in seen.items()]


def _infer_type_from_value(node: ast.expr) -> str:
    """Best-effort type inference from an AST value node."""
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    if isinstance(node, ast.NameConstant):  # Python 3.7 compat
        return type(node.value).__name__
    return "unknown"


def _extract_method_variables(func_node: ast.FunctionDef) -> list[dict]:
    """Extract parameters and local variables with their types from a method.

    Handles:
      - Annotated parameters:  ``def f(x: int)``
      - Return type:           ``def f() -> str``
      - Annotated locals:      ``x: int = ...``
      - Assigned locals:       ``x = value``
    Returns a list of ``{"name": ..., "type": ...}`` dicts.
    """
    seen: dict[str, str] = {}

    # ── Parameters ──
    for arg in func_node.args.args:
        if arg.arg == "self" or arg.arg == "cls":
            continue
        if arg.annotation:
            seen[arg.arg] = ast.unparse(arg.annotation)
        else:
            seen[arg.arg] = "unknown"

    # *args
    if func_node.args.vararg:
        va = func_node.args.vararg
        t = ast.unparse(va.annotation) if va.annotation else "unknown"
        seen[f"*{va.arg}"] = t

    # **kwargs
    if func_node.args.kwarg:
        kw = func_node.args.kwarg
        t = ast.unparse(kw.annotation) if kw.annotation else "unknown"
        seen[f"**{kw.arg}"] = t

    # ── Return type ──
    if func_node.returns:
        seen["return"] = ast.unparse(func_node.returns)

    # ── Local variables (top-level statements in the function body only) ──
    for stmt in func_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name not in seen:
                seen[name] = ast.unparse(stmt.annotation) if stmt.annotation else "unknown"
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    if name not in seen:
                        seen[name] = _infer_type_from_value(stmt.value)

    return [{"name": n, "type": t} for n, t in seen.items()]

class _ClassCollector(ast.NodeVisitor):
    """First pass: collect all class and function definitions."""

    def __init__(self, file_path: str, module_qname: str):
        self.file_path = file_path
        self.module_qname = module_qname
        self.classes: dict[str, Node] = {}
        self.functions: dict[str, Node] = {}
        self.loose_functions: dict[str, Node] = {}   # module-level functions
        self._current_class: Optional[str] = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qname = f"{self.module_qname}.{node.name}"
        nid = _make_id(self.file_path, node.name)

        methods = [
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))

        cls_node = Node(
            id=nid,
            kind=NodeKind.CLASS,
            name=node.name,
            qualified_name=qname,
            file_path=self.file_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            methods=methods,
            variables=_extract_class_variables(node),
            bases=bases,
            docstring=ast.get_docstring(node),
        )
        self.classes[node.name] = cls_node

        # Visit methods inside the class
        prev = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = prev

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self._current_class:
            # ── Top-level (loose) function ──
            qname = f"{self.module_qname}.{node.name}"
            nid = _make_id(self.file_path, node.name)
            fn_node = Node(
                id=nid,
                kind=NodeKind.METHOD,
                name=node.name,
                qualified_name=qname,
                file_path=self.file_path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                variables=_extract_method_variables(node),
                docstring=ast.get_docstring(node),
            )
            self.loose_functions[qname] = fn_node
            return

        qname = f"{self.module_qname}.{self._current_class}.{node.name}"
        nid = _make_id(self.file_path, f"{self._current_class}.{node.name}")

        fn_node = Node(
            id=nid,
            kind=NodeKind.METHOD,
            name=node.name,
            qualified_name=qname,
            file_path=self.file_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            variables=_extract_method_variables(node),
            docstring=ast.get_docstring(node),
        )
        self.functions[qname] = fn_node

    visit_AsyncFunctionDef = visit_FunctionDef


class _RelationshipCollector(ast.NodeVisitor):
    """Second pass: extract relationships (calls, type refs, instantiations)."""

    def __init__(self, file_path: str, module_qname: str, known_classes: set[str]):
        self.file_path = file_path
        self.module_qname = module_qname
        self.known_classes = known_classes
        self.imports: dict[str, str] = {}
        self.import_sources: dict[str, str] = {}
        self.calls: list[tuple[str, str, int]] = []
        self.type_refs: list[tuple[str, str]] = []
        self.class_refs: list[tuple[str, str]] = []   # (context, class_name)
        self._context_stack: list[str] = []

    @property
    def _context(self) -> str:
        return ".".join(self._context_stack) if self._context_stack else "__module__"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            self.imports[name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            name = alias.asname or alias.name
            self.imports[name] = f"{module}.{alias.name}"
            self.import_sources[name] = module

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._context_stack.append(node.name)
        self.generic_visit(node)
        self._context_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._context_stack.append(node.name)

        for arg in node.args.args:
            if arg.annotation:
                type_name = self._extract_name(arg.annotation)
                if type_name and type_name in self.known_classes:
                    self.type_refs.append((self._context, type_name))

        if node.returns:
            type_name = self._extract_name(node.returns)
            if type_name and type_name in self.known_classes:
                self.type_refs.append((self._context, type_name))

        self.generic_visit(node)
        self._context_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        callee = self._extract_name(node.func)
        if callee:
            self.calls.append((self._context, callee, node.lineno))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Record bare references to known class names (e.g. OrderStatus)."""
        if node.id in self.known_classes:
            self.class_refs.append((self._context, node.id))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Record attribute access on known classes (e.g. OrderStatus.CONFIRMED)."""
        if isinstance(node.value, ast.Name) and node.value.id in self.known_classes:
            self.class_refs.append((self._context, node.value.id))
        self.generic_visit(node)

    @staticmethod
    def _extract_name(node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None


# ── JavaScript / TypeScript / HTML regex-based analysis ─────────────

def _strip_js_comments(source: str) -> str:
    """Remove JS/TS comments while preserving line numbers."""
    def _keep_newlines(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")
    source = re.sub(r"/\*.*?\*/", _keep_newlines, source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", "", source)
    return source


_JS_CLASS_PAT = re.compile(
    r"(?:export\s+(?:default\s+)?)?class\s+(\w+)"
    r"(?:\s*<[^{]*?>)?"                              # optional type params
    r"(?:\s+extends\s+([\w.]+)(?:\s*<[^{]*?>)?)?"    # optional extends
    r"(?:\s+implements\s+[^{]+)?"                     # optional implements
    r"\s*\{",
)

_JS_METHOD_PAT = re.compile(
    r"^\s+(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?"
    r"(?!if\b|for\b|while\b|switch\b|catch\b|return\b|throw\b|new\b|else\b"
    r"|var\b|let\b|const\b|import\b|export\b)"
    r"(\w+)\s*\(([^)]*)\)[^{\n]*\{",
    re.MULTILINE,
)

_JS_FUNCTION_PAT = re.compile(
    r"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*\(",
)

_JS_NEW_PAT = re.compile(r"\bnew\s+(\w+)\s*(?:<[^>]*>)?\s*\(")

_JS_REQUIRE_PAT = re.compile(
    r"(?:const|let|var)\s+(?:\{([^}]+)\}|(\w+))\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
)

# ── JS/TS class property patterns ──────────────────────────────────

# this.propName = ...  (in constructor)
_JS_THIS_ASSIGN_PAT = re.compile(
    r"\bthis\.(\w+)\s*=",
)

# TypeScript class field:  propName: Type  or  propName: Type = value
_TS_CLASS_FIELD_PAT = re.compile(
    r"^\s+(?:(?:public|private|protected|readonly|static|override|abstract)\s+)*"
    r"(\w+)\s*(?:\?\s*)?:\s*([\w\[\]<>,\s|&]+?)(?:\s*=|;|\n)",
    re.MULTILINE,
)

# TypeScript constructor parameter property:  constructor(public name: Type, ...)
_TS_CTOR_PARAM_PAT = re.compile(
    r"(?:public|private|protected|readonly)\s+(\w+)\s*(?:\?\s*)?:\s*([\w\[\]<>,\s|&]+?)(?:\s*[,)])",
)

_HTML_SCRIPT_PAT = re.compile(
    r"<script([^>]*)>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


def _find_closing_brace_pos(source: str, open_pos: int) -> int:
    """Return the position *after* the '}' matching the '{' at open_pos.

    Handles nested braces and skips over string literals (single, double,
    and template).  Falls back to end-of-string if unbalanced.
    """
    depth = 1
    i = open_pos + 1
    n = len(source)
    in_sq = in_dq = in_tpl = False
    while i < n and depth > 0:
        ch = source[i]
        prev = source[i - 1] if i > 0 else ""
        if in_sq:
            if ch == "'" and prev != "\\":
                in_sq = False
        elif in_dq:
            if ch == '"' and prev != "\\":
                in_dq = False
        elif in_tpl:
            if ch == "`" and prev != "\\":
                in_tpl = False
        else:
            if ch == "'":
                in_sq = True
            elif ch == '"':
                in_dq = True
            elif ch == "`":
                in_tpl = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        i += 1
    return i


def _extract_html_scripts(html_source: str) -> list[tuple[str, int]]:
    """Extract inline ``<script>`` contents from HTML.

    Returns a list of ``(script_source, line_offset)`` tuples where
    *line_offset* is the 0-based line number of the ``<script>`` tag so
    that reported line numbers match the original HTML file.
    """
    results: list[tuple[str, int]] = []
    for m in _HTML_SCRIPT_PAT.finditer(html_source):
        attrs = m.group(1)
        content = m.group(2).strip()
        # Skip external scripts (<script src="...">)
        if re.search(r"\bsrc\s*=", attrs, re.IGNORECASE):
            continue
        # Skip non-JS types (e.g. type="application/json")
        type_match = re.search(r'\btype\s*=\s*[\'"]([^\'"]+)[\'"]', attrs, re.IGNORECASE)
        if type_match:
            stype = type_match.group(1).lower()
            if stype not in (
                "text/javascript", "application/javascript", "module",
                "text/ecmascript", "application/ecmascript",
            ):
                continue
        if content:
            line_offset = html_source[: m.start(2)].count("\n")
            results.append((content, line_offset))
    return results


def _extract_js_class_variables(class_body: str) -> list[dict]:
    """Extract variables/properties from a JS/TS class body.

    Handles:
      - ``this.propName = ...`` assignments in constructor
      - TypeScript class fields: ``propName: Type``
      - TypeScript constructor parameter properties: ``constructor(public name: Type)``
    Returns a de-duplicated list of ``{"name": ..., "type": ...}`` dicts.
    """
    seen: dict[str, str] = {}  # name → type

    # TypeScript class-level fields:  propName: Type
    for m in _TS_CLASS_FIELD_PAT.finditer(class_body):
        name = m.group(1)
        ts_type = m.group(2).strip()
        if name not in ("constructor", "static", "get", "set", "async"):
            seen[name] = ts_type

    # Find the constructor body and extract this.x = ... assignments
    ctor_match = re.search(
        r"\bconstructor\s*\(([^)]*)\)[^{]*\{", class_body
    )
    if ctor_match:
        # Extract TS constructor parameter properties
        ctor_params = ctor_match.group(1)
        for m in _TS_CTOR_PARAM_PAT.finditer(ctor_params):
            name = m.group(1)
            ts_type = m.group(2).strip()
            seen[name] = ts_type

        # Find constructor body
        ctor_open = class_body.find("{", ctor_match.start())
        if ctor_open != -1:
            ctor_close = _find_closing_brace_pos(class_body, ctor_open)
            ctor_body = class_body[ctor_open + 1 : ctor_close - 1]
            for m in _JS_THIS_ASSIGN_PAT.finditer(ctor_body):
                name = m.group(1)
                if name not in seen:
                    seen[name] = "any"

    return [{"name": n, "type": t} for n, t in seen.items()]


# Regex to parse a single TS parameter:  name: Type  or  name?: Type
_TS_PARAM_PAT = re.compile(
    r"(\w+)\s*\??\s*:\s*([\w\[\]<>,\s|&]+?)(?:\s*[,)=]|$)"
)


def _extract_js_method_variables(params_str: str) -> list[dict]:
    """Extract parameter names and types from a JS/TS method signature."""
    seen: dict[str, str] = {}
    params_str = params_str.strip()
    if not params_str:
        return []

    # Try TypeScript typed params first
    for m in _TS_PARAM_PAT.finditer(params_str):
        name = m.group(1)
        ts_type = m.group(2).strip()
        seen[name] = ts_type

    # If no typed params found, extract plain JS param names
    if not seen:
        for part in params_str.split(","):
            part = part.strip()
            if not part:
                continue
            # Handle destructuring / rest / defaults
            part = re.sub(r"\s*=\s*.*$", "", part)  # remove defaults
            m = re.match(r"\.{3}(\w+)|(\w+)", part)
            if m:
                name = m.group(1) or m.group(2)
                seen[name] = "any"

    return [{"name": n, "type": t} for n, t in seen.items()]


class _JSClassCollector:
    """Regex-based first-pass collector for JS/TS files.

    Produces the same ``classes`` / ``functions`` dicts as the Python
    ``_ClassCollector`` so the downstream edge-creation logic works
    unchanged.
    """

    def __init__(self, file_path: str, module_qname: str):
        self.file_path = file_path
        self.module_qname = module_qname
        self.classes: dict[str, Node] = {}
        self.functions: dict[str, Node] = {}
        self.loose_functions: dict[str, Node] = {}   # standalone functions
        self._class_ranges: dict[str, tuple[int, int]] = {}

    def collect(self, source: str, line_offset: int = 0) -> None:
        cleaned = _strip_js_comments(source)

        for match in _JS_CLASS_PAT.finditer(cleaned):
            class_name = match.group(1)
            base_name = match.group(2)
            line_start = cleaned[: match.start()].count("\n") + 1 + line_offset

            # Locate opening brace and find closing brace
            open_brace = cleaned.find("{", match.start())
            if open_brace == -1:
                continue
            close_pos = _find_closing_brace_pos(cleaned, open_brace)
            line_end = cleaned[:close_pos].count("\n") + 1 + line_offset

            class_body = cleaned[open_brace + 1 : close_pos - 1]

            # Detect methods inside the class body
            methods: list[str] = []
            for mm in _JS_METHOD_PAT.finditer(class_body):
                methods.append(mm.group(1))

            # Extract variables / properties
            variables = _extract_js_class_variables(class_body)

            nid = _make_id(self.file_path, class_name)
            cls_node = Node(
                id=nid,
                kind=NodeKind.CLASS,
                name=class_name,
                qualified_name=f"{self.module_qname}.{class_name}",
                file_path=self.file_path,
                line_start=line_start,
                line_end=line_end,
                methods=methods,
                variables=variables,
                bases=[base_name] if base_name else [],
            )
            self.classes[class_name] = cls_node
            self._class_ranges[class_name] = (line_start, line_end)

            # Create method nodes
            for mm in _JS_METHOD_PAT.finditer(class_body):
                method_name = mm.group(1)
                method_params = mm.group(2)
                method_line = line_start + class_body[: mm.start()].count("\n") + 1
                fn_qname = f"{self.module_qname}.{class_name}.{method_name}"
                fn_id = _make_id(self.file_path, f"{class_name}.{method_name}")
                fn_node = Node(
                    id=fn_id,
                    kind=NodeKind.METHOD,
                    name=method_name,
                    qualified_name=fn_qname,
                    file_path=self.file_path,
                    line_start=method_line,
                    line_end=method_line,
                    variables=_extract_js_method_variables(method_params),
                )
                self.functions[fn_qname] = fn_node

        # ── Standalone (top-level) functions ────────────────────────
        for match in _JS_FUNCTION_PAT.finditer(cleaned):
            func_name = match.group(1)
            func_line = cleaned[: match.start()].count("\n") + 1 + line_offset
            # Skip if inside a class body
            inside_class = False
            for cls_name, (cs, ce) in self._class_ranges.items():
                if cs <= func_line <= ce:
                    inside_class = True
                    break
            if inside_class:
                continue
            fn_qname = f"{self.module_qname}.{func_name}"
            fn_id = _make_id(self.file_path, func_name)
            fn_node = Node(
                id=fn_id,
                kind=NodeKind.METHOD,
                name=func_name,
                qualified_name=fn_qname,
                file_path=self.file_path,
                line_start=func_line,
                line_end=func_line,
            )
            self.loose_functions[fn_qname] = fn_node


class _JSRelationshipCollector:
    """Regex-based second-pass collector for JS/TS relationships.

    Exposes the same attribute interface (``imports``, ``import_sources``,
    ``calls``, ``type_refs``, ``class_refs``) as ``_RelationshipCollector``
    so the shared edge-creation helper works for both languages.
    """

    # Common JS / DOM / Web API built-in type names that should never be
    # matched as user-defined class references.  Without this, a Python
    # class named ``Node`` (for example) would falsely match every DOM
    # ``Node`` reference in JS code.
    _JS_BUILTIN_NAMES: set[str] = {
        # DOM / Web API
        "Node", "Element", "Document", "Window", "Event", "EventTarget",
        "HTMLElement", "SVGElement", "Text", "Comment", "Attr",
        "MutationObserver", "IntersectionObserver", "ResizeObserver",
        "Request", "Response", "Headers", "URL", "FormData",
        "Blob", "File", "Image", "Worker", "MessageChannel",
        # JS built-in objects
        "Object", "Array", "Function", "Symbol", "Date", "RegExp",
        "Promise", "Proxy", "Reflect", "Iterator", "Generator",
        "Map", "Set", "WeakMap", "WeakSet", "WeakRef",
        "ArrayBuffer", "DataView", "SharedArrayBuffer",
        "Int8Array", "Uint8Array", "Float32Array", "Float64Array",
        # Errors
        "Error", "TypeError", "RangeError", "SyntaxError",
        "ReferenceError", "URIError", "EvalError",
        # Other common globals
        "JSON", "Math", "Intl", "Console",
    }

    def __init__(self, file_path: str, module_qname: str, known_classes: set[str]):
        self.file_path = file_path
        self.module_qname = module_qname
        # Exclude JS built-in names from cross-file class matching
        self.known_classes = known_classes - self._JS_BUILTIN_NAMES
        self.imports: dict[str, str] = {}
        self.import_sources: dict[str, str] = {}
        self.calls: list[tuple[str, str, int]] = []
        self.type_refs: list[tuple[str, str]] = []
        self.class_refs: list[tuple[str, str]] = []

    def collect(
        self,
        source: str,
        class_ranges: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        cleaned = _strip_js_comments(source)
        class_ranges = class_ranges or {}

        # Extract JSDoc type references BEFORE stripping comments
        self._collect_jsdoc_types(source, class_ranges)
        self._collect_imports(cleaned)
        self._collect_refs(cleaned, class_ranges)

    # ── JSDoc type extraction ────────────────────────────────────────

    # Patterns for @type {ClassName}, @param {ClassName}, @returns {ClassName}
    _JSDOC_TYPE_PAT = re.compile(
        r"@(?:type|param|returns?|var|member|typedef)\s+\{([^}]+)}"
    )

    def _collect_jsdoc_types(
        self,
        source: str,
        class_ranges: dict[str, tuple[int, int]],
    ) -> None:
        """Extract type references from JSDoc annotations."""
        lines = source.split("\n")
        for line_num, line in enumerate(lines, 1):
            for m in self._JSDOC_TYPE_PAT.finditer(line):
                type_str = m.group(1)
                # Split on common type operators: |, &, <, >, ,
                parts = re.split(r"[|&<>,\s]+", type_str)
                context = self._line_context(line_num, class_ranges)
                for part in parts:
                    part = part.strip().rstrip("?").rstrip("[]")
                    if part in self.known_classes:
                        self.class_refs.append((context, part))

    # ── imports ──────────────────────────────────────────────────────

    def _collect_imports(self, cleaned: str) -> None:
        # import { Foo, Bar } from 'module'   /   import type { … } from '…'
        for m in re.finditer(
            r"(?:import|export)\s+(?:type\s+)?\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]",
            cleaned,
        ):
            names_str, module = m.group(1), m.group(2)
            for raw in names_str.split(","):
                name = raw.strip().split(" as ")[-1].strip()
                original = raw.strip().split(" as ")[0].strip()
                if name:
                    self.imports[name] = f"{module}.{original}"
                    self.import_sources[name] = module

        # import Foo from 'module'
        for m in re.finditer(
            r"import\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]", cleaned
        ):
            name, module = m.group(1), m.group(2)
            if name not in ("type",):
                self.imports[name] = f"{module}.{name}"
                self.import_sources[name] = module

        # const Foo = require('module')
        for m in _JS_REQUIRE_PAT.finditer(cleaned):
            named, default, module = m.group(1), m.group(2), m.group(3)
            if default:
                self.imports[default] = f"{module}.{default}"
                self.import_sources[default] = module
            if named:
                for n in named.split(","):
                    n = n.strip()
                    if n:
                        self.imports[n] = f"{module}.{n}"
                        self.import_sources[n] = module

    # ── instantiations & class references ────────────────────────────

    def _collect_refs(
        self,
        cleaned: str,
        class_ranges: dict[str, tuple[int, int]],
    ) -> None:
        lines = cleaned.split("\n")
        for line_num, line in enumerate(lines, 1):
            context = self._line_context(line_num, class_ranges)

            # new ClassName(…)
            for m in _JS_NEW_PAT.finditer(line):
                self.calls.append((context, m.group(1), line_num))

            # Bare references to known class names (exact case)
            for cls_name in self.known_classes:
                if cls_name in line and re.search(
                    r"\b" + re.escape(cls_name) + r"\b", line
                ):
                    self.class_refs.append((context, cls_name))

            # Case-insensitive parameter/identifier matching:
            # Catches patterns like constructor(layoutEngine) where
            # LayoutEngine is a known class.  We match identifiers whose
            # PascalCase form equals a known class name — e.g. the
            # camelCase variant "layoutEngine" → "LayoutEngine".
            for m in re.finditer(r"\b([a-z]\w{2,})\b", line):
                word = m.group(1)
                # Convert camelCase to PascalCase (capitalise first letter)
                pascal = word[0].upper() + word[1:]
                if pascal in self.known_classes:
                    self.class_refs.append((context, pascal))

    @staticmethod
    def _line_context(
        line_num: int, class_ranges: dict[str, tuple[int, int]]
    ) -> str:
        for cls_name, (start, end) in class_ranges.items():
            if start <= line_num <= end:
                return cls_name
        return "__module__"


# ── Display-name disambiguation ─────────────────────────────────────

def _disambiguate_display_names(graph: Graph) -> None:
    """Set ``display_name`` on nodes whose ``name`` is shared by multiple nodes.

    When two or more nodes have the same ``name`` (e.g. ``mainFunction`` in
    both ``test.py`` and ``train.py``), each receives a ``display_name`` that
    appends the source filename for clarity — e.g. ``mainFunction (test.py)``.

    Nodes with unique names are left with ``display_name = None`` so the
    frontend can fall back to ``name``.
    """
    from collections import Counter

    name_counts = Counter(n.name for n in graph.nodes)
    for n in graph.nodes:
        if name_counts[n.name] > 1 and n.file_path:
            stem = Path(n.file_path).name  # e.g. "test.py"
            if not stem:
                stem = Path(n.file_path).stem
            n.display_name = f"{n.name}  ({stem})"


# ── Analysis engine ─────────────────────────────────────────────────

def _module_qname(file_path: str, root: str) -> str:
    rel = os.path.relpath(file_path, root)
    parts = Path(rel).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def analyze_codebase(root_dir: str) -> Graph:
    """
    Walk a codebase directory, parse every .py / .js / .ts / .html file,
    and return a Graph of classes/methods and their relationships.
    """
    root_dir = os.path.abspath(root_dir)
    graph = Graph()

    all_classes: dict[str, Node] = {}
    all_functions: dict[str, Node] = {}
    file_collectors: list[tuple[str, str, _ClassCollector]] = []
    js_file_collectors: list[tuple[str, str, _JSClassCollector]] = []

    # ── Pass 1a: Python files ───────────────────────────────────────
    py_files = sorted(Path(root_dir).rglob("*.py"))

    for py_file in py_files:
        file_path = str(py_file)
        rel_path = os.path.relpath(file_path, root_dir)

        if any(part.startswith(".") or part in _SKIP_DIRS for part in Path(rel_path).parts):
            continue

        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            continue

        mod_qname = _module_qname(file_path, root_dir)

        collector = _ClassCollector(rel_path, mod_qname)
        collector.visit(tree)
        file_collectors.append((file_path, rel_path, collector))

        for cls_name, cls_node in collector.classes.items():
            all_classes[cls_name] = cls_node
            graph.add_node(cls_node)

        for fn_qname, fn_node in collector.functions.items():
            all_functions[fn_qname] = fn_node
            graph.add_node(fn_node)

            parts = fn_qname.split(".")
            parent_class_name = parts[-2]
            if parent_class_name in all_classes:
                graph.add_edge(Edge(
                    source=all_classes[parent_class_name].id,
                    target=fn_node.id,
                    kind=EdgeKind.CONTAINS,
                    label="has method",
                ))

        # Register top-level (loose) functions
        for fn_qname, fn_node in collector.loose_functions.items():
            all_functions[fn_qname] = fn_node
            graph.add_node(fn_node)

    # ── Pass 1b: JavaScript / TypeScript files ──────────────────────
    js_files = sorted(set(
        f for g in ("*.js", "*.jsx", "*.ts", "*.tsx")
        for f in Path(root_dir).rglob(g)
    ))

    for js_file in js_files:
        file_path = str(js_file)
        rel_path = os.path.relpath(file_path, root_dir)

        if any(part.startswith(".") or part in _SKIP_DIRS
               for part in Path(rel_path).parts):
            continue

        try:
            source = js_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        mod_qname = _module_qname(file_path, root_dir)
        collector = _JSClassCollector(rel_path, mod_qname)
        collector.collect(source)

        if not collector.classes and not collector.functions:
            continue

        js_file_collectors.append((file_path, rel_path, collector))
        _register_collected(graph, collector, all_classes, all_functions)

    # ── Pass 1c: HTML files (inline scripts) ────────────────────────
    html_files = sorted(set(
        f for g in ("*.html", "*.htm")
        for f in Path(root_dir).rglob(g)
    ))

    for html_file in html_files:
        file_path = str(html_file)
        rel_path = os.path.relpath(file_path, root_dir)

        if any(part.startswith(".") or part in _SKIP_DIRS
               for part in Path(rel_path).parts):
            continue

        try:
            source = html_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        scripts = _extract_html_scripts(source)
        if not scripts:
            continue

        mod_qname = _module_qname(file_path, root_dir)
        collector = _JSClassCollector(rel_path, mod_qname)
        for script_src, offset in scripts:
            collector.collect(script_src, line_offset=offset)

        if not collector.classes and not collector.functions:
            continue

        js_file_collectors.append((file_path, rel_path, collector))
        _register_collected(graph, collector, all_classes, all_functions)

    # ── Build cross-reference indexes ───────────────────────────────
    known_class_names = set(all_classes.keys())

    methods_by_class: dict[str, dict[str, Node]] = defaultdict(dict)
    for fn in all_functions.values():
        parts = fn.qualified_name.split(".")
        if len(parts) >= 2:
            cls_name = parts[-2]
            methods_by_class[cls_name][fn.name] = fn

    # Save (class_collector, rel_collector) pairs for the import-edge pass
    file_rel_data: list[tuple] = []

    # ── Pass 2a: Python relationships ───────────────────────────────
    for file_path, rel_path, class_collector in file_collectors:
        source = Path(file_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_path)
        mod_qname = _module_qname(file_path, root_dir)

        rel_collector = _RelationshipCollector(rel_path, mod_qname, known_class_names)
        rel_collector.visit(tree)
        file_rel_data.append((class_collector, rel_collector))

        _add_collector_edges(
            graph, class_collector, rel_collector, mod_qname,
            all_classes, all_functions, methods_by_class,
        )

    # ── Pass 2b: JS / HTML relationships ────────────────────────────
    for file_path, rel_path, class_collector in js_file_collectors:
        try:
            source = Path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # For HTML files, analyse only the inline script content
        if Path(file_path).suffix.lower() in _HTML_EXTENSIONS:
            scripts = _extract_html_scripts(source)
            source = "\n".join(s for s, _ in scripts)
            class_ranges: dict[str, tuple[int, int]] = {}
        else:
            class_ranges = class_collector._class_ranges

        mod_qname = _module_qname(file_path, root_dir)
        rel_collector = _JSRelationshipCollector(rel_path, mod_qname, known_class_names)
        rel_collector.collect(source, class_ranges)
        file_rel_data.append((class_collector, rel_collector))

        _add_collector_edges(
            graph, class_collector, rel_collector, mod_qname,
            all_classes, all_functions, methods_by_class,
        )

    # ── Fallback IMPORTS edges ──────────────────────────────────────
    # For known classes imported into a file but not directly referenced by
    # any class in that file (i.e. no INHERITS / INSTANTIATES / USES_TYPE /
    # CALLS edge already exists), create an IMPORTS edge.  This catches
    # module-level-only usage that the class-context visitors cannot see.
    _connected: set[tuple[str, str]] = set()
    for e in graph.edges:
        if e.kind not in (EdgeKind.CONTAINS, EdgeKind.IMPORTS):
            _connected.add((e.source, e.target))

    for class_collector, rel_collector in file_rel_data:
        for local_name, qualified_name in rel_collector.imports.items():
            simple_name = qualified_name.split(".")[-1]
            if simple_name not in all_classes:
                continue
            target = all_classes[simple_name]
            for cls_node in class_collector.classes.values():
                if cls_node.id == target.id:
                    continue
                if (cls_node.id, target.id) not in _connected:
                    graph.add_edge(Edge(
                        source=cls_node.id,
                        target=target.id,
                        kind=EdgeKind.IMPORTS,
                        label=f"imports {simple_name}",
                    ))

    # ── Disambiguate duplicate node names ────────────────────────────
    _disambiguate_display_names(graph)

    return graph


def _register_collected(
    graph: Graph,
    collector: _JSClassCollector,
    all_classes: dict[str, Node],
    all_functions: dict[str, Node],
) -> None:
    """Register classes & methods from a JS/HTML collector into the graph."""
    for cls_name, cls_node in collector.classes.items():
        all_classes[cls_name] = cls_node
        graph.add_node(cls_node)

    for fn_qname, fn_node in collector.functions.items():
        all_functions[fn_qname] = fn_node
        graph.add_node(fn_node)

        parts = fn_qname.split(".")
        parent_class_name = parts[-2]
        if parent_class_name in all_classes:
            graph.add_edge(Edge(
                source=all_classes[parent_class_name].id,
                target=fn_node.id,
                kind=EdgeKind.CONTAINS,
                label="has method",
            ))

    # Register top-level (loose) JS/TS functions
    for fn_qname, fn_node in collector.loose_functions.items():
        all_functions[fn_qname] = fn_node
        graph.add_node(fn_node)


def _add_collector_edges(
    graph: Graph,
    class_collector,
    rel_collector,
    mod_qname: str,
    all_classes: dict[str, Node],
    all_functions: dict[str, Node],
    methods_by_class: dict[str, dict[str, Node]],
) -> None:
    """Shared edge-creation logic for both Python and JS/HTML collectors."""
    # Inheritance
    for cls_name, cls_node in class_collector.classes.items():
        for base_name in cls_node.bases:
            simple_base = base_name.split(".")[-1]
            if simple_base in all_classes:
                graph.add_edge(Edge(
                    source=cls_node.id,
                    target=all_classes[simple_base].id,
                    kind=EdgeKind.INHERITS,
                    label=f"extends {simple_base}",
                ))

    # Calls / instantiations
    for context, callee_name, lineno in rel_collector.calls:
        # ── Resolve the caller node ──
        caller_fn = None
        caller_class_name = None
        if context != "__module__" and "." in context:
            # Call from inside a class method (e.g. context = "MyClass.do_stuff")
            caller_class_name = context.split(".")[0]
            caller_qname = f"{mod_qname}.{context}"
            caller_fn = all_functions.get(caller_qname)
        elif context != "__module__":
            # Call from inside a top-level function (e.g. context = "_ensure_cache_dir")
            caller_qname = f"{mod_qname}.{context}"
            caller_fn = all_functions.get(caller_qname)

        # ── Resolve the callee node ──
        # 1) Same-class method call
        if caller_fn and caller_class_name:
            callee_fn = methods_by_class.get(caller_class_name, {}).get(callee_name)
            if callee_fn:
                graph.add_edge(Edge(
                    source=caller_fn.id,
                    target=callee_fn.id,
                    kind=EdgeKind.CALLS,
                    label=f"calls {callee_name}",
                ))

        # 2) Call to a known top-level function (from any caller)
        if caller_fn:
            callee_loose_qname = f"{mod_qname}.{callee_name}"
            callee_loose = all_functions.get(callee_loose_qname)

            # 2b) Cross-module: resolve via imports table
            if callee_loose is None and callee_name in rel_collector.imports:
                imported_qname = rel_collector.imports[callee_name]
                callee_loose = all_functions.get(imported_qname)

            if callee_loose and callee_loose.id != caller_fn.id:
                graph.add_edge(Edge(
                    source=caller_fn.id,
                    target=callee_loose.id,
                    kind=EdgeKind.CALLS,
                    label=f"calls {callee_name}",
                ))

        # 3) Instantiation: call to a known class
        if callee_name in all_classes:
            # From a class context → class-to-class instantiation
            if caller_class_name and caller_class_name in all_classes:
                graph.add_edge(Edge(
                    source=all_classes[caller_class_name].id,
                    target=all_classes[callee_name].id,
                    kind=EdgeKind.INSTANTIATES,
                    label=f"creates {callee_name}",
                ))
            # From a top-level function context → function-to-class instantiation
            elif caller_fn and not caller_class_name:
                graph.add_edge(Edge(
                    source=caller_fn.id,
                    target=all_classes[callee_name].id,
                    kind=EdgeKind.INSTANTIATES,
                    label=f"creates {callee_name}",
                ))

    # Type references
    for context, type_name in rel_collector.type_refs:
        if type_name in all_classes:
            caller_class_name = context.split(".")[0] if context != "__module__" else None
            if caller_class_name and caller_class_name in all_classes and caller_class_name != type_name:
                graph.add_edge(Edge(
                    source=all_classes[caller_class_name].id,
                    target=all_classes[type_name].id,
                    kind=EdgeKind.USES_TYPE,
                    label=f"depends on {type_name}",
                ))
            elif context != "__module__" and "." not in context:
                # Top-level function referencing a type
                fn_qname = f"{mod_qname}.{context}"
                fn_node = all_functions.get(fn_qname)
                if fn_node:
                    graph.add_edge(Edge(
                        source=fn_node.id,
                        target=all_classes[type_name].id,
                        kind=EdgeKind.USES_TYPE,
                        label=f"depends on {type_name}",
                    ))

    # Class name references
    for context, class_name in rel_collector.class_refs:
        if class_name in all_classes:
            caller_class_name = context.split(".")[0] if context != "__module__" else None
            if caller_class_name and caller_class_name in all_classes and caller_class_name != class_name:
                graph.add_edge(Edge(
                    source=all_classes[caller_class_name].id,
                    target=all_classes[class_name].id,
                    kind=EdgeKind.USES_TYPE,
                    label=f"references {class_name}",
                ))



# ── Community-based abstraction ─────────────────────────────────────

def _build_adjacency(node_ids: set[str], edges: list[Edge]) -> dict[str, set[str]]:
    """Build undirected adjacency map for the given node IDs, ignoring 'contains' edges."""
    adj: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for e in edges:
        if e.kind == EdgeKind.CONTAINS:
            continue
        s, t = e.source, e.target
        if s in node_ids and t in node_ids:
            adj[s].add(t)
            adj[t].add(s)
    return adj


def _find_connected_components(node_ids: set[str], adj: dict[str, set[str]]) -> list[set[str]]:
    """Return list of connected components as sets of node IDs."""
    visited: set[str] = set()
    components: list[set[str]] = []
    for nid in node_ids:
        if nid in visited:
            continue
        comp: set[str] = set()
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.add(cur)
            for nb in adj.get(cur, set()):
                if nb not in visited and nb in node_ids:
                    stack.append(nb)
        if comp:
            components.append(comp)
    return components


def _count_bridge_edges(community: set[str], all_node_ids: set[str], edges: list[Edge]) -> int:
    """Count edges that cross the community boundary (one end in, one end out).
    An edge counts as a bridge if one end is in the community and the other end
    is in all_node_ids but NOT in the community. Ignores 'contains' edges."""
    count = 0
    for e in edges:
        if e.kind == EdgeKind.CONTAINS:
            continue
        s, t = e.source, e.target
        # We only care about edges where at least one end is in the community
        s_in = s in community
        t_in = t in community
        if not s_in and not t_in:
            continue
        # Bridge: one end inside, other end outside (but still in the graph)
        if s_in and not t_in and t in all_node_ids:
            count += 1
        elif t_in and not s_in and s in all_node_ids:
            count += 1
    return count


def _count_internal_edges(community: set[str], edges: list[Edge]) -> int:
    """Count edges where both ends are inside the community. Ignores 'contains'."""
    count = 0
    for e in edges:
        if e.kind == EdgeKind.CONTAINS:
            continue
        if e.source in community and e.target in community:
            count += 1
    return count


def _find_communities(
    node_ids: set[str],
    edges: list[Edge],
    max_bridge_edges: int = 2,
    min_community_size: int = 3,
    max_community_size: int = 100,
) -> list[set[str]]:
    """
    Find candidate communities: connected subgraphs where almost all edges
    are internal, with at most `max_bridge_edges` crossing the boundary.

    Communities are capped at `max_community_size` nodes.  Oversized
    components are subdivided via seed expansion so the hierarchical
    loop in abstract_graph can build the next level naturally.

    Strategy:
    1. Build adjacency (ignoring 'contains' edges).
    2. Find connected components that are already isolated (0 bridge edges)
       and within the size limit — these are natural communities.
    3. Oversized or high-bridge-edge components are subdivided via seed
       expansion to find tightly-coupled sub-groups.
    4. Rank by modularity and greedily select non-overlapping communities.
    """
    adj = _build_adjacency(node_ids, edges)

    # Start with connected components
    components = _find_connected_components(node_ids, adj)

    candidates: list[set[str]] = []

    for comp in components:
        if len(comp) < min_community_size:
            continue

        bridge_count = _count_bridge_edges(comp, node_ids, edges)

        if bridge_count <= max_bridge_edges and len(comp) <= max_community_size:
            # This whole component is self-contained AND small enough
            candidates.append(comp)
        else:
            # Component is too large or has too many external edges.
            # Find tighter sub-communities within it.
            sub_candidates = _seed_expand_communities(
                comp, node_ids, edges, adj,
                max_bridge_edges, min_community_size, max_community_size,
            )
            candidates.extend(sub_candidates)

    # Rank by modularity, then greedily select non-overlapping
    def modularity(community: set[str]) -> float:
        internal = _count_internal_edges(community, edges)
        bridge = _count_bridge_edges(community, node_ids, edges)
        total = internal + bridge
        if total == 0:
            return 0.0
        return internal / total

    # Prefer high modularity, then larger size
    candidates.sort(key=lambda c: (-modularity(c), -len(c)))

    selected: list[set[str]] = []
    used: set[str] = set()

    for community in candidates:
        if community & used:
            continue
        if len(community) < min_community_size:
            continue
        bridge = _count_bridge_edges(community, node_ids, edges)
        if bridge <= max_bridge_edges:
            selected.append(community)
            used |= community

    return selected


def _seed_expand_communities(
    component: set[str],
    all_node_ids: set[str],
    edges: list[Edge],
    adj: dict[str, set[str]],
    max_bridge_edges: int,
    min_community_size: int,
    max_community_size: int = 100,
) -> list[set[str]]:
    """
    Within a large component, find sub-communities by seed expansion.

    For each unused node, greedily grow a cluster by adding the neighbor
    that keeps bridge edges within the limit, preferring the one that
    maximises the internal-to-bridge ratio.  Growth stops at
    `max_community_size`.

    Uses the pre-computed adjacency map for fast incremental bridge/internal
    counting (O(degree) per candidate instead of O(|edges|)).
    """
    # Sort nodes by internal degree (ascending) — low-degree nodes are better seeds
    nodes_by_degree = sorted(
        component, key=lambda n: len(adj.get(n, set()) & component)
    )

    found: list[set[str]] = []
    used_in_community: set[str] = set()

    for seed in nodes_by_degree:
        if seed in used_in_community:
            continue

        community: set[str] = {seed}

        # Maintain running bridge count:  an edge (u,v) with u in community
        # and v in all_node_ids but NOT in community is a bridge.
        bridge_count = 0
        for nb in adj.get(seed, set()):
            if nb in all_node_ids:
                bridge_count += 1

        changed = True
        while changed:
            changed = False

            if len(community) >= max_community_size:
                break

            # Frontier: neighbours of the community in the component
            frontier: set[str] = set()
            for nid in community:
                for nb in adj.get(nid, set()):
                    if nb in component and nb not in community and nb not in used_in_community:
                        frontier.add(nb)

            if not frontier:
                break

            best_node = None
            best_score = -1
            best_bridge_delta = 0

            for candidate in frontier:
                # Incremental bridge delta: adding `candidate` to the community
                #   – each edge from candidate to a community member was a bridge,
                #     now becomes internal  →  bridge_count decreases
                #   – each edge from candidate to a non-community node in the
                #     graph is a new bridge  →  bridge_count increases
                links_inside = 0
                links_outside = 0
                for nb in adj.get(candidate, set()):
                    if nb in community:
                        links_inside += 1
                    elif nb in all_node_ids:
                        links_outside += 1

                # Net change: new bridges – removed bridges
                delta = links_outside - links_inside
                trial_bridge = bridge_count + delta

                if trial_bridge > max_bridge_edges:
                    continue

                # Score: favour candidates that add many internal edges
                score = links_inside - trial_bridge * 2
                if score > best_score:
                    best_score = score
                    best_node = candidate
                    best_bridge_delta = delta

            if best_node is not None:
                community.add(best_node)
                bridge_count += best_bridge_delta
                changed = True

        if len(community) >= min_community_size and bridge_count <= max_bridge_edges:
            found.append(community)
            used_in_community |= community

    return found


def _safe_common_dir(file_paths: set[str]) -> str:
    """Safely compute a common directory label from file paths."""
    paths = [p for p in file_paths if p]
    if not paths:
        return ""
    try:
        if len(paths) == 1:
            return os.path.dirname(paths[0])
        return os.path.commonpath(paths)
    except (ValueError, TypeError):
        # commonpath fails on Windows with mixed drives, or other edge cases
        return os.path.dirname(paths[0]) if paths else ""


def _collapse_communities(
    graph: Graph,
    communities: list[set[str]],
    depth: int,
) -> Graph:
    """
    Collapse each community into a single island chain node.
    Returns a new graph with communities replaced by island chain nodes.
    Tracks collapsed nodes/edges as intermediates for multi-depth drill-down.
    """
    node_by_id: dict[str, Node] = {n.id: n for n in graph.nodes}

    # Map: original node ID → island chain ID (if it's being collapsed)
    id_to_meta: dict[str, str] = {}
    collapsed_ids: set[str] = set()
    meta_nodes: list[Node] = []

    # Also collect method IDs that belong to collapsed classes
    class_to_methods: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS:
            class_to_methods[e.source].add(e.target)

    # Intermediate data: save collapsed nodes and their internal edges
    new_intermediate_nodes: list[Node] = []
    new_intermediate_edges: list[Edge] = []

    for community in communities:
        # Generate a stable ID from sorted member IDs
        sorted_ids = sorted(community)
        meta_id = _make_id("__meta__", f"community::{'|'.join(sorted_ids)}")

        member_nodes = [node_by_id[nid] for nid in sorted_ids if nid in node_by_id]
        member_class_names = sorted(n.name for n in member_nodes if n.kind != NodeKind.METHOD)
        member_method_names = sorted(n.name for n in member_nodes if n.kind == NodeKind.METHOD)
        member_names = member_class_names + member_method_names

        # Derive a meaningful name from common path or member names
        file_paths = set(n.file_path for n in member_nodes if n.file_path)
        common_dir = _safe_common_dir(file_paths)
        if common_dir:
            group_label = Path(common_dir).name or common_dir
        else:
            group_label = member_names[0] if member_names else "group"

        # Determine naming based on member kinds
        member_kinds = {
            node_by_id[nid].kind for nid in sorted_ids if nid in node_by_id
        }
        all_group_or_meta = all(
            node_by_id[nid].kind in _GROUPABLE_META_KINDS
            for nid in sorted_ids if nid in node_by_id
        )

        if len(member_kinds) == 1:
            single_kind = next(iter(member_kinds))
            if single_kind in _GROUP_KIND_LABEL:
                kind_label = _GROUP_KIND_LABEL[single_kind]
                name = f"All {kind_label} ({len(member_names)} groups)"
                docstring = f"Hierarchical grouping of {len(member_names)} {kind_label.lower()} sub-groups"
            elif single_kind == NodeKind.ISLAND_CHAIN:
                name = f"{group_label} ({len(member_names)} groups)"
                docstring = f"Community of {len(member_names)} tightly-coupled groups"
            else:
                name = f"{group_label} ({len(member_names)} classes)"
                docstring = f"Community of {len(member_names)} tightly-coupled nodes"
        elif all_group_or_meta:
            name = f"{group_label} ({len(member_names)} groups)"
            docstring = f"Community of {len(member_names)} tightly-coupled groups"
        else:
            name = f"{group_label} ({len(member_names)} classes)"
            docstring = f"Community of {len(member_names)} tightly-coupled nodes"

        meta_node = Node(
            id=meta_id,
            kind=NodeKind.ISLAND_CHAIN,
            name=name,
            qualified_name=f"meta::community::{meta_id}",
            file_path=common_dir,
            line_start=0,
            line_end=0,
            members=member_class_names,
            member_methods=member_method_names,
            member_ids=sorted_ids,
            docstring=docstring,
            depth=depth,
        )
        meta_nodes.append(meta_node)

        # Save collapsed nodes as intermediates for drill-down
        for nid in sorted_ids:
            if nid in node_by_id:
                new_intermediate_nodes.append(node_by_id[nid])

        # Save internal edges (both endpoints in same community) as intermediates
        for e in graph.edges:
            if e.kind == EdgeKind.CONTAINS:
                continue
            if e.source in community and e.target in community:
                new_intermediate_edges.append(e)

        for nid in community:
            collapsed_ids.add(nid)
            id_to_meta[nid] = meta_id
            # Also collapse methods belonging to collapsed classes
            for mid in class_to_methods.get(nid, set()):
                collapsed_ids.add(mid)
                id_to_meta[mid] = meta_id

    # Build new graph
    out = Graph()

    # Keep non-collapsed nodes
    for n in graph.nodes:
        if n.id not in collapsed_ids:
            out.add_node(n)

    # Add island chain nodes
    for mn in meta_nodes:
        out.add_node(mn)

    # Rewire edges
    for e in graph.edges:
        # Skip internal contains edges for collapsed classes
        if e.kind == EdgeKind.CONTAINS and (e.source in collapsed_ids or e.target in collapsed_ids):
            continue

        s = id_to_meta.get(e.source, e.source)
        t = id_to_meta.get(e.target, e.target)

        # Skip self-loops (both ends collapsed into same island chain)
        if s == t:
            continue

        # Skip if either end was collapsed but not remapped (shouldn't happen)
        if s in collapsed_ids or t in collapsed_ids:
            continue

        out.add_edge(Edge(source=s, target=t, kind=e.kind, label=e.label))

    # Carry forward existing intermediates + add newly collapsed
    out.intermediate_nodes = list(graph.intermediate_nodes) + new_intermediate_nodes
    out.intermediate_edges = list(graph.intermediate_edges) + new_intermediate_edges

    return out


def _is_test_node(node: Node) -> bool:
    """Check if a node is a test class based on name or file path."""
    if node.kind not in (NodeKind.CLASS, NodeKind.METHOD):
        return False
    name = node.name
    file_path = node.file_path.replace("\\", "/")
    file_name = os.path.basename(file_path)
    if name.startswith("Test") or name.endswith("Test") or name.endswith("Tests"):
        return True
    # Python conventions
    if file_name.startswith("test_") or file_name.endswith("_test.py") or file_name == "tests.py":
        return True
    # JS/TS conventions (.test.js, .spec.ts, etc.)
    if ".test." in file_name or ".spec." in file_name:
        return True
    # Test directories
    if ("/tests/" in file_path or "/test/" in file_path
            or "/__tests__/" in file_path
            or file_path.startswith("tests/") or file_path.startswith("test/")
            or file_path.startswith("__tests__/")):
        return True
    return False


def _find_loose_node_ids(graph: Graph) -> set[str]:
    """Find CLASS nodes that have zero non-contains edges.

    Only raw CLASS nodes are eligible — METACLASS nodes represent
    communities of connected nodes found by the graph algorithm and
    must never be treated as loose."""
    # Count non-contains edges per node
    edge_count: dict[str, int] = defaultdict(int)
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS:
            continue
        edge_count[e.source] += 1
        edge_count[e.target] += 1

    loose = set()
    for n in graph.nodes:
        if n.kind != NodeKind.CLASS:
            continue
        if edge_count.get(n.id, 0) == 0:
            loose.add(n.id)
    return loose


# ── Category maps ───────────────────────────────────────────────────

_CATEGORY_KIND: dict[str, NodeKind] = {
    "test":      NodeKind.GROUP,
    "model":     NodeKind.GROUP,
    "config":    NodeKind.GROUP,
    "handler":   NodeKind.GROUP,
    "utility":   NodeKind.GROUP,
    "exception": NodeKind.GROUP,
    "event":     NodeKind.GROUP,
}

_CATEGORY_LABEL: dict[str, str] = {
    "test":      "Tests",
    "model":     "Data Models",
    "config":    "Configuration",
    "handler":   "Services & Handlers",
    "utility":   "Utilities",
    "exception": "Exceptions & Warnings",
    "event":     "Events & Signals",
}

# Kinds that represent groups/island chains (eligible for higher-level grouping)
_GROUPABLE_META_KINDS: set[NodeKind] = {NodeKind.ISLAND_CHAIN, NodeKind.GROUP}

# Human-readable label for each group kind (used when naming higher-level groups)
_GROUP_KIND_LABEL: dict[NodeKind, str] = {
    NodeKind.GROUP:         "Groups",
    NodeKind.ISLAND_CHAIN:  "Groups",
}


def _classify_by_name(name: str, file_name: str, bases_lower: list[str]) -> Optional[str]:
    """
    Core name/path/base classifier — shared by both class and island chain paths.
    Returns a category key or None.

    Uses stem-based file matching so it works across Python, JS/TS and HTML.
    """
    name_lower = name.lower()
    file_stem = Path(file_name).stem if file_name else ""

    # ── EXCEPTION / ERROR / WARNING ────────────────────────────────
    if (any(name.endswith(s) for s in ("Error", "Exception", "Warning", "Fault",
                                        "Exc", "Failure", "Problem", "Issue"))
            or name.startswith("Invalid")
            or any(b in bases_lower for b in ("exception", "baseexception", "valueerror",
                                               "runtimeerror", "typeerror", "ioerror",
                                               "oserror", "keyerror", "indexerror",
                                               "attributeerror", "notimplementederror",
                                               "userwarning", "deprecationwarning",
                                               "warning"))
            or any("error" in b or "exception" in b or "warning" in b for b in bases_lower)
            or file_stem in ("exceptions", "errors", "warnings",
                             "faults", "exc", "failures")):
        return "exception"

    # ── EVENT / SIGNAL / COMMAND / MESSAGE ─────────────────────────
    if (any(name.endswith(s) for s in ("Event", "Signal", "Command", "Message",
                                        "Notification", "Trigger", "Hook",
                                        "Listener", "Observer", "Subscriber",
                                        "Publisher", "Emitter", "Dispatcher"))
            or file_stem in ("events", "signals", "commands",
                             "messages", "notifications", "hooks",
                             "listeners", "observers", "dispatchers")):
        return "event"

    # ── TEST ───────────────────────────────────────────────────────
    if (name.startswith("Test") or name.endswith("Test") or name.endswith("Tests")
            or name.endswith("Spec") or name.endswith("Suite")
            or any("testcase" in b or "testsuit" in b for b in bases_lower)
            or file_name.startswith("test_") or file_name.endswith("_test.py")
            or file_stem in ("tests", "spec", "conftest")
            or ".test" in file_name or ".spec" in file_name):
        return "test"

    # ── MODEL / SCHEMA / DATA ──────────────────────────────────────
    if (any(name.endswith(s) for s in ("Model", "Schema", "Entity", "Record",
                                        "DTO", "Dto", "Document", "Row",
                                        "Aggregate", "Dataclass", "Struct",
                                        "Payload", "Response", "Request",
                                        "Serializer", "Deserializer", "Codec"))
            or any(b in bases_lower for b in ("basemodel", "model", "declarativebase",
                                               "document", "base", "typeddict",
                                               "namedtuple"))
            or any("model" in b or "schema" in b for b in bases_lower)
            or file_stem in ("models", "schemas", "entities", "domain",
                             "records", "dto", "payloads",
                             "serializers", "types", "structures")):
        return "model"

    # ── CONFIG / SETTINGS ──────────────────────────────────────────
    if (any(name.endswith(s) for s in ("Config", "Settings", "Options", "Constants",
                                        "Configuration", "Env", "Environment",
                                        "Params", "Parameters", "Flags", "Feature"))
            or file_stem in ("config", "settings", "constants",
                             "configuration", "env", "params",
                             "flags", "features")
            or "config" in name_lower or "settings" in name_lower):
        return "config"

    # ── SERVICE / HANDLER / MANAGER / CONTROLLER ───────────────────
    if (any(name.endswith(s) for s in ("Service", "Handler", "Manager", "Controller",
                                        "View", "Router", "Processor", "Worker",
                                        "Repository", "Repo", "Gateway", "Client",
                                        "Adapter", "Facade", "Coordinator",
                                        "Orchestrator", "Task", "Job", "Action",
                                        "UseCase", "Interactor", "Builder",
                                        "Director", "Pipeline", "Component"))
            or file_stem in ("services", "handlers", "controllers",
                             "views", "routers", "managers",
                             "repositories", "gateways", "tasks",
                             "jobs", "actions", "usecases",
                             "pipelines", "builders", "components")):
        return "handler"

    # ── UTILITY / HELPER / MIXIN ───────────────────────────────────
    if (any(name.endswith(s) for s in ("Helper", "Utils", "Util", "Mixin", "Helpers",
                                        "Utilities", "Tools", "Common", "Decorator",
                                        "Decorators", "Middleware", "Guard", "Filter",
                                        "Validator", "Formatter", "Parser", "Converter",
                                        "Registry", "Cache", "Pool", "Buffer",
                                        "Proxy", "Wrapper", "Singleton"))
            or name.startswith("Base") or name.startswith("Abstract")
            or name.startswith("Mixin")
            or file_stem in ("utils", "helpers", "mixins", "common",
                             "base", "tools", "decorators", "middleware",
                             "guards", "validators", "formatters",
                             "parsers", "converters", "registry",
                             "cache", "pool", "proxy")):
        return "utility"

    return None


def _classify_loose_node(node: Node) -> Optional[str]:
    """
    Classify a loose CLASS node into a category using naming / path /
    base-class conventions.  Returns a category key or None.
    """
    file_path = node.file_path.replace("\\", "/")
    # Check parent directory names for test detection
    if ("/tests/" in file_path or "/test/" in file_path
            or "/__tests__/" in file_path
            or file_path.startswith("tests/") or file_path.startswith("test/")
            or file_path.startswith("__tests__/")):
        return "test"

    file_name = os.path.basename(file_path).lower()
    bases_lower = [b.lower() for b in (node.bases or [])]
    return _classify_by_name(node.name, file_name, bases_lower)



def _collapse_loose_nodes_heuristic(graph: Graph) -> Graph:
    """Legacy wrapper — delegates to the broader all-node heuristic."""
    return _collapse_all_by_heuristic(graph)


def _collapse_all_by_heuristic(graph: Graph, min_group_size: int = 2) -> Graph:
    """
    Classify **all** CLASS nodes by naming / path / base-class heuristics and
    group them.  Within each category, nodes are sub-grouped by source file so
    that groups stay cohesive and navigable.  Unclassified classes that share a
    source file with at least *min_group_size* peers are grouped by file.

    Unlike the old loose-node-only approach, this works on every CLASS node
    and every loose METHOD node (top-level functions not belonging to a class)
    so the abstract view gets meaningful clusters even when the raw graph is
    heavily interconnected.
    """
    node_by_id = {n.id: n for n in graph.nodes}

    # Identify loose METHOD nodes: those NOT targeted by any CONTAINS edge
    contained_ids: set[str] = set()
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS:
            contained_ids.add(e.target)

    # ── Step 1: classify every CLASS node and loose METHOD node ──────
    classified: dict[str, str] = {}       # node_id → category key
    unclassified_ids: list[str] = []

    for n in graph.nodes:
        # Include CLASS nodes and loose METHOD nodes
        if n.kind == NodeKind.CLASS:
            pass  # always eligible
        elif n.kind == NodeKind.METHOD and n.id not in contained_ids:
            pass  # loose function — eligible
        else:
            continue
        cat = _classify_loose_node(n)
        if cat:
            classified[n.id] = cat
        else:
            unclassified_ids.append(n.id)

    # ── Step 2: sub-group classified nodes by (category, file) ──────
    cat_file_buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid, cat in classified.items():
        fp = node_by_id[nid].file_path
        cat_file_buckets[(cat, fp)].append(nid)

    # Merge undersized (< min_group_size) same-category buckets together
    cat_to_buckets: dict[str, list[list[str]]] = defaultdict(list)
    for (cat, _fp), ids in cat_file_buckets.items():
        cat_to_buckets[cat].append(ids)

    final_groups: list[tuple[str, str, list[str]]] = []   # (category, label_suffix, ids)
    for cat, buckets in cat_to_buckets.items():
        buckets.sort(key=len, reverse=True)
        big: list[tuple[str, list[str]]] = []
        remainder: list[str] = []
        for b in buckets:
            if len(b) >= min_group_size:
                # Derive a short label from the file name
                fp = node_by_id[b[0]].file_path
                stem = Path(fp).stem if fp else ""
                big.append((stem, b))
            else:
                remainder.extend(b)

        # Fold remainder into the largest bucket, or form its own if big enough
        if remainder:
            if big:
                stem, ids = big[0]
                big[0] = (stem, ids + remainder)
            elif len(remainder) >= min_group_size:
                fp = node_by_id[remainder[0]].file_path
                stem = Path(fp).stem if fp else ""
                big.append((stem, remainder))
            # else: remainder too small and no existing bucket → leave standalone

        for stem, ids in big:
            final_groups.append((cat, stem, ids))

    # ── Step 3: group remaining unclassified nodes by file ──────────
    file_buckets: dict[str, list[str]] = defaultdict(list)
    grouped_ids = {nid for (_, _, ids) in final_groups for nid in ids}
    for nid in unclassified_ids:
        if nid not in grouped_ids:
            fp = node_by_id[nid].file_path
            file_buckets[fp].append(nid)

    for fp, ids in file_buckets.items():
        if len(ids) >= min_group_size:
            stem = Path(fp).stem if fp else "misc"
            final_groups.append(("file", stem, ids))

    if not final_groups:
        return graph

    # ── Step 4: build group nodes and remap edges ───────────────────
    class_to_methods: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS:
            class_to_methods[e.source].add(e.target)

    all_collapsed: set[str] = set()
    id_remap: dict[str, str] = {}
    new_nodes: list[Node] = []

    for category, stem, node_ids in final_groups:
        if not node_ids:
            continue

        sorted_ids = sorted(node_ids)
        meta_id = _make_id("__heuristic__", f"{category}::{stem}::{'|'.join(sorted_ids)}")

        member_nodes = [node_by_id[nid] for nid in sorted_ids if nid in node_by_id]
        member_class_names = sorted(n.name for n in member_nodes if n.kind != NodeKind.METHOD)
        member_method_names = sorted(n.name for n in member_nodes if n.kind == NodeKind.METHOD)
        all_member_names = member_class_names + member_method_names
        file_paths = set(n.file_path for n in member_nodes if n.file_path)
        common_dir = _safe_common_dir(file_paths)

        if category == "file":
            kind = NodeKind.ISLAND_CHAIN
            label = stem or "module"
            name = f"{label} ({len(all_member_names)})"
            docstring = f"Nodes from {stem}.py grouped by co-location"
        else:
            kind = _CATEGORY_KIND[category]
            base_label = _CATEGORY_LABEL[category]
            # Include file stem when multiple files contribute to a category
            same_cat_groups = [(c, s, i) for (c, s, i) in final_groups if c == category]
            if len(same_cat_groups) > 1:
                name = f"{base_label} — {stem} ({len(all_member_names)})"
            else:
                name = f"{base_label} ({len(all_member_names)})"
            docstring = f"Heuristically grouped by naming conventions: {base_label}"

        group_node = Node(
            id=meta_id,
            kind=kind,
            name=name,
            qualified_name=f"meta::heuristic::{category}::{meta_id}",
            file_path=common_dir,
            line_start=0,
            line_end=0,
            members=member_class_names,
            member_methods=member_method_names,
            member_ids=sorted_ids,
            docstring=docstring,
            depth=0,
        )
        new_nodes.append(group_node)

        for nid in sorted_ids:
            all_collapsed.add(nid)
            id_remap[nid] = meta_id
            for mid in class_to_methods.get(nid, set()):
                all_collapsed.add(mid)
                id_remap[mid] = meta_id

    # ── Step 5: build output graph ──────────────────────────────────
    out = Graph()

    for n in graph.nodes:
        if n.id not in all_collapsed:
            out.add_node(n)

    for gn in new_nodes:
        out.add_node(gn)

    seen_edges: set[tuple[str, str, str]] = set()
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS and (e.source in all_collapsed or e.target in all_collapsed):
            continue

        s = id_remap.get(e.source, e.source)
        t = id_remap.get(e.target, e.target)

        if s == t:
            continue
        if s in all_collapsed or t in all_collapsed:
            continue

        edge_key = (s, t, e.kind.value)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        out.add_edge(Edge(source=s, target=t, kind=e.kind, label=e.label))

    # Carry forward existing intermediates + save collapsed nodes/edges
    out.intermediate_nodes = list(graph.intermediate_nodes)
    out.intermediate_edges = list(graph.intermediate_edges)
    for n in graph.nodes:
        if n.id in all_collapsed:
            out.intermediate_nodes.append(n)
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS:
            continue
        if e.source in all_collapsed and e.target in all_collapsed:
            # Both endpoints collapsed into the same or different groups
            if id_remap.get(e.source) == id_remap.get(e.target):
                out.intermediate_edges.append(e)

    return out



def _find_metanode_groups(
    graph: Graph,
    min_group_size: int = 3,
) -> list[set[str]]:
    """
    Find groups of metanodes / group nodes that can be collapsed into
    higher-level island chain nodes.

    Grouping strategies (applied in priority order):
    1. Same kind  (e.g. all group → "All Groups")
    2. Same parent directory for remaining ungrouped metanodes

    After each strategy, groups are split into their connected sub-components
    so that nodes which share no edge path are never grouped together.
    """
    groupable = [n for n in graph.nodes if n.kind in _GROUPABLE_META_KINDS]
    if len(groupable) < min_group_size:
        return []

    # Pre-build adjacency for connectivity checks
    groupable_ids = {n.id for n in groupable}
    adj = _build_adjacency(groupable_ids, graph.edges)

    def _split_into_connected(candidate: set[str]) -> list[set[str]]:
        """Split a candidate group into its connected sub-components."""
        sub_adj = {nid: adj.get(nid, set()) & candidate for nid in candidate}
        return _find_connected_components(candidate, sub_adj)

    groups: list[set[str]] = []
    already_grouped: set[str] = set()

    # ── Strategy 1: group by same kind ──────────────────────────────
    kind_buckets: dict[NodeKind, list[str]] = defaultdict(list)
    for n in groupable:
        kind_buckets[n.kind].append(n.id)

    for kind, ids in sorted(kind_buckets.items(), key=lambda x: -len(x[1])):
        available = [i for i in ids if i not in already_grouped]
        if len(available) >= min_group_size:
            # Split into connected sub-components
            for comp in _split_into_connected(set(available)):
                if len(comp) >= min_group_size:
                    groups.append(comp)
                    already_grouped.update(comp)

    # ── Strategy 2: group remaining by common parent directory ──────
    dir_buckets: dict[str, list[str]] = defaultdict(list)
    for n in groupable:
        if n.id not in already_grouped:
            parent_dir = os.path.dirname(n.file_path) if n.file_path else ""
            dir_buckets[parent_dir].append(n.id)

    for dir_path, ids in sorted(dir_buckets.items(), key=lambda x: -len(x[1])):
        if len(ids) >= min_group_size:
            # Split into connected sub-components
            for comp in _split_into_connected(set(ids)):
                if len(comp) >= min_group_size:
                    groups.append(comp)
                    already_grouped.update(comp)

    return groups


def _apply_min_top_nodes_filter(
    groups: list[set[str]],
    all_node_ids: set[str],
    min_top_nodes: int,
) -> list[set[str]]:
    """Drop smallest groups until collapsing won't leave fewer than min_top_nodes."""
    if not groups:
        return groups

    nodes_being_collapsed: set[str] = set()
    for g in groups:
        nodes_being_collapsed |= g

    remaining = all_node_ids - nodes_being_collapsed
    new_top_count = len(remaining) + len(groups)

    if new_top_count >= min_top_nodes:
        return groups

    # Sort largest first, drop from the tail
    groups = sorted(groups, key=len, reverse=True)
    while groups and new_top_count < min_top_nodes:
        dropped = groups.pop()
        nodes_being_collapsed -= dropped
        remaining = all_node_ids - nodes_being_collapsed
        new_top_count = len(remaining) + len(groups)

    return groups


def abstract_graph(
    graph: Graph,
    max_bridge_edges: int = 2,
    min_community_size: int = 3,
    max_community_size: int = 100,
    min_top_nodes: int = 3,
    max_depth: int = 10,
) -> Graph:
    """
    Recursively detect tightly-coupled communities and collapse them into
    island chain nodes.  ALL class nodes are first grouped by naming / path /
    base-class heuristics (sub-grouped by source file for cohesion).

    Then a multi-pass loop alternates between:
      A) graph-based community detection (edge connectivity)
      B) heuristic meta-grouping (same kind / same directory)

    This enables multiple depth levels: metanodes can themselves become
    members of higher-level metanodes.

    Communities are capped at `max_community_size` nodes so the overview
    stays navigable.  Larger clusters are subdivided and the hierarchical
    loop naturally builds deeper levels of island chains.
    """
    # First, group all classes by naming / file heuristics
    current = _collapse_all_by_heuristic(graph)

    # Node kinds eligible for further community-based collapsing
    _GROUPABLE_KINDS = {NodeKind.CLASS, NodeKind.ISLAND_CHAIN, NodeKind.GROUP}

    depth = 0

    while depth < max_depth:
        made_progress = False

        # ── Strategy A: edge-based community detection ──────────────
        candidate_ids = {
            n.id for n in current.nodes
            if n.kind in _GROUPABLE_KINDS
        }

        if len(candidate_ids) >= min_community_size:
            communities = _find_communities(
                candidate_ids,
                current.edges,
                max_bridge_edges=max_bridge_edges,
                min_community_size=min_community_size,
                max_community_size=max_community_size,
            )

            communities = _apply_min_top_nodes_filter(
                communities, candidate_ids, min_top_nodes,
            )

            if communities:
                depth += 1
                current = _collapse_communities(current, communities, depth)
                made_progress = True
                continue  # re-enter loop — new communities may have formed

        # ── Strategy B: heuristic meta-grouping ─────────────────────
        meta_groups = _find_metanode_groups(current, min_community_size)

        all_node_ids = {n.id for n in current.nodes}
        meta_groups = _apply_min_top_nodes_filter(
            meta_groups, all_node_ids, min_top_nodes,
        )

        if meta_groups:
            depth += 1
            current = _collapse_communities(current, meta_groups, depth)
            made_progress = True

        if not made_progress:
            break

    return current


def analyze_to_json(root_dir: str) -> str:
    """Analyze a codebase and return the graph as a JSON string."""
    graph = analyze_codebase(root_dir)
    return json.dumps(graph.to_dict(), indent=2)


# ── CLI entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "mock_codebase"
    print(analyze_to_json(target))

