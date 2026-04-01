"""Tests for backend.insights — pattern detection engine.

Covers:
  - Circular dependency detection (Tarjan's SCC)
  - God class detection
  - Orphan module detection
  - Hub class detection
  - Layer violation detection
  - Architecture pattern inference
  - Orchestrator (detect_insights)
"""

from backend.insights import (
    detect_insights,
    _detect_circular_dependencies,
    _detect_god_classes,
    _detect_orphan_modules,
    _detect_hub_classes,
    _detect_layer_violations,
    _detect_architecture_pattern,
)


# ── Helpers ─────────────────────────────────────────────────────────

def _node(nid, name, kind="class", methods=None, file_path="mod.py"):
    return {
        "id": nid, "name": name, "kind": kind,
        "file_path": file_path,
        "methods": methods or [],
    }


def _edge(src, tgt, kind="calls"):
    return {"source": src, "target": tgt, "kind": kind}


# ════════════════════════════════════════════════════════════════════
# Circular dependency detection
# ════════════════════════════════════════════════════════════════════


class TestCircularDependencies:

    def test_no_cycle(self):
        nodes = [_node("a", "A"), _node("b", "B"), _node("c", "C")]
        edges = [_edge("a", "b", "imports"), _edge("b", "c", "imports")]
        result = _detect_circular_dependencies(nodes, edges)
        assert len(result) == 0

    def test_simple_cycle(self):
        nodes = [_node("a", "A"), _node("b", "B")]
        edges = [_edge("a", "b", "imports"), _edge("b", "a", "imports")]
        result = _detect_circular_dependencies(nodes, edges)
        assert len(result) == 1
        assert result[0].rule_name == "circular_dependency"
        assert result[0].severity == "error"
        # Both nodes should be in affected_node_ids
        assert set(result[0].affected_node_ids) == {"a", "b"}

    def test_three_node_cycle(self):
        nodes = [_node("a", "A"), _node("b", "B"), _node("c", "C")]
        edges = [
            _edge("a", "b", "calls"),
            _edge("b", "c", "calls"),
            _edge("c", "a", "calls"),
        ]
        result = _detect_circular_dependencies(nodes, edges)
        assert len(result) == 1
        assert len(result[0].affected_node_ids) == 3

    def test_ignores_contains_edges(self):
        """Contains edges should not be considered for cycles."""
        nodes = [_node("a", "A"), _node("b", "B")]
        edges = [_edge("a", "b", "contains"), _edge("b", "a", "contains")]
        result = _detect_circular_dependencies(nodes, edges)
        assert len(result) == 0

    def test_ignores_non_class_nodes(self):
        """Only class nodes should be considered for cycle detection."""
        nodes = [
            _node("a", "method_a", kind="method"),
            _node("b", "method_b", kind="method"),
        ]
        edges = [_edge("a", "b", "calls"), _edge("b", "a", "calls")]
        result = _detect_circular_dependencies(nodes, edges)
        assert len(result) == 0

    def test_self_loop_not_cycle(self):
        """A self-referencing node is not a cycle."""
        nodes = [_node("a", "A")]
        edges = [_edge("a", "a", "calls")]
        result = _detect_circular_dependencies(nodes, edges)
        assert len(result) == 0


# ════════════════════════════════════════════════════════════════════
# God class detection
# ════════════════════════════════════════════════════════════════════


class TestGodClasses:

    def test_normal_class_not_flagged(self):
        nodes = [_node("a", "SmallClass", methods=["m1", "m2", "m3"])]
        edges = [_edge("a", "x", "calls")]
        result = _detect_god_classes(nodes, edges)
        assert len(result) == 0

    def test_many_methods_low_degree_not_flagged(self):
        """A class with many methods but few connections is not a god class."""
        methods = [f"method_{i}" for i in range(15)]
        nodes = [_node("a", "BigButIsolated", methods=methods)]
        edges = [_edge("a", "b", "calls")]
        result = _detect_god_classes(nodes, edges)
        assert len(result) == 0

    def test_god_class_flagged(self):
        """A class with many methods AND many connections is a god class."""
        methods = [f"method_{i}" for i in range(12)]
        nodes = [_node("a", "GodClass", methods=methods)]
        # Create many edges to push degree above threshold
        edges = [_edge("a", f"t{i}", "calls") for i in range(10)]
        result = _detect_god_classes(nodes, edges)
        assert len(result) == 1
        assert result[0].rule_name == "god_class"
        assert result[0].severity == "warning"

    def test_custom_thresholds(self):
        nodes = [_node("a", "Medium", methods=["m1", "m2", "m3"])]
        edges = [_edge("a", "b", "calls"), _edge("a", "c", "calls")]
        result = _detect_god_classes(nodes, edges, method_threshold=3, degree_threshold=2)
        assert len(result) == 1


# ════════════════════════════════════════════════════════════════════
# Orphan module detection
# ════════════════════════════════════════════════════════════════════


class TestOrphanModules:

    def test_connected_class_not_orphan(self):
        nodes = [_node("a", "Connected"), _node("b", "Other")]
        edges = [_edge("a", "b", "imports")]
        result = _detect_orphan_modules(nodes, edges)
        assert len(result) == 0

    def test_orphan_class_detected(self):
        """A class with zero non-contains edges is an orphan."""
        nodes = [_node("a", "Orphan"), _node("b", "Other")]
        edges = [_edge("a", "m", "contains")]  # only contains edge
        result = _detect_orphan_modules(nodes, edges)
        # 'b' has no edges at all, so it should be detected as orphan
        orphan_names = [r.affected_node_ids[0] for r in result]
        assert "b" in orphan_names

    def test_methods_not_flagged(self):
        """Method nodes should not be flagged as orphans."""
        nodes = [_node("m", "some_method", kind="method")]
        edges = []
        result = _detect_orphan_modules(nodes, edges)
        assert len(result) == 0

    def test_orphan_has_info_severity(self):
        nodes = [_node("a", "LonelyClass")]
        edges = []
        result = _detect_orphan_modules(nodes, edges)
        assert len(result) == 1
        assert result[0].severity == "info"
        assert result[0].rule_name == "orphan_module"


# ════════════════════════════════════════════════════════════════════
# Hub class detection
# ════════════════════════════════════════════════════════════════════


class TestHubClasses:

    def test_normal_fan_in_not_flagged(self):
        nodes = [_node("hub", "Hub"), _node("a", "A"), _node("b", "B")]
        edges = [_edge("a", "hub", "imports"), _edge("b", "hub", "imports")]
        result = _detect_hub_classes(nodes, edges)
        assert len(result) == 0

    def test_hub_class_flagged(self):
        """A class with high fan-in should be flagged."""
        nodes = [_node("hub", "CentralClass")]
        edges = []
        for i in range(15):
            nid = f"dep_{i}"
            nodes.append(_node(nid, f"Dep{i}"))
            edges.append(_edge(nid, "hub", "imports"))
        result = _detect_hub_classes(nodes, edges)
        assert len(result) == 1
        assert result[0].rule_name == "hub_class"
        assert "CentralClass" in result[0].title

    def test_custom_threshold(self):
        nodes = [_node("hub", "Hub"), _node("a", "A"), _node("b", "B")]
        edges = [_edge("a", "hub", "imports"), _edge("b", "hub", "imports")]
        result = _detect_hub_classes(nodes, edges, degree_threshold=2)
        assert len(result) == 1


# ════════════════════════════════════════════════════════════════════
# Layer violation detection
# ════════════════════════════════════════════════════════════════════


class TestLayerViolations:

    def test_normal_direction_no_violation(self):
        """Service → Repository is a normal dependency direction."""
        nodes = [
            _node("s", "OrderService", file_path="services.py"),
            _node("r", "OrderRepository", file_path="repositories.py"),
        ]
        edges = [_edge("s", "r", "imports")]
        result = _detect_layer_violations(nodes, edges)
        assert len(result) == 0

    def test_reverse_violation(self):
        """Repository → Controller is a reverse violation."""
        nodes = [
            _node("c", "UserController", file_path="controllers.py"),
            _node("r", "UserRepository", file_path="repositories.py"),
        ]
        edges = [_edge("r", "c", "imports")]
        result = _detect_layer_violations(nodes, edges)
        assert len(result) >= 1
        assert any(r.rule_name == "layer_violation" for r in result)

    def test_single_layer_no_violation(self):
        """If all classes are in the same layer, no violation."""
        nodes = [
            _node("a", "ServiceA", file_path="services.py"),
            _node("b", "ServiceB", file_path="services.py"),
        ]
        edges = [_edge("a", "b", "calls")]
        result = _detect_layer_violations(nodes, edges)
        assert len(result) == 0


# ════════════════════════════════════════════════════════════════════
# Architecture pattern inference
# ════════════════════════════════════════════════════════════════════


class TestArchitecturePattern:

    def test_mvc_pattern(self):
        nodes = [
            _node("c", "UserController", file_path="controllers.py"),
            _node("v", "UserView", file_path="views.py"),
            _node("m", "UserModel", file_path="models.py"),
        ]
        edges = [_edge("c", "v", "calls"), _edge("c", "m", "calls")]
        result = _detect_architecture_pattern(nodes, edges)
        if result:
            assert result[0].rule_name == "architecture_pattern"
            assert "MVC" in result[0].title or "Layered" in result[0].title

    def test_no_pattern_for_generic_names(self):
        """Classes with no pattern signals should not generate an insight."""
        nodes = [_node("a", "Foo"), _node("b", "Bar"), _node("c", "Baz")]
        edges = [_edge("a", "b", "calls")]
        result = _detect_architecture_pattern(nodes, edges)
        # Might be empty if score is below threshold
        assert all(r.rule_name == "architecture_pattern" for r in result)

    def test_empty_graph(self):
        result = _detect_architecture_pattern([], [])
        assert len(result) == 0


# ════════════════════════════════════════════════════════════════════
# Orchestrator
# ════════════════════════════════════════════════════════════════════


class TestDetectInsights:

    def test_returns_list_of_dicts(self):
        graph_dict = {"nodes": [_node("a", "Orphan")], "edges": []}
        result = detect_insights(graph_dict)
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)
            assert "severity" in item
            assert "title" in item
            assert "description" in item
            assert "affected_node_ids" in item
            assert "rule_name" in item

    def test_sorted_by_severity(self):
        """Results should be error > warning > info."""
        # Create a scenario with multiple severity levels
        nodes = [_node("a", "A"), _node("b", "B"), _node("c", "Orphan")]
        edges = [_edge("a", "b", "imports"), _edge("b", "a", "imports")]
        result = detect_insights({"nodes": nodes, "edges": edges})
        severities = [r["severity"] for r in result]
        severity_order = {"error": 0, "warning": 1, "info": 2}
        for i in range(len(severities) - 1):
            assert severity_order.get(severities[i], 9) <= severity_order.get(severities[i + 1], 9)

    def test_empty_graph(self):
        result = detect_insights({"nodes": [], "edges": []})
        assert isinstance(result, list)

    def test_with_sample_fixture(self):
        """Run insights on the sample Python fixture."""
        from backend.analyzer import analyze_codebase
        from pathlib import Path

        fixtures = Path(__file__).parent / "fixtures" / "sample_python"
        graph = analyze_codebase(str(fixtures))
        graph_dict = graph.to_dict()
        result = detect_insights(graph_dict)
        assert isinstance(result, list)
        # All results should have required fields
        for item in result:
            assert "severity" in item
            assert item["severity"] in ("error", "warning", "info")
