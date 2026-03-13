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


# ── Cache directory ─────────────────────────────────────────────────

_VCA_CACHE_DIR = Path(__file__).parent.parent / ".vca_cache"


def _ensure_cache_dir() -> Path:
    _VCA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _VCA_CACHE_DIR


def _source_fingerprint(root_dir: str) -> str:
    """Hash all .py file paths + contents under root_dir for cache invalidation."""
    h = hashlib.sha256()
    py_files = sorted(Path(root_dir).rglob("*.py"))
    for py_file in py_files:
        rel = os.path.relpath(str(py_file), root_dir)
        if any(part.startswith(".") or part == "__pycache__" for part in Path(rel).parts):
            continue
        h.update(rel.encode())
        try:
            h.update(py_file.read_bytes())
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
    METACLASS = "metaclass"
    TEST_GROUP = "test_group"
    MODEL_GROUP = "model_group"
    CONFIG_GROUP = "config_group"
    HANDLER_GROUP = "handler_group"
    UTILITY_GROUP = "utility_group"
    EXCEPTION_GROUP = "exception_group"
    EVENT_GROUP = "event_group"


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
        return d


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


# ── Analysis engine ─────────────────────────────────────────────────

def _module_qname(file_path: str, root: str) -> str:
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

    all_classes: dict[str, Node] = {}
    all_functions: dict[str, Node] = {}
    file_collectors: list[tuple[str, str, _ClassCollector]] = []

    py_files = sorted(Path(root_dir).rglob("*.py"))

    for py_file in py_files:
        file_path = str(py_file)
        rel_path = os.path.relpath(file_path, root_dir)

        if any(part.startswith(".") or part == "__pycache__" for part in Path(rel_path).parts):
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

    known_class_names = set(all_classes.keys())

    methods_by_class: dict[str, dict[str, Node]] = defaultdict(dict)
    for fn in all_functions.values():
        parts = fn.qualified_name.split(".")
        if len(parts) >= 2:
            cls_name = parts[-2]
            methods_by_class[cls_name][fn.name] = fn

    # Save (class_collector, rel_collector) pairs for the import-edge pass
    file_rel_data: list[tuple[_ClassCollector, _RelationshipCollector]] = []

    for file_path, rel_path, class_collector in file_collectors:
        source = Path(file_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_path)
        mod_qname = _module_qname(file_path, root_dir)

        rel_collector = _RelationshipCollector(rel_path, mod_qname, known_class_names)
        rel_collector.visit(tree)
        file_rel_data.append((class_collector, rel_collector))

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

        for context, callee_name, lineno in rel_collector.calls:
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

            if callee_name in all_classes:
                caller_class_name = context.split(".")[0] if context != "__module__" else None
                if caller_class_name and caller_class_name in all_classes:
                    graph.add_edge(Edge(
                        source=all_classes[caller_class_name].id,
                        target=all_classes[callee_name].id,
                        kind=EdgeKind.INSTANTIATES,
                        label=f"creates {callee_name}",
                    ))

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

        # Class name references — catches attribute access (OrderStatus.CONFIRMED)
        # and bare name references that the call/type-annotation visitors miss.
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

    return graph


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
    Collapse each community into a single metaclass node.
    Returns a new graph with communities replaced by metaclass nodes.
    Tracks collapsed nodes/edges as intermediates for multi-depth drill-down.
    """
    node_by_id: dict[str, Node] = {n.id: n for n in graph.nodes}

    # Map: original node ID → metaclass ID (if it's being collapsed)
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
        member_names = sorted(n.name for n in member_nodes)

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
            elif single_kind == NodeKind.METACLASS:
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
            kind=NodeKind.METACLASS,
            name=name,
            qualified_name=f"meta::community::{meta_id}",
            file_path=common_dir,
            line_start=0,
            line_end=0,
            members=member_names,
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

    # Add metaclass nodes
    for mn in meta_nodes:
        out.add_node(mn)

    # Rewire edges
    for e in graph.edges:
        # Skip internal contains edges for collapsed classes
        if e.kind == EdgeKind.CONTAINS and (e.source in collapsed_ids or e.target in collapsed_ids):
            continue

        s = id_to_meta.get(e.source, e.source)
        t = id_to_meta.get(e.target, e.target)

        # Skip self-loops (both ends collapsed into same metaclass)
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
    if file_name.startswith("test_") or file_name.endswith("_test.py") or file_name == "tests.py":
        return True
    if "/tests/" in file_path or "/test/" in file_path or file_path.startswith("tests/") or file_path.startswith("test/"):
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
    "test":      NodeKind.TEST_GROUP,
    "model":     NodeKind.MODEL_GROUP,
    "config":    NodeKind.CONFIG_GROUP,
    "handler":   NodeKind.HANDLER_GROUP,
    "utility":   NodeKind.UTILITY_GROUP,
    "exception": NodeKind.EXCEPTION_GROUP,
    "event":     NodeKind.EVENT_GROUP,
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

# Kinds that represent groups/metaclasses (eligible for higher-level grouping)
_GROUPABLE_META_KINDS: set[NodeKind] = {NodeKind.METACLASS} | {
    k for k in NodeKind if k.value.endswith("_group")
}

# Human-readable label for each group kind (used when naming higher-level groups)
_GROUP_KIND_LABEL: dict[NodeKind, str] = {
    NodeKind.TEST_GROUP:      "Tests",
    NodeKind.MODEL_GROUP:     "Data Models",
    NodeKind.CONFIG_GROUP:    "Configuration",
    NodeKind.HANDLER_GROUP:   "Services & Handlers",
    NodeKind.UTILITY_GROUP:   "Utilities",
    NodeKind.EXCEPTION_GROUP: "Exceptions & Warnings",
    NodeKind.EVENT_GROUP:     "Events & Signals",
    NodeKind.METACLASS:       "Groups",
}


def _classify_by_name(name: str, file_name: str, bases_lower: list[str]) -> Optional[str]:
    """
    Core name/path/base classifier — shared by both class and metaclass paths.
    Returns a category key or None.
    """
    name_lower = name.lower()

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
            or file_name in ("exceptions.py", "errors.py", "warnings.py",
                             "faults.py", "exc.py", "failures.py")):
        return "exception"

    # ── EVENT / SIGNAL / COMMAND / MESSAGE ─────────────────────────
    if (any(name.endswith(s) for s in ("Event", "Signal", "Command", "Message",
                                        "Notification", "Trigger", "Hook",
                                        "Listener", "Observer", "Subscriber",
                                        "Publisher", "Emitter", "Dispatcher"))
            or file_name in ("events.py", "signals.py", "commands.py",
                             "messages.py", "notifications.py", "hooks.py",
                             "listeners.py", "observers.py", "dispatchers.py")):
        return "event"

    # ── TEST ───────────────────────────────────────────────────────
    if (name.startswith("Test") or name.endswith("Test") or name.endswith("Tests")
            or name.endswith("Spec") or name.endswith("Suite")
            or any("testcase" in b or "testsuit" in b for b in bases_lower)
            or file_name.startswith("test_") or file_name.endswith("_test.py")
            or file_name in ("tests.py", "spec.py", "conftest.py")):
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
            or file_name in ("models.py", "schemas.py", "entities.py", "domain.py",
                             "records.py", "dto.py", "payloads.py",
                             "serializers.py", "types.py", "structures.py")):
        return "model"

    # ── CONFIG / SETTINGS ──────────────────────────────────────────
    if (any(name.endswith(s) for s in ("Config", "Settings", "Options", "Constants",
                                        "Configuration", "Env", "Environment",
                                        "Params", "Parameters", "Flags", "Feature"))
            or file_name in ("config.py", "settings.py", "constants.py",
                             "configuration.py", "env.py", "params.py",
                             "flags.py", "features.py")
            or "config" in name_lower or "settings" in name_lower):
        return "config"

    # ── SERVICE / HANDLER / MANAGER / CONTROLLER ───────────────────
    if (any(name.endswith(s) for s in ("Service", "Handler", "Manager", "Controller",
                                        "View", "Router", "Processor", "Worker",
                                        "Repository", "Repo", "Gateway", "Client",
                                        "Adapter", "Facade", "Coordinator",
                                        "Orchestrator", "Task", "Job", "Action",
                                        "UseCase", "Interactor", "Builder",
                                        "Director", "Pipeline"))
            or file_name in ("services.py", "handlers.py", "controllers.py",
                             "views.py", "routers.py", "managers.py",
                             "repositories.py", "gateways.py", "tasks.py",
                             "jobs.py", "actions.py", "usecases.py",
                             "pipelines.py", "builders.py")):
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
            or file_name in ("utils.py", "helpers.py", "mixins.py", "common.py",
                             "base.py", "tools.py", "decorators.py", "middleware.py",
                             "guards.py", "validators.py", "formatters.py",
                             "parsers.py", "converters.py", "registry.py",
                             "cache.py", "pool.py", "proxy.py")):
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
            or file_path.startswith("tests/") or file_path.startswith("test/")):
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

    Unlike the old loose-node-only approach, this works on every CLASS node so
    the abstract view gets meaningful clusters even when the raw graph is
    heavily interconnected.
    """
    node_by_id = {n.id: n for n in graph.nodes}

    # ── Step 1: classify every CLASS node ────────────────────────────
    classified: dict[str, str] = {}       # node_id → category key
    unclassified_ids: list[str] = []

    for n in graph.nodes:
        if n.kind != NodeKind.CLASS:
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
        member_names = sorted(n.name for n in member_nodes)
        file_paths = set(n.file_path for n in member_nodes if n.file_path)
        common_dir = _safe_common_dir(file_paths)

        if category == "file":
            kind = NodeKind.METACLASS
            label = stem or "module"
            name = f"{label} ({len(member_names)} classes)"
            docstring = f"Classes from {stem}.py grouped by co-location"
        else:
            kind = _CATEGORY_KIND[category]
            base_label = _CATEGORY_LABEL[category]
            # Include file stem when multiple files contribute to a category
            same_cat_groups = [(c, s, i) for (c, s, i) in final_groups if c == category]
            if len(same_cat_groups) > 1:
                name = f"{base_label} — {stem} ({len(member_names)})"
            else:
                name = f"{base_label} ({len(member_names)})"
            docstring = f"Heuristically grouped by naming conventions: {base_label}"

        group_node = Node(
            id=meta_id,
            kind=kind,
            name=name,
            qualified_name=f"meta::heuristic::{category}::{meta_id}",
            file_path=common_dir,
            line_start=0,
            line_end=0,
            members=member_names,
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

    return out



def _find_metanode_groups(
    graph: Graph,
    min_group_size: int = 3,
) -> list[set[str]]:
    """
    Find groups of metanodes / *_group nodes that can be collapsed into
    higher-level metaclass nodes.

    Grouping strategies (applied in priority order):
    1. Same kind  (e.g. all test_group → "All Tests")
    2. Same parent directory for remaining ungrouped metanodes
    """
    groupable = [n for n in graph.nodes if n.kind in _GROUPABLE_META_KINDS]
    if len(groupable) < min_group_size:
        return []

    groups: list[set[str]] = []
    already_grouped: set[str] = set()

    # ── Strategy 1: group by same kind ──────────────────────────────
    kind_buckets: dict[NodeKind, list[str]] = defaultdict(list)
    for n in groupable:
        kind_buckets[n.kind].append(n.id)

    for kind, ids in sorted(kind_buckets.items(), key=lambda x: -len(x[1])):
        available = [i for i in ids if i not in already_grouped]
        if len(available) >= min_group_size:
            groups.append(set(available))
            already_grouped.update(available)

    # ── Strategy 2: group remaining by common parent directory ──────
    dir_buckets: dict[str, list[str]] = defaultdict(list)
    for n in groupable:
        if n.id not in already_grouped:
            parent_dir = os.path.dirname(n.file_path) if n.file_path else ""
            dir_buckets[parent_dir].append(n.id)

    for dir_path, ids in sorted(dir_buckets.items(), key=lambda x: -len(x[1])):
        if len(ids) >= min_group_size:
            groups.append(set(ids))
            already_grouped.update(ids)

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
    metaclass nodes.  ALL class nodes are first grouped by naming / path /
    base-class heuristics (sub-grouped by source file for cohesion).

    Then a multi-pass loop alternates between:
      A) graph-based community detection (edge connectivity)
      B) heuristic meta-grouping (same kind / same directory)

    This enables multiple depth levels: metanodes can themselves become
    members of higher-level metanodes.

    Communities are capped at `max_community_size` nodes so the overview
    stays navigable.  Larger clusters are subdivided and the hierarchical
    loop naturally builds deeper levels of metaclasses.
    """
    # First, group all classes by naming / file heuristics
    current = _collapse_all_by_heuristic(graph)

    # Node kinds eligible for further community-based collapsing
    _GROUPABLE_KINDS = {NodeKind.CLASS, NodeKind.METACLASS} | {
        k for k in NodeKind if k.value.endswith("_group")
    }

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

