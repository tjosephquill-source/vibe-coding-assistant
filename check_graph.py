from backend.analyzer import analyze_codebase, NodeKind
from collections import Counter

g = analyze_codebase("mock_codebase")
kinds = Counter(n.kind for n in g.nodes)

print("=== Node kinds ===")
for k, v in sorted(kinds.items(), key=lambda x: x[0].value):
    print(f"  {k.value}: {v}")

print(f"\n=== Total edges: {len(g.edges)} ===")
ekinds = Counter(e.kind for e in g.edges)
for k, v in sorted(ekinds.items(), key=lambda x: x[0].value):
    print(f"  {k.value}: {v}")

assert all(k.value != "function" for k in kinds), "FAIL: function nodes still present"
assert all(k.value != "module" for k in kinds), "FAIL: module/file nodes still present"
print("\nPASS: no function or module nodes in graph")
