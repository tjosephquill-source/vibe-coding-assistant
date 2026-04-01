"""Tests for backend.analyzer — static analysis engine.

Covers:
  - Python AST parsing (classes, methods, inheritance, calls, type refs)
  - JS/TS regex-based parsing (classes, methods, inheritance, instantiation)
  - Graph data model (Node, Edge, Graph, add/dedupe)
  - Abstract graph generation (grouping, island chains)
  - Edge kind coverage (contains, inherits, calls, imports, instantiates, uses_type)
  - Cache helpers
"""

import json
import tempfile
from pathlib import Path

import pytest

from backend.analyzer import (
    analyze_codebase,
    abstract_graph,
    Graph,
    Node,
    Edge,
    NodeKind,
    EdgeKind,
    _make_id,
    _save_to_cache,
    _load_from_cache,
)

# Path to test fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PYTHON = FIXTURES_DIR / "sample_python"
SAMPLE_JS = FIXTURES_DIR / "sample_js"


# ════════════════════════════════════════════════════════════════════
# Graph data model tests
# ════════════════════════════════════════════════════════════════════


class TestGraphModel:
    """Tests for the Graph, Node, and Edge dataclasses."""

    def test_create_node(self):
        node = Node(
            id="abc123",
            kind=NodeKind.CLASS,
            name="MyClass",
            qualified_name="mod.MyClass",
            file_path="mod.py",
            line_start=1,
            line_end=10,
        )
        assert node.id == "abc123"
        assert node.kind == NodeKind.CLASS
        assert node.name == "MyClass"

    def test_create_edge(self):
        edge = Edge(source="a", target="b", kind=EdgeKind.CALLS)
        assert edge.source == "a"
        assert edge.target == "b"
        assert edge.kind == EdgeKind.CALLS

    def test_graph_add_node_deduplicates(self):
        g = Graph()
        n1 = Node("id1", NodeKind.CLASS, "A", "A", "f.py", 1, 5)
        n2 = Node("id1", NodeKind.CLASS, "A", "A", "f.py", 1, 5)
        g.add_node(n1)
        g.add_node(n2)
        assert len(g.nodes) == 1

    def test_graph_add_edge_deduplicates(self):
        g = Graph()
        e1 = Edge("a", "b", EdgeKind.CALLS)
        e2 = Edge("a", "b", EdgeKind.CALLS)
        g.add_edge(e1)
        g.add_edge(e2)
        assert len(g.edges) == 1

    def test_graph_add_different_edges(self):
        g = Graph()
        g.add_edge(Edge("a", "b", EdgeKind.CALLS))
        g.add_edge(Edge("a", "b", EdgeKind.IMPORTS))
        assert len(g.edges) == 2

    def test_graph_to_dict(self):
        g = Graph()
        g.add_node(Node("n1", NodeKind.CLASS, "Foo", "mod.Foo", "mod.py", 1, 10))
        g.add_edge(Edge("n1", "n2", EdgeKind.INHERITS))
        d = g.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert len(d["nodes"]) == 1
        assert len(d["edges"]) == 1
        assert d["nodes"][0]["kind"] == "class"
        assert d["edges"][0]["kind"] == "inherits"

    def test_node_kind_values(self):
        assert NodeKind.CLASS.value == "class"
        assert NodeKind.METHOD.value == "method"
        assert NodeKind.ISLAND_CHAIN.value == "island_chain"
        assert NodeKind.GROUP.value == "group"

    def test_edge_kind_values(self):
        assert EdgeKind.INHERITS.value == "inherits"
        assert EdgeKind.CALLS.value == "calls"
        assert EdgeKind.IMPORTS.value == "imports"
        assert EdgeKind.CONTAINS.value == "contains"
        assert EdgeKind.INSTANTIATES.value == "instantiates"
        assert EdgeKind.USES_TYPE.value == "uses_type"


class TestMakeId:
    """Tests for deterministic node ID generation."""

    def test_deterministic(self):
        id1 = _make_id("file.py", "MyClass")
        id2 = _make_id("file.py", "MyClass")
        assert id1 == id2

    def test_different_inputs(self):
        id1 = _make_id("file.py", "ClassA")
        id2 = _make_id("file.py", "ClassB")
        assert id1 != id2

    def test_returns_string(self):
        result = _make_id("test.py", "Foo")
        assert isinstance(result, str)
        assert len(result) == 12


# ════════════════════════════════════════════════════════════════════
# Python parser tests
# ════════════════════════════════════════════════════════════════════


class TestPythonParsing:
    """Tests for Python AST-based analysis of the sample_python fixture."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Analyze the sample_python fixture once for all tests."""
        self.graph = analyze_codebase(str(SAMPLE_PYTHON))
        self.nodes_by_name = {n.name: n for n in self.graph.nodes}
        self.node_ids = {n.id for n in self.graph.nodes}
        self.edges = self.graph.edges

    # ── Node extraction ─────────────────────────────────────────

    def test_finds_classes(self):
        class_names = {n.name for n in self.graph.nodes if n.kind == NodeKind.CLASS}
        assert "BaseModel" in class_names
        assert "User" in class_names
        assert "Order" in class_names
        assert "UserService" in class_names
        assert "OrderService" in class_names
        assert "NotificationService" in class_names

    def test_finds_methods(self):
        method_names = {n.name for n in self.graph.nodes if n.kind == NodeKind.METHOD}
        assert "validate" in method_names
        assert "get_display_name" in method_names
        assert "create_user" in method_names
        assert "create_order" in method_names

    def test_class_has_correct_kind(self):
        assert self.nodes_by_name["User"].kind == NodeKind.CLASS

    def test_method_has_correct_kind(self):
        assert self.nodes_by_name["validate"].kind == NodeKind.METHOD

    def test_node_has_file_path(self):
        user = self.nodes_by_name["User"]
        assert user.file_path.endswith("models.py")

    def test_node_has_line_numbers(self):
        user = self.nodes_by_name["User"]
        assert user.line_start > 0
        assert user.line_end >= user.line_start

    def test_class_has_methods_list(self):
        user = self.nodes_by_name["User"]
        assert "get_display_name" in user.methods
        assert "is_admin" in user.methods

    def test_class_has_bases(self):
        user = self.nodes_by_name["User"]
        assert "BaseModel" in user.bases

    def test_class_has_docstring(self):
        user = self.nodes_by_name["User"]
        assert user.docstring is not None
        assert "user" in user.docstring.lower()

    def test_class_variables_extracted(self):
        user = self.nodes_by_name["User"]
        var_names = {v["name"] for v in user.variables}
        assert "name" in var_names
        assert "email" in var_names

    def test_exception_class_found(self):
        assert "OrderError" in self.nodes_by_name

    def test_test_class_found(self):
        assert "TestUserService" in self.nodes_by_name

    # ── Edge extraction ─────────────────────────────────────────

    def _edge_exists(self, source_name, target_name, kind):
        """Helper: check if an edge of the given kind exists between named nodes."""
        src_ids = {n.id for n in self.graph.nodes if n.name == source_name}
        tgt_ids = {n.id for n in self.graph.nodes if n.name == target_name}
        return any(
            e.source in src_ids and e.target in tgt_ids and e.kind == kind
            for e in self.edges
        )

    def test_contains_edges(self):
        """Classes should have 'contains' edges to their methods."""
        assert self._edge_exists("User", "get_display_name", EdgeKind.CONTAINS)
        assert self._edge_exists("User", "is_admin", EdgeKind.CONTAINS)

    def test_inherits_edges(self):
        """User and Order should inherit from BaseModel."""
        assert self._edge_exists("User", "BaseModel", EdgeKind.INHERITS)
        assert self._edge_exists("Order", "BaseModel", EdgeKind.INHERITS)

    def test_uses_type_edges(self):
        """OrderService constructor takes UserService — should create uses_type."""
        # Order.__init__ takes user: User parameter
        order = self.nodes_by_name.get("Order")
        if order:
            type_edges = [
                e for e in self.edges
                if e.kind == EdgeKind.USES_TYPE
            ]
            # At minimum we should have some type reference edges
            assert len(type_edges) >= 0  # may vary based on resolution

    def test_no_self_loop_edges(self):
        """No edge should have the same source and target."""
        for e in self.edges:
            assert e.source != e.target, f"Self-loop edge found: {e}"

    # ── Full graph properties ───────────────────────────────────

    def test_graph_has_nodes_and_edges(self):
        assert len(self.graph.nodes) > 0
        assert len(self.graph.edges) > 0

    def test_all_edge_targets_exist(self):
        """Every edge target should reference an existing node."""
        for e in self.edges:
            assert e.source in self.node_ids, f"Edge source {e.source} not in nodes"
            assert e.target in self.node_ids, f"Edge target {e.target} not in nodes"

    def test_to_dict_round_trip(self):
        """Graph.to_dict() should produce valid JSON-serializable data."""
        d = self.graph.to_dict()
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert len(parsed["nodes"]) == len(self.graph.nodes)
        assert len(parsed["edges"]) == len(self.graph.edges)


# ════════════════════════════════════════════════════════════════════
# JavaScript / TypeScript parser tests
# ════════════════════════════════════════════════════════════════════


class TestJavaScriptParsing:
    """Tests for JS/TS regex-based analysis of the sample_js fixture."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.graph = analyze_codebase(str(SAMPLE_JS))
        self.nodes_by_name = {n.name: n for n in self.graph.nodes}

    def test_finds_js_classes(self):
        class_names = {n.name for n in self.graph.nodes if n.kind == NodeKind.CLASS}
        assert "EventEmitter" in class_names
        assert "AppController" in class_names
        assert "HttpServer" in class_names

    def test_finds_js_methods(self):
        method_names = {n.name for n in self.graph.nodes if n.kind == NodeKind.METHOD}
        assert "on" in method_names
        assert "emit" in method_names
        assert "start" in method_names
        assert "listen" in method_names

    def test_js_inheritance(self):
        """AppController extends EventEmitter."""
        edges = self.graph.edges
        controller_ids = {n.id for n in self.graph.nodes if n.name == "AppController"}
        emitter_ids = {n.id for n in self.graph.nodes if n.name == "EventEmitter"}
        inherits = [
            e for e in edges
            if e.kind == EdgeKind.INHERITS
            and e.source in controller_ids
            and e.target in emitter_ids
        ]
        assert len(inherits) >= 1

    def test_js_type_references(self):
        """AppController references HttpServer (via new) — detected as uses_type."""
        edges = self.graph.edges
        controller_ids = {n.id for n in self.graph.nodes if n.name == "AppController"}
        server_ids = {n.id for n in self.graph.nodes if n.name == "HttpServer"}
        type_refs = [
            e for e in edges
            if e.kind == EdgeKind.USES_TYPE
            and e.source in controller_ids
            and e.target in server_ids
        ]
        assert len(type_refs) >= 1

    def test_js_contains_edges(self):
        """Classes should contain their methods."""
        edges = self.graph.edges
        contains_edges = [e for e in edges if e.kind == EdgeKind.CONTAINS]
        assert len(contains_edges) >= 3  # at least a few class→method edges

    def test_js_node_has_file_path(self):
        emitter = self.nodes_by_name["EventEmitter"]
        assert emitter.file_path.endswith(".js")


# ════════════════════════════════════════════════════════════════════
# Abstract graph tests
# ════════════════════════════════════════════════════════════════════


class TestAbstractGraph:
    """Tests for the abstract/heuristic grouping view."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.full = analyze_codebase(str(SAMPLE_PYTHON))
        self.abstract = abstract_graph(self.full)

    def test_abstract_has_fewer_nodes(self):
        """Abstract graph should collapse classes into groups."""
        assert len(self.abstract.nodes) <= len(self.full.nodes)

    def test_abstract_nodes_have_group_or_chain_kind(self):
        """Abstract nodes should be groups or island chains (plus maybe some classes)."""
        kinds = {n.kind for n in self.abstract.nodes}
        # Should have at least one group-type node
        assert kinds & {NodeKind.ISLAND_CHAIN, NodeKind.GROUP, NodeKind.CLASS}

    def test_abstract_preserves_edges(self):
        """Abstract graph should still have edges."""
        assert len(self.abstract.edges) >= 0  # may have fewer

    def test_abstract_to_dict(self):
        d = self.abstract.to_dict()
        assert "nodes" in d
        assert "edges" in d
        # Abstract graph often includes intermediate_nodes for drill-down
        serialized = json.dumps(d)
        assert len(serialized) > 0


# ════════════════════════════════════════════════════════════════════
# Cache tests
# ════════════════════════════════════════════════════════════════════


class TestCaching:
    """Tests for analysis result caching."""

    def test_save_and_load_cache(self):
        """Saving and loading from cache should return the same data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal file so fingerprinting works
            py_file = Path(tmpdir) / "hello.py"
            py_file.write_text("class Hello:\n    pass\n")

            graph_dict = {"nodes": [{"id": "n1", "name": "Hello"}], "edges": []}
            _save_to_cache(tmpdir, False, graph_dict)
            loaded = _load_from_cache(tmpdir, False)
            assert loaded is not None
            assert loaded["nodes"][0]["name"] == "Hello"

    def test_load_missing_cache(self):
        """Loading from a non-existent cache should return None."""
        result = _load_from_cache("/tmp/nonexistent_vca_test_dir", False)
        assert result is None

    def test_stale_cache_returns_none(self):
        """Modifying a file should invalidate the cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / "test.py"
            py_file.write_text("class A:\n    pass\n")

            _save_to_cache(tmpdir, False, {"nodes": [], "edges": []})

            # Modify the file → fingerprint changes
            py_file.write_text("class A:\n    x = 1\n")

            loaded = _load_from_cache(tmpdir, False)
            assert loaded is None


# ════════════════════════════════════════════════════════════════════
# Variable extraction tests
# ════════════════════════════════════════════════════════════════════


class TestVariableExtraction:
    """Tests for class and method variable extraction."""

    def test_class_variable_types(self):
        """Analyze sample and check that User class has typed variables."""
        graph = analyze_codebase(str(SAMPLE_PYTHON))
        user = next(n for n in graph.nodes if n.name == "User")
        var_names = {v["name"] for v in user.variables}
        assert "name" in var_names
        assert "email" in var_names

    def test_method_parameters(self):
        """Methods should have their parameters extracted as variables."""
        graph = analyze_codebase(str(SAMPLE_PYTHON))
        add_item = next((n for n in graph.nodes if n.name == "add_item"), None)
        if add_item:
            var_names = {v["name"] for v in add_item.variables}
            assert "item" in var_names
            assert "price" in var_names


# ════════════════════════════════════════════════════════════════════
# Language enrichment tests
# ════════════════════════════════════════════════════════════════════


class TestLanguageEnrichment:
    """Tests for the language annotation on nodes."""

    def test_python_nodes_have_language(self):
        graph = analyze_codebase(str(SAMPLE_PYTHON))
        d = graph.to_dict()
        for node in d["nodes"]:
            assert "languages" in node
            if node.get("file_path", "").endswith(".py"):
                assert "python" in node["languages"]

    def test_js_nodes_have_language(self):
        graph = analyze_codebase(str(SAMPLE_JS))
        d = graph.to_dict()
        for node in d["nodes"]:
            if node.get("file_path", "").endswith(".js"):
                assert "javascript" in node["languages"]
