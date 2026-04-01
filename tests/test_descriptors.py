"""Tests for backend.descriptors — hierarchy building and caching.

Covers:
  - Disk cache save/load (version check, round-trip)
  - Content hashing (source hash, children hash)
  - Hierarchy building (_build_hierarchy)
  - Topological level ordering (_topo_levels)
  - Edge context building
  - Source snippet reading
"""

import json
import tempfile
from pathlib import Path

from backend.descriptors import (
    _load_desc_cache,
    _save_desc_cache,
    _hash_source,
    _hash_children,
    _read_source_snippet,
    _build_hierarchy,
    _topo_levels,
    _build_edge_context,
)


# ════════════════════════════════════════════════════════════════════
# Disk cache
# ════════════════════════════════════════════════════════════════════


class TestDescCache:

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            descs = {
                "node1": {"description": "A class", "hash": "abc123"},
                "node2": {"description": "A method", "hash": "def456"},
            }
            _save_desc_cache(tmpdir, descs)
            loaded = _load_desc_cache(tmpdir)
            assert loaded is not None
            assert "node1" in loaded
            assert loaded["node1"]["description"] == "A class"

    def test_load_empty(self):
        result = _load_desc_cache("/tmp/nonexistent_vca_desc_test")
        assert result == {}

    def test_version_mismatch_returns_empty(self):
        """If cache version doesn't match, return empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _save_desc_cache(tmpdir, {"x": {"description": "old"}})

            # Manually tamper with the version
            from backend.descriptors import _desc_cache_path
            path = _desc_cache_path(tmpdir)
            data = json.loads(path.read_text())
            data["_version"] = -1  # wrong version
            path.write_text(json.dumps(data))

            loaded = _load_desc_cache(tmpdir)
            assert loaded == {}


# ════════════════════════════════════════════════════════════════════
# Content hashing
# ════════════════════════════════════════════════════════════════════


class TestHashing:

    def test_hash_source_deterministic(self):
        h1 = _hash_source("class Foo:\n    pass")
        h2 = _hash_source("class Foo:\n    pass")
        assert h1 == h2

    def test_hash_source_differs(self):
        h1 = _hash_source("class Foo:\n    pass")
        h2 = _hash_source("class Bar:\n    pass")
        assert h1 != h2

    def test_hash_source_length(self):
        h = _hash_source("test")
        assert len(h) == 20

    def test_hash_children_deterministic(self):
        h1 = _hash_children(["hash_a", "hash_b"])
        h2 = _hash_children(["hash_a", "hash_b"])
        assert h1 == h2

    def test_hash_children_order_independent(self):
        """Order of children hashes should not matter."""
        h1 = _hash_children(["hash_a", "hash_b"])
        h2 = _hash_children(["hash_b", "hash_a"])
        assert h1 == h2

    def test_hash_children_differs(self):
        h1 = _hash_children(["hash_a", "hash_b"])
        h2 = _hash_children(["hash_a", "hash_c"])
        assert h1 != h2


# ════════════════════════════════════════════════════════════════════
# Source snippet reading
# ════════════════════════════════════════════════════════════════════


class TestSourceSnippet:

    def test_read_snippet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "test.py").write_text("line1\nline2\nline3\nline4\nline5\n")
            snippet = _read_source_snippet(tmpdir, "test.py", 2, 4)
            assert "line2" in snippet
            assert "line3" in snippet
            assert "line4" in snippet
            assert "line1" not in snippet

    def test_read_snippet_missing_file(self):
        snippet = _read_source_snippet("/tmp", "nonexistent.py", 1, 5)
        assert snippet == ""

    def test_read_snippet_no_path(self):
        snippet = _read_source_snippet("/tmp", "", 1, 5)
        assert snippet == ""

    def test_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_content = "x" * 10000 + "\n"
            Path(tmpdir, "big.py").write_text(long_content)
            snippet = _read_source_snippet(tmpdir, "big.py", 1, 1, max_chars=100)
            assert len(snippet) <= 120  # 100 + truncation marker


# ════════════════════════════════════════════════════════════════════
# Hierarchy building
# ════════════════════════════════════════════════════════════════════


class TestBuildHierarchy:

    def _make_graph_dict(self):
        return {
            "nodes": [
                {"id": "group1", "name": "Services", "kind": "island_chain",
                 "member_ids": ["class1", "class2"]},
            ],
            "edges": [
                {"source": "class1", "target": "method1", "kind": "contains"},
            ],
            "intermediate_nodes": [
                {"id": "class1", "name": "UserService", "kind": "class",
                 "member_ids": []},
                {"id": "class2", "name": "OrderService", "kind": "class",
                 "member_ids": []},
                {"id": "method1", "name": "create_user", "kind": "method",
                 "member_ids": []},
            ],
            "intermediate_edges": [],
        }

    def test_builds_node_map(self):
        gd = self._make_graph_dict()
        node_map, children_map, leaf_ids = _build_hierarchy(gd)
        assert "group1" in node_map
        assert "class1" in node_map
        assert "method1" in node_map

    def test_children_map(self):
        gd = self._make_graph_dict()
        node_map, children_map, leaf_ids = _build_hierarchy(gd)
        assert "class1" in children_map["group1"]
        assert "class2" in children_map["group1"]

    def test_contains_edges_create_children(self):
        gd = self._make_graph_dict()
        node_map, children_map, leaf_ids = _build_hierarchy(gd)
        assert "method1" in children_map.get("class1", [])

    def test_leaf_ids(self):
        gd = self._make_graph_dict()
        node_map, children_map, leaf_ids = _build_hierarchy(gd)
        # method1, class2 are leaves (no children)
        assert "method1" in leaf_ids
        assert "class2" in leaf_ids


# ════════════════════════════════════════════════════════════════════
# Topological levels
# ════════════════════════════════════════════════════════════════════


class TestTopoLevels:

    def test_simple_hierarchy(self):
        all_nodes = {"a": {}, "b": {}, "c": {}}
        children_map = {"a": ["b", "c"]}
        levels = _topo_levels(all_nodes, children_map)
        # Level 0: b, c (leaves)
        # Level 1: a (parent)
        assert len(levels) == 2
        assert set(levels[0]) == {"b", "c"}
        assert levels[1] == ["a"]

    def test_flat_graph(self):
        """All leaves, no hierarchy."""
        all_nodes = {"a": {}, "b": {}, "c": {}}
        children_map = {}
        levels = _topo_levels(all_nodes, children_map)
        assert len(levels) == 1
        assert set(levels[0]) == {"a", "b", "c"}

    def test_deep_hierarchy(self):
        all_nodes = {"root": {}, "mid": {}, "leaf": {}}
        children_map = {"root": ["mid"], "mid": ["leaf"]}
        levels = _topo_levels(all_nodes, children_map)
        assert len(levels) == 3
        assert levels[0] == ["leaf"]
        assert levels[1] == ["mid"]
        assert levels[2] == ["root"]


# ════════════════════════════════════════════════════════════════════
# Edge context building
# ════════════════════════════════════════════════════════════════════


class TestEdgeContext:

    def test_calls_context(self):
        all_nodes = {"a": {"name": "ServiceA"}, "b": {"name": "ServiceB"}}
        graph_dict = {
            "edges": [{"source": "a", "target": "b", "kind": "calls"}],
            "intermediate_edges": [],
        }
        ctx = _build_edge_context(all_nodes, graph_dict)
        assert any("calls ServiceB" in s for s in ctx.get("a", []))
        assert any("called by ServiceA" in s for s in ctx.get("b", []))

    def test_inherits_context(self):
        all_nodes = {"child": {"name": "Child"}, "base": {"name": "Base"}}
        graph_dict = {
            "edges": [{"source": "child", "target": "base", "kind": "inherits"}],
            "intermediate_edges": [],
        }
        ctx = _build_edge_context(all_nodes, graph_dict)
        assert any("inherits from Base" in s for s in ctx.get("child", []))

    def test_contains_edges_skipped(self):
        all_nodes = {"a": {"name": "A"}, "b": {"name": "B"}}
        graph_dict = {
            "edges": [{"source": "a", "target": "b", "kind": "contains"}],
            "intermediate_edges": [],
        }
        ctx = _build_edge_context(all_nodes, graph_dict)
        assert len(ctx) == 0

    def test_empty_graph(self):
        ctx = _build_edge_context({}, {"edges": [], "intermediate_edges": []})
        assert ctx == {}

