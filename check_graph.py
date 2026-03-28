from backend.analyzer import (
    analyze_codebase, abstract_graph, NodeKind, EdgeKind,
    _build_adjacency, _find_communities, _count_bridge_edges, _count_internal_edges,
    _is_test_node, _find_loose_node_ids,
)
from collections import Counter

g = analyze_codebase("mock_codebase")
kinds = Counter(n.kind for n in g.nodes)

print("=== Raw Graph ===")
print(f"  Nodes: {len(g.nodes)}")
for k, v in sorted(kinds.items(), key=lambda x: x[0].value):
    print(f"    {k.value}: {v}")

print(f"\n=== Total edges: {len(g.edges)} ===")
ekinds = Counter(e.kind for e in g.edges)
for k, v in sorted(ekinds.items(), key=lambda x: x[0].value):
    print(f"    {k.value}: {v}")

assert all(k.value != "function" for k in kinds), "FAIL: function nodes still present"
assert all(k.value != "module" for k in kinds), "FAIL: module/file nodes still present"
print("\nPASS: no function or module nodes in graph")


# Show loose nodes
loose = _find_loose_node_ids(g)
node_by_id = {n.id: n for n in g.nodes}
print(f"\n=== Loose nodes (no non-contains edges): {len(loose)} ===")
for nid in sorted(loose):
    n = node_by_id.get(nid)
    if n:
        print(f"  {n.name} ({n.file_path}) {'[TEST]' if _is_test_node(n) else ''}")

# Debug: show class-level connectivity
class_ids = {n.id for n in g.nodes if n.kind == NodeKind.CLASS}
class_by_id = {n.id: n for n in g.nodes if n.kind == NodeKind.CLASS}
adj = _build_adjacency(class_ids, g.edges)
print(f"\n=== Class-level adjacency (non-contains edges) ===")
for nid in sorted(class_ids):
    n = class_by_id[nid]
    neighbors = adj.get(nid, set())
    neighbor_names = sorted(class_by_id[nb].name for nb in neighbors if nb in class_by_id)
    print(f"  {n.name}: {neighbor_names}")

# Debug: show what communities are detected
print(f"\n=== Community detection (max_bridge=2, min_size=3) ===")
communities = _find_communities(class_ids, g.edges, max_bridge_edges=10, min_community_size=3)
for i, comm in enumerate(communities):
    names = sorted(class_by_id[nid].name for nid in comm if nid in class_by_id)
    bridge = _count_bridge_edges(comm, class_ids, g.edges)
    internal = _count_internal_edges(comm, g.edges)
    print(f"  Community {i+1}: {names}")
    print(f"    Internal edges: {internal}, Bridge edges: {bridge}")

# Test abstraction
ag = abstract_graph(g)
akinds = Counter(n.kind for n in ag.nodes)

print(f"\n=== Abstracted Graph ===")
print(f"  Nodes: {len(ag.nodes)}")
for k, v in sorted(akinds.items(), key=lambda x: x[0].value):
    print(f"    {k.value}: {v}")

print(f"  Edges: {len(ag.edges)}")

groups = [n for n in ag.nodes if n.kind == NodeKind.GROUP]
metaclasses = [n for n in ag.nodes if n.kind == NodeKind.ISLAND_CHAIN]

for sg in groups:
    print(f"\n  Group: {sg.name}")
    print(f"    Members: {sg.members}")
    print(f"    Docstring: {sg.docstring}")

for mc in metaclasses:
    print(f"\n  Island Chain: {mc.name}")
    print(f"    Members: {mc.members}")
    print(f"    Member IDs: {mc.member_ids}")
    print(f"    Depth: {mc.depth}")

if not metaclasses and not groups:
    print("\n  (No groups formed)")
