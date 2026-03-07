"""
Static analysis engine — parses Python source files and extracts
classes, functions, and their relationships into a graph structure.
"""

import ast
import os
import json
import hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from collections import defaultdict


# ── Graph data model ────────────────────────────────────────────────

class NodeKind(str, Enum):
    CLASS = "class"
    METHOD = "method"
    METACLASS = "metaclass"


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
    members: list[str] = field(default_factory=list)      # for metaclass nodes
    member_ids: list[str] = field(default_factory=list)   # for metaclass drilldown
    bases: list[str] = field(default_factory=list)
    docstring: Optional[str] = None


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
        return {
            "nodes": [_node_to_dict(n) for n in self.nodes],
            "edges": [_edge_to_dict(e) for e in self.edges],
        }


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

class _ClassCollector(ast.NodeVisitor):
    """First pass: collect all class and function definitions."""

    def __init__(self, file_path: str, module_qname: str):
        self.file_path = file_path
        self.module_qname = module_qname
        self.classes: dict[str, Node] = {}
        self.functions: dict[str, Node] = {}
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
        # Only collect methods inside a class — standalone functions are excluded from the graph
        if not self._current_class:
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
        self.imports: dict[str, str] = {}           # alias → qualified name
        self.import_sources: dict[str, str] = {}    # alias → source module
        self.calls: list[tuple[str, str, int]] = [] # (caller_context, callee_name, lineno)
        self.type_refs: list[tuple[str, str]] = []  # (context, type_name)
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

        # Collect type annotations from parameters
        for arg in node.args.args:
            if arg.annotation:
                type_name = self._extract_name(arg.annotation)
                if type_name and type_name in self.known_classes:
                    self.type_refs.append((self._context, type_name))

        # Return annotation
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

    @staticmethod
    def _extract_name(node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None


# ── Analysis engine ─────────────────────────────────────────────────

def _module_qname(file_path: str, root: str) -> str:
    """Convert file path to a dotted module name."""
    rel = os.path.relpath(file_path, root)
    parts = Path(rel).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def analyze_codebase(root_dir: str) -> Graph:
    """
    Walk a Python codebase directory, parse every .py file,
    and return a Graph of classes/methods and their relationships.
    """
    root_dir = os.path.abspath(root_dir)
    graph = Graph()

    # Phase 1: Collect all classes and methods across all files
    all_classes: dict[str, Node] = {}       # class_name → Node
    all_functions: dict[str, Node] = {}     # qualified_name → Node
    file_collectors: list[tuple[str, str, _ClassCollector]] = []

    py_files = sorted(Path(root_dir).rglob("*.py"))

    for py_file in py_files:
        file_path = str(py_file)
        rel_path = os.path.relpath(file_path, root_dir)

        # Skip __pycache__, .venv, etc.
        if any(part.startswith(".") or part == "__pycache__" for part in Path(rel_path).parts):
            continue

        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            continue

        mod_qname = _module_qname(file_path, root_dir)

        # Collect definitions
        collector = _ClassCollector(rel_path, mod_qname)
        collector.visit(tree)
        file_collectors.append((file_path, rel_path, collector))

        for cls_name, cls_node in collector.classes.items():
            all_classes[cls_name] = cls_node
            graph.add_node(cls_node)

        for fn_qname, fn_node in collector.functions.items():
            all_functions[fn_qname] = fn_node
            graph.add_node(fn_node)

            # CONTAINS edge: class → method
            parts = fn_qname.split(".")
            parent_class_name = parts[-2]
            if parent_class_name in all_classes:
                graph.add_edge(Edge(
                    source=all_classes[parent_class_name].id,
                    target=fn_node.id,
                    kind=EdgeKind.CONTAINS,
                    label="has method",
                ))

    known_class_names = set(all_classes.keys())

    # quick lookup: class_name -> {method_name: method_node}
    methods_by_class: dict[str, dict[str, Node]] = defaultdict(dict)
    for fn in all_functions.values():
        parts = fn.qualified_name.split(".")
        if len(parts) >= 2:
            cls_name = parts[-2]
            methods_by_class[cls_name][fn.name] = fn

    # Phase 2: Extract relationships
    for file_path, rel_path, class_collector in file_collectors:
        source = Path(file_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_path)
        mod_qname = _module_qname(file_path, root_dir)

        rel_collector = _RelationshipCollector(rel_path, mod_qname, known_class_names)
        rel_collector.visit(tree)

        # Inheritance edges
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

        # Call / instantiation edges
        for context, callee_name, lineno in rel_collector.calls:
            # Method -> method calls (within known class scope)
            if context != "__module__" and "." in context:
                caller_class_name, _caller_method_name = context.split(".", 1)
                caller_qname = f"{mod_qname}.{context}"
                caller_fn = all_functions.get(caller_qname)
                callee_fn = methods_by_class.get(caller_class_name, {}).get(callee_name)
                if caller_fn and callee_fn:
                    graph.add_edge(Edge(
                        source=caller_fn.id,
                        target=callee_fn.id,
                        kind=EdgeKind.CALLS,
                        label=f"calls {callee_name}",
                    ))

            # Class -> class instantiation
            if callee_name in all_classes:
                caller_class_name = context.split(".")[0] if context != "__module__" else None
                if caller_class_name and caller_class_name in all_classes:
                    graph.add_edge(Edge(
                        source=all_classes[caller_class_name].id,
                        target=all_classes[callee_name].id,
                        kind=EdgeKind.INSTANTIATES,
                        label=f"creates {callee_name}",
                    ))

        # Type reference edges (parameter types = dependency)
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

    return graph


def abstract_graph(graph: Graph, threshold: int = 8) -> Graph:
    """Collapse large class groups (by directory) into metaclass nodes."""
    if threshold < 2:
        return graph

    class_nodes = [n for n in graph.nodes if n.kind == NodeKind.CLASS]
    if not class_nodes:
        return graph

    groups: dict[str, list[Node]] = defaultdict(list)
    for cls in class_nodes:
        group_key = os.path.dirname(cls.file_path) or "."
        groups[group_key].append(cls)

    target_groups = {k: v for k, v in groups.items() if len(v) >= threshold}
    if not target_groups:
        return graph

    class_to_methods: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS:
            class_to_methods[e.source].add(e.target)

    grouped_class_ids: set[str] = set()
    grouped_method_ids: set[str] = set()
    id_to_meta: dict[str, str] = {}
    meta_nodes: list[Node] = []

    for group_key, members in target_groups.items():
        meta_id = _make_id("__meta__", f"metaclass::{group_key}")
        class_names = sorted(n.name for n in members)
        class_ids = sorted(n.id for n in members)
        meta_nodes.append(Node(
            id=meta_id,
            kind=NodeKind.METACLASS,
            name=f"{Path(group_key).name or group_key} (meta)",
            qualified_name=f"meta::{group_key}",
            file_path=group_key,
            line_start=0,
            line_end=0,
            members=class_names,
            member_ids=class_ids,
            docstring=f"Aggregates {len(class_names)} classes",
        ))

        for cls in members:
            grouped_class_ids.add(cls.id)
            id_to_meta[cls.id] = meta_id
            for mid in class_to_methods.get(cls.id, set()):
                grouped_method_ids.add(mid)
                id_to_meta[mid] = meta_id

    removed_ids = grouped_class_ids | grouped_method_ids
    meta_ids = {n.id for n in meta_nodes}
    out = Graph()

    for n in graph.nodes:
        if n.id not in removed_ids:
            out.add_node(n)
    for mn in meta_nodes:
        out.add_node(mn)

    for e in graph.edges:
        if e.kind == EdgeKind.CONTAINS and (e.source in grouped_class_ids or e.target in grouped_method_ids):
            continue

        s = id_to_meta.get(e.source, e.source)
        t = id_to_meta.get(e.target, e.target)

        if s == t:
            continue
        if s in removed_ids or t in removed_ids:
            continue
        if s in meta_ids and t in meta_ids and s == t:
            continue

        out.add_edge(Edge(source=s, target=t, kind=e.kind, label=e.label))

    return out


def analyze_to_json(root_dir: str) -> str:
    """Analyze a codebase and return the graph as a JSON string."""
    graph = analyze_codebase(root_dir)
    return json.dumps(graph.to_dict(), indent=2)


# ── CLI entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "mock_codebase"
    print(analyze_to_json(target))

