"""
Hierarchical description generator — bottom-up LLM-powered descriptions.

For every node in the graph (leaf classes/functions up through island-chain
groups to the root), this module:
  1. Reads the source code for leaf nodes
  2. Generates a rich description via GPT (what it does, inputs, outputs,
     role in the architecture)
  3. Synthesises parent descriptions from their children's descriptions
  4. Caches everything to disk keyed by content hash so only changed
     nodes are regenerated
"""

import os
import json
import hashlib
import logging
import asyncio
from pathlib import Path

from backend.analyzer import _ensure_cache_dir

logger = logging.getLogger(__name__)

# ── Disk cache ──────────────────────────────────────────────────────

_DESC_CACHE_VERSION = 6  # bump to force full regeneration


def _desc_cache_path(root_dir: str) -> Path:
    """Return the path to the description cache JSON for a codebase."""
    dir_hash = hashlib.md5(os.path.abspath(root_dir).encode()).hexdigest()[:12]
    return _ensure_cache_dir() / f"descriptions_{dir_hash}.json"


def _load_desc_cache(root_dir: str) -> dict:
    """Load cached descriptions from disk.

    Returns: {node_id: {hash, description, children_ids, level}} or empty dict.
    """
    path = _desc_cache_path(root_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("_version") != _DESC_CACHE_VERSION:
            return {}
        return data.get("descriptions", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_desc_cache(root_dir: str, descriptions: dict) -> None:
    """Persist description cache to disk."""
    path = _desc_cache_path(root_dir)
    payload = {"_version": _DESC_CACHE_VERSION, "descriptions": descriptions}
    try:
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write description cache: %s", e)


# ── Content hashing ─────────────────────────────────────────────────

def _hash_source(source: str) -> str:
    """Hash source code content."""
    return hashlib.sha256(source.encode()).hexdigest()[:20]


def _hash_children(children_hashes: list[str]) -> str:
    """Hash a parent node based on its children's hashes (order-independent)."""
    combined = "|".join(sorted(children_hashes))
    return hashlib.sha256(combined.encode()).hexdigest()[:20]


def _read_source_snippet(root_dir: str, file_path: str,
                         line_start: int, line_end: int,
                         max_chars: int = 6000) -> str:
    """Read source code for a node. Returns empty string on failure."""
    if not file_path or not line_start or not line_end:
        return ""
    full = os.path.normpath(os.path.join(root_dir, file_path))
    if not os.path.isfile(full):
        return ""
    try:
        lines = Path(full).read_text(encoding="utf-8", errors="replace").splitlines()
        snippet = "\n".join(lines[max(0, line_start - 1):line_end])
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars] + "\n… [truncated]"
        return snippet
    except OSError:
        return ""


# ── Build the node hierarchy ────────────────────────────────────────

def _build_hierarchy(graph_dict: dict) -> tuple[dict, dict, list]:
    """Build parent→children mapping from graph data.

    Returns:
      - node_map: {id: node_dict}
      - children_map: {parent_id: [child_ids]}
      - leaf_ids: list of nodes with no children (bottom of hierarchy)
    """
    all_nodes = {}
    for n in graph_dict.get("nodes", []):
        all_nodes[n["id"]] = n
    for n in graph_dict.get("intermediate_nodes", []):
        all_nodes[n["id"]] = n

    children_map: dict[str, list[str]] = {}
    has_parent: set[str] = set()

    for nid, n in all_nodes.items():
        member_ids = n.get("member_ids") or []
        if member_ids:
            children_map[nid] = [
                mid for mid in member_ids if mid in all_nodes
            ]
            for mid in children_map[nid]:
                has_parent.add(mid)

    # Also check for CONTAINS edges (class → method)
    for e in (graph_dict.get("edges", []) +
              graph_dict.get("intermediate_edges", [])):
        kind = e.get("kind", "")
        if kind == "contains":
            src = e["source"] if isinstance(e["source"], str) else e["source"]["id"]
            tgt = e["target"] if isinstance(e["target"], str) else e["target"]["id"]
            if src in all_nodes and tgt in all_nodes:
                if src not in children_map:
                    children_map[src] = []
                if tgt not in children_map[src]:
                    children_map[src].append(tgt)
                    has_parent.add(tgt)

    leaf_ids = [nid for nid in all_nodes if nid not in children_map
                or len(children_map.get(nid, [])) == 0]

    return all_nodes, children_map, leaf_ids


def _topo_levels(all_nodes: dict, children_map: dict) -> list[list[str]]:
    """Return nodes grouped into bottom-up levels.

    Level 0 = leaves (no children).
    Level 1 = nodes whose children are all leaves.
    etc.

    This ensures we always describe children before their parents.
    """
    node_level: dict[str, int] = {}

    def _get_level(nid: str) -> int:
        if nid in node_level:
            return node_level[nid]
        kids = children_map.get(nid, [])
        if not kids:
            node_level[nid] = 0
            return 0
        lvl = max(_get_level(kid) for kid in kids if kid in all_nodes) + 1
        node_level[nid] = lvl
        return lvl

    for nid in all_nodes:
        _get_level(nid)

    # Group by level
    max_level = max(node_level.values()) if node_level else 0
    levels: list[list[str]] = [[] for _ in range(max_level + 1)]
    for nid, lvl in node_level.items():
        levels[lvl].append(nid)

    return levels


# ── LLM description generation ──────────────────────────────────────

def _build_edge_context(all_nodes: dict, graph_dict: dict) -> dict[str, list[str]]:
    """Build a map of node_id → list of relationship strings.

    Collects calls, inherits, imports, instantiates, uses_type edges
    from all edge lists in the graph.  Skips 'contains' (parent-child
    is handled structurally).
    """
    ctx: dict[str, list[str]] = {}
    all_edges = (graph_dict.get("edges", []) +
                 graph_dict.get("intermediate_edges", []))

    for e in all_edges:
        kind = e.get("kind", "")
        if kind == "contains":
            continue
        src = e["source"] if isinstance(e["source"], str) else e["source"].get("id", "")
        tgt = e["target"] if isinstance(e["target"], str) else e["target"].get("id", "")
        src_name = all_nodes.get(src, {}).get("name", src)
        tgt_name = all_nodes.get(tgt, {}).get("name", tgt)

        if kind == "calls":
            ctx.setdefault(src, []).append(f"calls {tgt_name}")
            ctx.setdefault(tgt, []).append(f"called by {src_name}")
        elif kind == "inherits":
            ctx.setdefault(src, []).append(f"inherits from {tgt_name}")
            ctx.setdefault(tgt, []).append(f"inherited by {src_name}")
        elif kind == "imports":
            ctx.setdefault(src, []).append(f"imports {tgt_name}")
        elif kind == "instantiates":
            ctx.setdefault(src, []).append(f"instantiates {tgt_name}")
            ctx.setdefault(tgt, []).append(f"instantiated by {src_name}")
        elif kind == "uses_type":
            ctx.setdefault(src, []).append(f"uses type {tgt_name}")

    # Deduplicate
    for nid in ctx:
        ctx[nid] = list(dict.fromkeys(ctx[nid]))

    return ctx


def _build_leaf_prompt(node: dict, source: str, edge_lines: list[str] | None = None) -> str:
    """Build an LLM prompt for a leaf node (class, function, method)."""
    kind = node.get("kind", "unknown")
    name = node.get("name", "unknown")
    file_path = node.get("file_path", "")

    header = f"'{name}' — {kind}"
    if file_path:
        header += f" in {file_path}"

    parts = [header, ""]

    if source:
        parts.append(f"```\n{source}\n```")
    else:
        meta_bits = []
        if node.get("bases"):
            meta_bits.append(f"Bases: {', '.join(node['bases'])}")
        if node.get("methods"):
            meta_bits.append(f"Methods: {', '.join(node['methods'][:20])}")
        if node.get("variables"):
            var_strs = [f"{v['name']}: {v.get('type', '?')}" for v in node["variables"][:15]]
            meta_bits.append(f"Vars: {', '.join(var_strs)}")
        if meta_bits:
            parts.append("\n".join(meta_bits))
        else:
            parts.append("(No source available.)")

    if edge_lines:
        parts.append("\nRelationships: " + "; ".join(edge_lines[:12]))

    parts.append(
        "\nDescribe ONLY the non-obvious specifics: "
        "what concrete work it does, what it takes in (param names and types), "
        "what it returns, and what it depends on. "
        "If this code renders UI or handles user interaction, describe what the "
        "user SEES and DOES — not just what the code does internally. "
        "Do NOT explain what a class/function/__init__/method is in general. "
        "Do NOT restate the function signature in prose."
    )

    return "\n".join(parts)


def _build_parent_prompt(node: dict, children_descs: list[dict],
                         edge_lines: list[str] | None = None) -> str:
    """Build an LLM prompt for a parent node that synthesises children."""
    kind = node.get("kind", "group")
    name = node.get("name", "unknown")

    children_text = []
    for cd in children_descs:
        child_name = cd.get("name", "?")
        child_kind = cd.get("kind", "?")
        child_desc = cd.get("description", "(no description)")
        children_text.append(f"  • {child_name} ({child_kind}): {child_desc}")

    parts = [
        f"'{name}' ({kind}) contains:\n",
        "\n".join(children_text),
    ]

    if edge_lines:
        parts.append("\nExternal relationships: " + "; ".join(edge_lines[:15]))

    parts.append(
        "\n\nUsing the component descriptions above as ground truth, describe "
        "this group's concrete purpose: what capability it provides, how data "
        "flows between its components, and what external callers use it for. "
        "If any components are user-facing (UI, visualization, interactive), "
        "describe what the user sees and does, not just the internal mechanics. "
        "Do NOT list the components — the reader already sees them. "
        "Focus on how they combine into a larger function."
    )

    return "\n".join(parts)


def _build_root_prompt(top_level_descs: list[dict], edges: list[dict],
                       all_nodes: dict) -> str:
    """Build an LLM prompt for the root-level codebase overview."""
    components = []
    for td in top_level_descs:
        components.append(
            f"  • {td.get('name', '?')} ({td.get('kind', '?')}): {td.get('description', '(no description)')}"
        )

    edge_summary = []
    for e in edges[:30]:
        src = e.get("source", "?")
        tgt = e.get("target", "?")
        ekind = e.get("kind", "?")
        if isinstance(src, dict):
            src = src.get("id", "?")
        if isinstance(tgt, dict):
            tgt = tgt.get("id", "?")
        src_name = all_nodes.get(src, {}).get("name", src)
        tgt_name = all_nodes.get(tgt, {}).get("name", tgt)
        edge_summary.append(f"  {src_name} —[{ekind}]→ {tgt_name}")

    parts = [
        "Top-level components:\n",
        "\n".join(components),
    ]
    if edge_summary:
        parts.append("\nRelationships:\n" + "\n".join(edge_summary))

    parts.append(
        "\n\nUsing ALL the component descriptions above, answer these questions "
        "in order as a single flowing passage:\n"
        "1. What is this software? State its purpose in one sentence as if writing "
        "the first line of a README — what does a user USE it for?\n"
        "2. What are the key capabilities it provides to its users? "
        "Describe what the user SEES and DOES, not just internal mechanics.\n"
        "3. How does data enter the system, get processed, and produce output? "
        "Name the actual entry points and outputs.\n"
        "4. What architecture pattern does it follow?\n\n"
        "IMPORTANT: Synthesise the OVERALL purpose from all components together. "
        "Do NOT describe each component separately. Many components are internal "
        "infrastructure (UI frameworks, layout engines, HTTP servers) — those are "
        "HOW it works, not WHAT it does. Focus on WHAT the software does for its user "
        "and use the software's own vocabulary for its features and UI elements."
    )
    return "\n".join(parts)


SYSTEM_PROMPT = (
    "You are a senior engineer writing internal documentation. "
    "The reader is a developer who already understands programming concepts — "
    "never explain what a class, function, method, __init__, constructor, or "
    "design pattern IS in general terms. "
    "Only state facts specific to THIS code: concrete behavior, specific "
    "parameter names and types, actual return values, real dependencies by name. "
    "No filler, no hedging ('likely', 'probably', 'appears to'), no padding. "
    "Every sentence must contain information the reader cannot infer from the "
    "function signature alone. "
    "2-4 sentences for leaf nodes. 3-5 sentences for groups. "
    "5-8 sentences for the root overview. "
    "Plain prose, no markdown, no bullets, no headers."
)


async def _generate_description(prompt: str, api_key: str,
                                max_tokens: int = 300,
                                client=None,
                                semaphore=None) -> str:
    """Call the LLM to generate a description."""
    from openai import AsyncOpenAI

    if client is None:
        client = AsyncOpenAI(api_key=api_key)

    async def _call():
        try:
            resp = await client.chat.completions.create(
                model=os.environ.get("LLM_MODEL", "gpt-5.4-mini"),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_completion_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error("LLM description generation failed: %s", e)
            return f"Description generation failed: {e}"

    if semaphore is not None:
        async with semaphore:
            return await _call()
    return await _call()


# ── Main orchestrator ───────────────────────────────────────────────

async def generate_hierarchical_descriptions(
    root_dir: str,
    graph_dict: dict,
    api_key: str,
    force: bool = False,
    progress_callback=None,
) -> dict:
    """Generate bottom-up descriptions for every node in the graph.

    Walks the hierarchy from leaves to root:
      1. Leaf nodes get descriptions from their source code.
      2. Parent nodes get descriptions synthesised from children.
      3. A root-level description covers the entire codebase.

    Each node's description is cached by a hash of its content (source for
    leaves, children hashes for parents).  When source code changes, only
    the affected leaf + its ancestors are regenerated.

    Args:
        root_dir: Absolute path to the codebase.
        graph_dict: The full graph dict (with intermediate_nodes/edges).
        api_key: OpenAI API key.
        force: If True, regenerate all descriptions ignoring cache.
        progress_callback: Optional async fn(done, total, node_name) for progress.

    Returns:
        {
          node_id: {
            "name": str,
            "kind": str,
            "description": str,
            "hash": str,
            "level": int,        # 0=leaf, higher=more abstract
            "children_ids": [],
          },
          "__root__": { ... root-level overview ... }
        }
    """
    all_nodes, children_map, leaf_ids = _build_hierarchy(graph_dict)
    levels = _topo_levels(all_nodes, children_map)

    # Build edge context so every node knows its callers/callees/etc.
    edge_ctx = _build_edge_context(all_nodes, graph_dict)

    # Load existing cache
    cache = {} if force else _load_desc_cache(root_dir)

    # Track which nodes need regeneration
    descriptions: dict[str, dict] = {}
    node_hashes: dict[str, str] = {}
    total_to_generate = 0
    generated_count = 0

    # ── Phase 1: compute hashes bottom-up to find what's stale ──────
    for level_idx, level_nodes in enumerate(levels):
        for nid in level_nodes:
            n = all_nodes.get(nid)
            if not n:
                continue
            kids = children_map.get(nid, [])
            if not kids:
                # Leaf: hash from source code
                source = _read_source_snippet(
                    root_dir, n.get("file_path", ""),
                    n.get("line_start", 0), n.get("line_end", 0),
                )
                if source:
                    node_hashes[nid] = _hash_source(source)
                else:
                    # No source — hash from metadata
                    meta = f"{n.get('name', '')}:{n.get('kind', '')}:{n.get('file_path', '')}"
                    node_hashes[nid] = _hash_source(meta)
            else:
                # Parent: hash from children's hashes
                child_hashes = [
                    node_hashes.get(kid, "missing") for kid in kids
                ]
                node_hashes[nid] = _hash_children(child_hashes)

            # Check if cache is valid
            cached_entry = cache.get(nid)
            if cached_entry and cached_entry.get("hash") == node_hashes[nid]:
                descriptions[nid] = cached_entry
            else:
                total_to_generate += 1

    # Count root too — top_ids are nodes that are not children of anything
    all_children = set()
    for kids in children_map.values():
        all_children.update(kids)
    top_ids = [nid for nid in all_nodes if nid not in all_children]
    if not top_ids:
        top_ids = [n["id"] for n in graph_dict.get("nodes", [])]

    top_hashes = [node_hashes.get(tid, "x") for tid in top_ids]
    root_hash = _hash_children(top_hashes)
    cached_root = cache.get("__root__")
    if not (cached_root and cached_root.get("hash") == root_hash):
        total_to_generate += 1

    if total_to_generate == 0:
        # Everything is cached and current
        descriptions["__root__"] = cached_root or cache.get("__root__", {})
        return descriptions

    # Report total immediately so callers can show progress
    if progress_callback:
        await progress_callback(0, total_to_generate, "preparing…")

    # ── Phase 2: generate descriptions bottom-up (concurrent) ─────
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    semaphore = asyncio.Semaphore(15)  # max concurrent LLM calls

    for level_idx, level_nodes in enumerate(levels):
        # Collect nodes that need generation at this level
        stale_at_level = []
        for nid in level_nodes:
            if nid in descriptions:
                continue
            n = all_nodes.get(nid)
            if not n:
                continue
            stale_at_level.append(nid)

        if not stale_at_level:
            continue

        logger.info("Descriptions level %d: %d nodes to generate",
                    level_idx, len(stale_at_level))

        async def _gen_one(nid, _level_idx=level_idx):
            nonlocal generated_count
            n = all_nodes[nid]
            kids = children_map.get(nid, [])

            if not kids:
                source = _read_source_snippet(
                    root_dir, n.get("file_path", ""),
                    n.get("line_start", 0), n.get("line_end", 0),
                )
                prompt = _build_leaf_prompt(n, source, edge_ctx.get(nid))
                desc_text = await _generate_description(
                    prompt, api_key, max_tokens=200,
                    client=client, semaphore=semaphore,
                )
            else:
                children_descs = []
                for kid in kids:
                    kid_entry = descriptions.get(kid, {})
                    kid_node = all_nodes.get(kid, {})
                    children_descs.append({
                        "name": kid_node.get("name", kid_entry.get("name", kid)),
                        "kind": kid_node.get("kind", kid_entry.get("kind", "?")),
                        "description": kid_entry.get("description",
                                                     "(no description)"),
                    })
                prompt = _build_parent_prompt(n, children_descs, edge_ctx.get(nid))
                desc_text = await _generate_description(
                    prompt, api_key, max_tokens=300,
                    client=client, semaphore=semaphore,
                )

            entry = {
                "name": n.get("name", "unknown"),
                "kind": n.get("kind", "unknown"),
                "description": desc_text,
                "hash": node_hashes.get(nid, ""),
                "level": _level_idx,
                "children_ids": kids,
            }
            descriptions[nid] = entry
            generated_count += 1
            if progress_callback:
                await progress_callback(
                    generated_count, total_to_generate,
                    n.get("name", nid),
                )
            return nid, entry

        # Run all stale nodes at this level concurrently
        tasks = [asyncio.ensure_future(_gen_one(nid)) for nid in stale_at_level]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("Description generation error: %s", result)

        # Save incremental progress after each level
        _save_desc_cache(root_dir, descriptions)

    # ── Phase 3: generate root-level overview ───────────────────────
    if not (cached_root and cached_root.get("hash") == root_hash):
        top_descs = []
        for tid in top_ids:
            entry = descriptions.get(tid, {})
            nd = all_nodes.get(tid, {})
            top_descs.append({
                "name": nd.get("name", entry.get("name", tid)),
                "kind": nd.get("kind", entry.get("kind", "?")),
                "description": entry.get("description",
                                         "No description available."),
            })

        edges = graph_dict.get("edges", [])
        prompt = _build_root_prompt(top_descs, edges, all_nodes)
        root_desc = await _generate_description(
            prompt, api_key, max_tokens=400,
            client=client,
        )

        descriptions["__root__"] = {
            "name": "Codebase Overview",
            "kind": "root",
            "description": root_desc,
            "hash": root_hash,
            "level": len(levels),
            "children_ids": top_ids,
        }

        generated_count += 1
        if progress_callback:
            await progress_callback(
                generated_count, total_to_generate, "Root Overview",
            )
    else:
        descriptions["__root__"] = cached_root

    # ── Phase 4: persist to disk ────────────────────────────────────
    _save_desc_cache(root_dir, descriptions)

    return descriptions

