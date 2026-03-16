"""
Pattern detection engine — analyses a graph dict and returns architectural insights.

Runs six detection rules:
  1. Circular dependencies — Tarjan's SCC on the directed import/call graph
  2. God classes — classes with excessive methods and high connectivity
  3. Orphan modules — classes with zero non-contains edges
  4. Hub classes — classes with unusually high fan-in
  5. Layer violations — edges that skip or reverse the expected layer order
  6. Architecture pattern inference — heuristic labelling of the overall pattern
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from collections import defaultdict
from typing import Optional


# ── Insight model ───────────────────────────────────────────────────

@dataclass
class Insight:
    severity: str          # "error", "warning", "info"
    title: str
    description: str
    affected_node_ids: list[str]
    rule_name: str


def _insight_to_dict(i: Insight) -> dict:
    return asdict(i)


# ── Rule 1: Circular dependency detection (Tarjan's SCC) ───────────

_DEPENDENCY_EDGE_KINDS = {"imports", "calls", "instantiates", "uses_type", "inherits"}


def _detect_circular_dependencies(nodes: list[dict], edges: list[dict]) -> list[Insight]:
    """Find strongly connected components (cycles) in the dependency graph.

    Uses iterative Tarjan's SCC algorithm.  Only class-level nodes and
    non-contains edges are considered.
    """
    # Build directed adjacency from class-level nodes only
    class_ids = {n["id"] for n in nodes if n.get("kind") == "class"}

    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.get("kind") not in _DEPENDENCY_EDGE_KINDS:
            continue
        src = e["source"] if isinstance(e["source"], str) else e["source"]["id"]
        tgt = e["target"] if isinstance(e["target"], str) else e["target"]["id"]
        if src in class_ids and tgt in class_ids and src != tgt:
            adj[src].append(tgt)

    # Iterative Tarjan's SCC
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    lowlink: dict[str, int] = {}
    index: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        # Use an explicit work-stack to avoid Python recursion limits
        work: list[tuple[str, int]] = [(v, 0)]
        while work:
            node, adj_idx = work[-1]

            if adj_idx == 0:
                # First visit to this node
                index[node] = index_counter[0]
                lowlink[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack.add(node)

            neighbors = adj.get(node, [])

            if adj_idx < len(neighbors):
                w = neighbors[adj_idx]
                work[-1] = (node, adj_idx + 1)

                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    lowlink[node] = min(lowlink[node], index[w])
            else:
                # All neighbors explored — check if this is a root
                if lowlink[node] == index[node]:
                    scc: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        scc.append(w)
                        if w == node:
                            break
                    sccs.append(scc)

                # Propagate lowlink to parent
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

    for nid in class_ids:
        if nid not in index:
            strongconnect(nid)

    # Report SCCs with more than one node (= actual cycles)
    node_by_id = {n["id"]: n for n in nodes}
    insights: list[Insight] = []

    for scc in sccs:
        if len(scc) < 2:
            continue
        names = [node_by_id[nid]["name"] for nid in scc if nid in node_by_id]
        cycle_str = " → ".join(names + [names[0]]) if names else "unknown"

        insights.append(Insight(
            severity="error",
            title=f"Circular dependency ({len(scc)} classes)",
            description=f"Cycle: {cycle_str}",
            affected_node_ids=list(scc),
            rule_name="circular_dependency",
        ))

    return insights


# ── Rule 2: God class detection ─────────────────────────────────────

_GOD_CLASS_METHOD_THRESHOLD = 10
_GOD_CLASS_DEGREE_THRESHOLD = 8


def _detect_god_classes(
    nodes: list[dict],
    edges: list[dict],
    method_threshold: int = _GOD_CLASS_METHOD_THRESHOLD,
    degree_threshold: int = _GOD_CLASS_DEGREE_THRESHOLD,
) -> list[Insight]:
    """Flag classes with too many methods AND too many connections."""
    class_nodes = [n for n in nodes if n.get("kind") == "class"]

    # Compute degree (fan-in + fan-out) excluding 'contains' edges
    degree: dict[str, int] = defaultdict(int)
    for e in edges:
        if e.get("kind") == "contains":
            continue
        src = e["source"] if isinstance(e["source"], str) else e["source"]["id"]
        tgt = e["target"] if isinstance(e["target"], str) else e["target"]["id"]
        degree[src] += 1
        degree[tgt] += 1

    insights: list[Insight] = []
    for n in class_nodes:
        methods = n.get("methods", [])
        nid = n["id"]
        node_degree = degree.get(nid, 0)

        if len(methods) >= method_threshold and node_degree >= degree_threshold:
            insights.append(Insight(
                severity="warning",
                title=f"God class: {n['name']}",
                description=(
                    f"{n['name']} has {len(methods)} methods and {node_degree} "
                    f"connections — consider breaking it into smaller classes."
                ),
                affected_node_ids=[nid],
                rule_name="god_class",
            ))

    return insights


# ── Rule 3: Orphan module detection ─────────────────────────────────

def _detect_orphan_modules(nodes: list[dict], edges: list[dict]) -> list[Insight]:
    """Flag class nodes with zero non-contains edges (completely disconnected)."""
    edge_count: dict[str, int] = defaultdict(int)
    for e in edges:
        if e.get("kind") == "contains":
            continue
        src = e["source"] if isinstance(e["source"], str) else e["source"]["id"]
        tgt = e["target"] if isinstance(e["target"], str) else e["target"]["id"]
        edge_count[src] += 1
        edge_count[tgt] += 1

    insights: list[Insight] = []
    for n in nodes:
        if n.get("kind") != "class":
            continue
        nid = n["id"]
        if edge_count.get(nid, 0) == 0:
            insights.append(Insight(
                severity="info",
                title=f"Orphan class: {n['name']}",
                description=(
                    f"{n['name']} in {n.get('file_path', '?')} has no "
                    f"connections to other classes — it may be unused or "
                    f"missing import links."
                ),
                affected_node_ids=[nid],
                rule_name="orphan_module",
            ))

    return insights


# ── Rule 4: Hub class detection ─────────────────────────────────────

_HUB_CLASS_DEGREE_THRESHOLD = 12


def _detect_hub_classes(
    nodes: list[dict],
    edges: list[dict],
    degree_threshold: int = _HUB_CLASS_DEGREE_THRESHOLD,
) -> list[Insight]:
    """Flag classes that are depended upon by an unusually high number of other
    classes (high fan-in), making them risky change hotspots."""
    class_ids = {n["id"] for n in nodes if n.get("kind") == "class"}

    fan_in: dict[str, int] = defaultdict(int)
    for e in edges:
        if e.get("kind") == "contains":
            continue
        tgt = e["target"] if isinstance(e["target"], str) else e["target"]["id"]
        if tgt in class_ids:
            fan_in[tgt] += 1

    node_by_id = {n["id"]: n for n in nodes}
    insights: list[Insight] = []
    for nid, count in fan_in.items():
        if count >= degree_threshold:
            n = node_by_id.get(nid)
            if not n:
                continue
            insights.append(Insight(
                severity="warning",
                title=f"Hub class: {n['name']}",
                description=(
                    f"{n['name']} is depended on by {count} other classes — "
                    f"changes here will ripple widely."
                ),
                affected_node_ids=[nid],
                rule_name="hub_class",
            ))

    return insights


# ── Rule 5: Layer violation detection ────────────────────────────────

# Default ordered layers (top → bottom).  Lower index = higher layer.
# An edge from a lower layer to a higher layer is fine.
# An edge that reverses direction or skips a layer is a violation.
_DEFAULT_LAYERS: list[list[str]] = [
    # Layer 0 — Presentation / entry-point
    ["app", "application", "main", "cli", "entrypoint", "ui", "view",
     "controller", "router", "endpoint", "api", "handler", "command",
     "server", "web", "rest"],
    # Layer 1 — Service / business logic
    ["service", "usecase", "interactor", "orchestrator", "coordinator",
     "manager", "processor", "workflow", "pipeline", "facade",
     "mediator", "dispatcher"],
    # Layer 2 — Domain / models
    ["model", "entity", "domain", "schema", "dto", "aggregate",
     "valueobject", "event", "enum", "dataclass", "record",
     "protocol", "interface", "abc", "base"],
    # Layer 3 — Data access / infrastructure
    ["repository", "repo", "dao", "store", "gateway", "adapter",
     "client", "connector", "driver", "persistence", "database",
     "cache", "queue"],
    # Layer 4 — Utilities / cross-cutting
    ["util", "utils", "helper", "helpers", "common", "shared",
     "mixin", "decorator", "middleware", "guard", "validator",
     "formatter", "converter", "exception", "error", "config",
     "settings", "constants", "logging", "metrics"],
]


def _classify_layer(node: dict) -> Optional[int]:
    """Return the layer index (0 = top) for a node, or None if unclassifiable."""
    name_lower = (node.get("name") or "").lower()
    file_stem = os.path.splitext(os.path.basename(node.get("file_path") or ""))[0].lower()

    for layer_idx, keywords in enumerate(_DEFAULT_LAYERS):
        for kw in keywords:
            if kw in name_lower or kw in file_stem:
                return layer_idx
    return None


def _detect_layer_violations(nodes: list[dict], edges: list[dict]) -> list[Insight]:
    """Detect edges that reverse or skip the expected layered architecture.

    A **reverse** violation occurs when a lower-layer class depends on a
    higher-layer class (e.g. Repository → Controller).

    A **skip** violation occurs when the presentation layer directly
    depends on data-access, bypassing service and domain.  Service→Repository
    (layer 1→3) is considered normal and is *not* flagged.
    """
    class_nodes = {n["id"]: n for n in nodes if n.get("kind") == "class"}

    # Classify each class into a layer
    node_layer: dict[str, int] = {}
    for nid, n in class_nodes.items():
        layer = _classify_layer(n)
        if layer is not None:
            node_layer[nid] = layer

    # Need at least two different layers to detect violations
    if len(set(node_layer.values())) < 2:
        return []

    layer_names = ["presentation", "service", "domain", "data-access", "utility"]

    reverse_violations: list[tuple[str, str, str, str, str, str]] = []  # (src_id, tgt_id, src_name, tgt_name, src_layer_name, tgt_layer_name)
    skip_violations: list[tuple[str, str, str, str, str, str, list[str]]] = []

    seen_pairs: set[tuple[str, str]] = set()

    for e in edges:
        kind = e.get("kind")
        if kind == "contains":
            continue
        src = e["source"] if isinstance(e["source"], str) else e["source"]["id"]
        tgt = e["target"] if isinstance(e["target"], str) else e["target"]["id"]

        if src not in node_layer or tgt not in node_layer:
            continue

        pair_key = (src, tgt)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        src_layer = node_layer[src]
        tgt_layer = node_layer[tgt]
        # Utility layer (4) is cross-cutting — skip it
        if src_layer >= 4 or tgt_layer >= 4:
            continue

        src_name = class_nodes[src]["name"]
        tgt_name = class_nodes[tgt]["name"]
        src_ln = layer_names[src_layer] if src_layer < len(layer_names) else f"layer-{src_layer}"
        tgt_ln = layer_names[tgt_layer] if tgt_layer < len(layer_names) else f"layer-{tgt_layer}"

        # Reverse dependency: data-access → presentation/service, or domain → presentation
        if src_layer > tgt_layer:
            # data-access → domain is normal (repositories use domain models), skip it
            if src_layer == 3 and tgt_layer == 2:
                continue
            reverse_violations.append((src, tgt, src_name, tgt_name, src_ln, tgt_ln))

        # Skip-layer: only flag presentation → data-access (skipping 2 layers)
        # Service→Repository is a standard pattern, not a violation
        elif tgt_layer - src_layer > 1 and src_layer == 0:
            skipped = [
                layer_names[i] if i < len(layer_names) else f"layer-{i}"
                for i in range(src_layer + 1, tgt_layer)
            ]
            skip_violations.append((src, tgt, src_name, tgt_name, src_ln, tgt_ln, skipped))

    insights: list[Insight] = []

    # Emit individual reverse-violation insights (these are important)
    for src_id, tgt_id, src_name, tgt_name, src_ln, tgt_ln in reverse_violations:
        insights.append(Insight(
            severity="warning",
            title=f"Layer violation: {src_name} → {tgt_name}",
            description=(
                f"{src_name} ({src_ln}) depends on {tgt_name} "
                f"({tgt_ln}) — this reverses the expected "
                f"dependency direction."
            ),
            affected_node_ids=[src_id, tgt_id],
            rule_name="layer_violation",
        ))

    # Consolidate skip-violations into a summary when there are many
    if len(skip_violations) > 3:
        all_ids: list[str] = []
        for sv in skip_violations:
            all_ids.extend([sv[0], sv[1]])
        unique_ids = list(dict.fromkeys(all_ids))  # preserve order, dedupe
        sources = sorted(set(sv[2] for sv in skip_violations))
        insights.append(Insight(
            severity="info",
            title=f"Layer skips: {len(skip_violations)} presentation → data-access edges",
            description=(
                f"Classes in the presentation layer ({', '.join(sources[:4])}"
                f"{'…' if len(sources) > 4 else ''}) directly depend on "
                f"data-access classes, bypassing service/domain. "
                f"Consider routing through intermediate layers."
            ),
            affected_node_ids=unique_ids[:10],
            rule_name="layer_violation",
        ))
    else:
        for src_id, tgt_id, src_name, tgt_name, src_ln, tgt_ln, skipped in skip_violations:
            insights.append(Insight(
                severity="info",
                title=f"Layer skip: {src_name} → {tgt_name}",
                description=(
                    f"{src_name} ({src_ln}) directly depends on "
                    f"{tgt_name} ({tgt_ln}), skipping "
                    f"{', '.join(skipped)}."
                ),
                affected_node_ids=[src_id, tgt_id],
                rule_name="layer_violation",
            ))

    return insights


# ── Rule 6: Architecture pattern inference ──────────────────────────

# File-name / class-name patterns associated with each architecture style
_PATTERN_SIGNALS: dict[str, list[str]] = {
    "mvc": ["controller", "view", "model", "template", "viewmodel"],
    "layered": ["service", "repository", "repo", "dao", "entity",
                "domain", "handler", "usecase", "gateway"],
    "hexagonal": ["port", "adapter", "inbound", "outbound",
                  "driven", "driving", "infrastructure"],
    "event_driven": ["event", "listener", "subscriber", "publisher",
                     "emitter", "handler", "command", "saga",
                     "eventbus", "dispatcher", "message"],
    "microkernel": ["plugin", "extension", "kernel", "core",
                    "registry", "module", "hook"],
    "pipeline": ["pipeline", "stage", "filter", "pipe",
                 "transformer", "step", "chain"],
}

_PATTERN_LABELS: dict[str, str] = {
    "mvc": "MVC (Model-View-Controller)",
    "layered": "Layered Architecture",
    "hexagonal": "Hexagonal / Ports & Adapters",
    "event_driven": "Event-Driven Architecture",
    "microkernel": "Microkernel / Plugin Architecture",
    "pipeline": "Pipeline / Pipes-and-Filters",
}


def _detect_architecture_pattern(nodes: list[dict], edges: list[dict]) -> list[Insight]:
    """Infer the dominant architectural pattern from naming conventions and
    dependency structure."""
    class_nodes = [n for n in nodes if n.get("kind") == "class"]
    if not class_nodes:
        return []

    # Collect all name/file signals
    all_tokens: list[str] = []
    for n in class_nodes:
        name_lower = (n.get("name") or "").lower()
        # Split CamelCase and underscores into tokens
        tokens = re.sub(r"([a-z])([A-Z])", r"\1 \2", name_lower)
        tokens = re.sub(r"[_\-./]", " ", tokens).lower().split()
        all_tokens.extend(tokens)

        file_stem = os.path.splitext(os.path.basename(n.get("file_path") or ""))[0].lower()
        file_tokens = re.sub(r"[_\-./]", " ", file_stem).split()
        all_tokens.extend(file_tokens)

    # Score each pattern
    pattern_scores: dict[str, float] = defaultdict(float)
    token_set = set(all_tokens)
    for pattern, signals in _PATTERN_SIGNALS.items():
        for signal in signals:
            # Exact token match
            if signal in token_set:
                pattern_scores[pattern] += 2.0
            # Substring match in any token
            for tok in all_tokens:
                if signal in tok and signal != tok:
                    pattern_scores[pattern] += 0.5

    if not pattern_scores:
        return []

    # Normalise by number of class nodes for fair comparison
    total_classes = max(len(class_nodes), 1)
    for p in pattern_scores:
        pattern_scores[p] /= total_classes

    # Rank patterns
    ranked = sorted(pattern_scores.items(), key=lambda x: -x[1])
    top_pattern, top_score = ranked[0]

    # Minimum threshold: at least some signal per class on average
    if top_score < 0.3:
        return []

    insights: list[Insight] = []

    label = _PATTERN_LABELS.get(top_pattern, top_pattern)
    confidence = "high" if top_score > 1.5 else "medium" if top_score > 0.7 else "low"

    description = f"This codebase most closely resembles a {label} pattern ({confidence} confidence)."

    # Add secondary patterns if close in score
    if len(ranked) > 1:
        secondary = [(p, s) for p, s in ranked[1:] if s >= top_score * 0.5]
        if secondary:
            secondary_labels = [_PATTERN_LABELS.get(p, p) for p, _ in secondary[:2]]
            description += f" Also shows traits of: {', '.join(secondary_labels)}."

    # Layer check: verify dependency direction supports the identified pattern
    if top_pattern in ("layered", "mvc"):
        layer_count = 0
        for n in class_nodes:
            layer = _classify_layer(n)
            if layer is not None:
                layer_count += 1
        if layer_count > 0:
            coverage = layer_count / total_classes
            if coverage > 0.5:
                description += f" Layer classification covers {layer_count}/{total_classes} classes ({coverage:.0%})."

    all_ids = [n["id"] for n in class_nodes[:5]]  # Reference a few representative nodes
    insights.append(Insight(
        severity="info",
        title=f"Architecture: {label}",
        description=description,
        affected_node_ids=all_ids,
        rule_name="architecture_pattern",
    ))

    return insights


# ── Orchestrator ────────────────────────────────────────────────────

def detect_insights(graph_dict: dict) -> list[dict]:
    """Run all pattern-detection rules against a graph dict.

    Args:
        graph_dict: dict with "nodes" and "edges" lists (as returned by
                    ``Graph.to_dict()``).

    Returns:
        List of insight dicts, sorted by severity (error > warning > info).
    """
    nodes = graph_dict.get("nodes", [])
    edges = graph_dict.get("edges", [])

    all_insights: list[Insight] = []
    all_insights.extend(_detect_circular_dependencies(nodes, edges))
    all_insights.extend(_detect_god_classes(nodes, edges))
    all_insights.extend(_detect_orphan_modules(nodes, edges))
    all_insights.extend(_detect_hub_classes(nodes, edges))
    all_insights.extend(_detect_layer_violations(nodes, edges))
    all_insights.extend(_detect_architecture_pattern(nodes, edges))

    # Sort: error first, then warning, then info
    _severity_order = {"error": 0, "warning": 1, "info": 2}
    all_insights.sort(key=lambda i: (_severity_order.get(i.severity, 9), i.title))

    return [_insight_to_dict(i) for i in all_insights]

