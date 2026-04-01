"""LLM Chat handler — OpenAI integration with codebase tool-use."""

import os
import json
import difflib
from typing import AsyncGenerator

from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Tool definitions for OpenAI function-calling
# ---------------------------------------------------------------------------

# ── Read-only tools (shared by both Ask and Agent modes) ─────────────

_READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the analysed codebase. Returns the full source text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the codebase (e.g. 'models.py' or 'core/utils.py').",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all code files in the analysed codebase, with their relative paths.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a text pattern across all files in the codebase. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text or substring to search for (case-insensitive).",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_graph_summary",
            "description": "Get a high-level summary of the codebase architecture graph: node counts by kind, edge counts by kind, and the top-level structure.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_architecture_overview",
            "description": "Get a rich, LLM-generated hierarchical description of the codebase architecture. Returns the root overview plus descriptions of top-level components and their children. Use this to understand what the codebase does, its key subsystems, and how data flows through it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "detail_level": {
                        "type": "string",
                        "enum": ["high", "medium", "full"],
                        "description": "Level of detail: 'high' = root + top-level components only, 'medium' = include children of top-level, 'full' = all available descriptions.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_to_node",
            "description": "Navigate the UI to focus on a specific class, method, or module in the architecture graph. Use this when the user asks to 'show me' or 'go to' a node.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_name": {
                        "type": "string",
                        "description": "The name of the class, method, or module to navigate to.",
                    }
                },
                "required": ["node_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file_in_editor",
            "description": "Open a file in the code editor panel, optionally highlighting specific lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the codebase.",
                    },
                    "line_start": {
                        "type": "integer",
                        "description": "Start line to highlight (1-based, optional).",
                    },
                    "line_end": {
                        "type": "integer",
                        "description": "End line to highlight (1-based, optional).",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

# ── Write tools (Agent mode only) ───────────────────────────────────

_WRITE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or completely overwrite an existing file with the given content. Use this when you need to create a new file or rewrite a file from scratch. For targeted edits to existing files, prefer edit_file instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the codebase.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Make a targeted edit to an existing file by replacing a specific section of text. This is safer than write_file for modifications because you only change what you need to. Always read the file first to get the exact text to replace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the codebase.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact existing text to find and replace. Must match the file content exactly (including whitespace and indentation). Include enough context lines to uniquely identify the location.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text. Can be empty string to delete the matched section.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the codebase.",
                    }
                },
                "required": ["path"],
            },
        },
    },
]

# Composed tool sets
ASK_TOOLS = _READ_TOOLS
AGENT_TOOLS = _READ_TOOLS + _WRITE_TOOLS

# ---------------------------------------------------------------------------
# Code extensions for file listing
# ---------------------------------------------------------------------------

_CODE_EXTENSIONS = {
    ".py", ".pyx", ".pxd", ".pyi",
    ".js", ".ts", ".jsx", ".tsx",
    ".java", ".kt", ".scala",
    ".c", ".h", ".cpp", ".hpp",
    ".go", ".rs", ".rb", ".php",
    ".cs", ".swift", ".m", ".mm",
}

_IGNORE_DIRS = {
    "__pycache__", ".git", ".svn", ".hg", "node_modules",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs",
}

# Module-level codebase root — set by the server before each chat call.
# Falls back to legacy mock_codebase directories if not explicitly set.
_codebase_root: str | None = None
# Module-level hierarchical descriptions — set by the server.
_hier_descriptions: dict | None = None


def set_codebase_root(path: str | None) -> None:
    """Set the codebase root used by all chat tool implementations."""
    global _codebase_root
    _codebase_root = path


def set_hier_descriptions(descriptions: dict | None) -> None:
    """Provide the hierarchical descriptions for the get_architecture_overview tool."""
    global _hier_descriptions
    _hier_descriptions = descriptions


def _resolve_codebase_root() -> str:
    """Return the absolute path to the target codebase."""
    if _codebase_root:
        return _codebase_root
    # Legacy fallback
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mock2 = os.path.join(project_root, "mock_codebase2")
    target = mock2 if os.path.isdir(mock2) else os.path.join(project_root, "mock_codebase")
    return os.path.abspath(target)


def _safe_path(root: str, rel: str) -> str | None:
    """Resolve *rel* inside *root* and make sure it doesn't escape."""
    requested = os.path.normpath(os.path.join(root, rel))
    if not requested.startswith(root + os.sep) and requested != root:
        return None
    return requested


# ---------------------------------------------------------------------------
# Tool implementations (all synchronous — called from async via tool dispatch)
# ---------------------------------------------------------------------------

def _tool_read_file(path: str) -> str:
    root = _resolve_codebase_root()
    full = _safe_path(root, path)
    if full is None:
        return json.dumps({"error": "Invalid path — outside codebase."})
    if not os.path.isfile(full):
        return json.dumps({"error": f"File not found: {path}"})
    try:
        content = open(full, encoding="utf-8", errors="replace").read()
        # Truncate very large files
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n… [truncated — file too large]"
        return json.dumps({"path": path, "content": content})
    except OSError as e:
        return json.dumps({"error": str(e)})


def _tool_list_files() -> str:
    root = _resolve_codebase_root()
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for f in sorted(filenames):
            ext = os.path.splitext(f)[1].lower()
            if ext in _CODE_EXTENSIONS:
                files.append(os.path.relpath(os.path.join(dirpath, f), root))
    return json.dumps({"files": files, "count": len(files)})


def _tool_search_code(query: str) -> str:
    root = _resolve_codebase_root()
    matches = []
    q_lower = query.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _CODE_EXTENSIONS:
                continue
            full = os.path.join(dirpath, fname)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if q_lower in line.lower():
                            matches.append({
                                "file": os.path.relpath(full, root),
                                "line": i,
                                "text": line.rstrip()[:200],
                            })
                            if len(matches) >= 50:
                                return json.dumps({"matches": matches, "truncated": True})
            except OSError:
                continue
    return json.dumps({"matches": matches, "truncated": False})


def _tool_write_file(path: str, content: str) -> str:
    root = _resolve_codebase_root()
    full = _safe_path(root, path)
    if full is None:
        return json.dumps({"error": "Invalid path — outside codebase."})
    try:
        existed = os.path.isfile(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        change_type = "modified" if existed else "created"
        return json.dumps({
            "status": "ok",
            "path": path,
            "bytes": len(content),
            "file_change": {"type": change_type, "path": path},
        })
    except OSError as e:
        return json.dumps({"error": str(e)})


def _tool_edit_file(path: str, old_text: str, new_text: str) -> str:
    """Surgical search-and-replace within a file."""
    root = _resolve_codebase_root()
    full = _safe_path(root, path)
    if full is None:
        return json.dumps({"error": "Invalid path — outside codebase."})
    if not os.path.isfile(full):
        return json.dumps({"error": f"File not found: {path}"})
    try:
        original = open(full, encoding="utf-8", errors="replace").read()
        if old_text not in original:
            # Try to find a close match for a helpful error
            close = difflib.get_close_matches(
                old_text[:200], [original[i:i+len(old_text)] for i in range(0, min(len(original), 5000), 50)],
                n=1, cutoff=0.6,
            )
            hint = ""
            if close:
                hint = f" Closest match starts with: {close[0][:120]!r}"
            return json.dumps({"error": f"old_text not found in {path}.{hint} Read the file first to get exact text."})

        count = original.count(old_text)
        if count > 1:
            return json.dumps({"error": f"old_text matches {count} locations in {path}. Include more context lines to uniquely identify the edit location."})

        updated = original.replace(old_text, new_text, 1)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(updated)

        # Build a compact unified diff snippet
        orig_lines = original.splitlines(keepends=True)
        new_lines = updated.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(orig_lines, new_lines, fromfile=path, tofile=path, lineterm=""))
        diff_snippet = "\n".join(diff_lines[:60])
        if len(diff_lines) > 60:
            diff_snippet += "\n… (diff truncated)"

        return json.dumps({
            "status": "ok",
            "path": path,
            "replacements": 1,
            "diff": diff_snippet,
            "file_change": {"type": "modified", "path": path},
        })
    except OSError as e:
        return json.dumps({"error": str(e)})


def _tool_delete_file(path: str) -> str:
    root = _resolve_codebase_root()
    full = _safe_path(root, path)
    if full is None:
        return json.dumps({"error": "Invalid path — outside codebase."})
    if not os.path.isfile(full):
        return json.dumps({"error": f"File not found: {path}"})
    try:
        os.remove(full)
        return json.dumps({
            "status": "ok",
            "path": path,
            "file_change": {"type": "deleted", "path": path},
        })
    except OSError as e:
        return json.dumps({"error": str(e)})


def _tool_get_graph_summary(graph_dict: dict | None) -> str:
    if not graph_dict:
        return json.dumps({"error": "No graph data available."})
    nodes = graph_dict.get("nodes", [])
    edges = graph_dict.get("edges", [])
    kind_counts = {}
    for n in nodes:
        k = n.get("kind", "unknown")
        kind_counts[k] = kind_counts.get(k, 0) + 1
    edge_kind_counts = {}
    for e in edges:
        k = e.get("kind", "unknown")
        edge_kind_counts[k] = edge_kind_counts.get(k, 0) + 1
    top_nodes = [{"name": n["name"], "kind": n["kind"]} for n in nodes[:30]]
    return json.dumps({
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "nodes_by_kind": kind_counts,
        "edges_by_kind": edge_kind_counts,
        "sample_nodes": top_nodes,
    })


# navigate_to_node and open_file_in_editor return JSON commands
# that the frontend will interpret
def _tool_navigate_to_node(node_name: str) -> str:
    return json.dumps({"action": "navigate_to_node", "node_name": node_name})


def _tool_open_file_in_editor(path: str, line_start: int | None = None, line_end: int | None = None) -> str:
    return json.dumps({"action": "open_file_in_editor", "path": path, "line_start": line_start, "line_end": line_end})


def _tool_get_architecture_overview(detail_level: str = "high") -> str:
    """Return hierarchical codebase descriptions at the requested detail level."""
    descs = _hier_descriptions
    if not descs:
        return json.dumps({"error": "No architecture descriptions available yet. They may still be generating."})

    root_entry = descs.get("__root__")
    if not root_entry:
        return json.dumps({"error": "Root description not available."})

    result: dict = {
        "codebase_overview": root_entry.get("description", ""),
    }

    # Collect top-level component IDs
    top_ids = root_entry.get("children_ids", [])

    components = []
    for tid in top_ids:
        entry = descs.get(tid)
        if not entry:
            continue
        comp: dict = {
            "id": tid,
            "name": entry.get("name", "?"),
            "kind": entry.get("kind", "?"),
            "description": entry.get("description", ""),
        }

        # For medium/full, include children descriptions
        if detail_level in ("medium", "full"):
            child_ids = entry.get("children_ids", [])
            children = []
            for cid in child_ids:
                child_entry = descs.get(cid)
                if not child_entry:
                    continue
                child: dict = {
                    "id": cid,
                    "name": child_entry.get("name", "?"),
                    "kind": child_entry.get("kind", "?"),
                    "description": child_entry.get("description", ""),
                }

                # For full detail, include grandchildren too
                if detail_level == "full":
                    gc_ids = child_entry.get("children_ids", [])
                    grandchildren = []
                    for gid in gc_ids:
                        gc_entry = descs.get(gid)
                        if gc_entry:
                            grandchildren.append({
                                "id": gid,
                                "name": gc_entry.get("name", "?"),
                                "kind": gc_entry.get("kind", "?"),
                                "description": gc_entry.get("description", ""),
                            })
                    if grandchildren:
                        child["children"] = grandchildren

                children.append(child)
            if children:
                comp["children"] = children

        components.append(comp)

    result["components"] = components
    result["total_described_nodes"] = len(descs) - 1  # exclude __root__

    # Truncate if too large
    output = json.dumps(result, indent=1)
    if len(output) > 30_000:
        output = output[:30_000] + '\n… [truncated]"}'
    return output


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch_tool(name: str, arguments: dict, graph_dict: dict | None = None) -> str:
    """Execute a tool call and return the result as a string."""
    if name == "read_file":
        return _tool_read_file(arguments["path"])
    elif name == "list_files":
        return _tool_list_files()
    elif name == "search_code":
        return _tool_search_code(arguments["query"])
    elif name == "write_file":
        return _tool_write_file(arguments["path"], arguments["content"])
    elif name == "edit_file":
        return _tool_edit_file(arguments["path"], arguments["old_text"], arguments["new_text"])
    elif name == "delete_file":
        return _tool_delete_file(arguments["path"])
    elif name == "get_graph_summary":
        return _tool_get_graph_summary(graph_dict)
    elif name == "navigate_to_node":
        return _tool_navigate_to_node(arguments["node_name"])
    elif name == "open_file_in_editor":
        return _tool_open_file_in_editor(
            arguments["path"],
            arguments.get("line_start"),
            arguments.get("line_end"),
        )
    elif name == "get_architecture_overview":
        return _tool_get_architecture_overview(arguments.get("detail_level", "high"))
    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# System prompts — Ask vs Agent mode
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_ASK = """\
You are the Vibe Coding Assistant — an AI embedded in an interactive architecture visualization tool.

You are in **Ask mode**: you answer questions about the codebase but do NOT modify any files.

You have access to the full analysed codebase and can:
• Read and search files
• Navigate the architecture graph to show specific classes/modules
• Open files in the code editor panel with line highlights
• Get a rich hierarchical architecture overview of the codebase

When the user asks about code structure, architecture patterns, dependencies, \
or specific classes — use your tools to look things up rather than guessing. \
For high-level "what does this codebase do" questions, use get_architecture_overview first.

Keep responses concise but informative. Use markdown formatting for readability. \
When you reference a class or file, offer to navigate to it or open it.
"""

_SYSTEM_PROMPT_AGENT = """\
You are the Vibe Coding Assistant — an AI embedded in an interactive architecture visualization tool.

You are in **Agent mode**: you can read, create, edit, and delete files in the codebase.

You have access to the full analysed codebase and can:
• Read, search, create, edit, and delete files
• Navigate the architecture graph to show specific classes/modules
• Open files in the code editor panel with line highlights
• Get a rich hierarchical architecture overview of the codebase

**Workflow for making changes:**
1. Always **read the relevant file(s) first** before making edits, so you have the exact content.
2. Use **edit_file** for targeted modifications to existing files — provide the exact text to find and the replacement. This is preferred over write_file for edits.
3. Use **write_file** only when creating new files or when the changes are so extensive that a full rewrite is clearer.
4. Use **delete_file** to remove files that are no longer needed.
5. After making changes, briefly **summarise** what you changed and why.

When the user asks about code structure, architecture, or dependencies, \
use your tools to look things up rather than guessing.

Keep responses concise but informative. Use markdown formatting for readability.
"""

# Legacy alias for backward compat
SYSTEM_PROMPT = _SYSTEM_PROMPT_ASK


# ---------------------------------------------------------------------------
# Streaming chat handler
# ---------------------------------------------------------------------------

async def stream_chat(
    messages: list[dict],
    model: str = "",
    graph_dict: dict | None = None,
    mode: str = "ask",
) -> AsyncGenerator[str, None]:
    """
    Yield newline-delimited JSON events:
      {"type":"delta","content":"..."}          — assistant text chunk
      {"type":"tool_call","name":"...","args":"...","id":"..."}  — tool invocation
      {"type":"tool_result","id":"...","content":"..."}  — tool result
      {"type":"file_change","change_type":"...","path":"..."}  — file modification (agent mode)
      {"type":"done"}                           — stream finished
      {"type":"error","content":"..."}          — error
    """
    if not model:
        model = os.environ.get("LLM_MODEL", "gpt-5.4-mini")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        yield json.dumps({"type": "error", "content": "OPENAI_API_KEY is not set. Please set it as an environment variable."}) + "\n"
        return

    client = AsyncOpenAI(api_key=api_key)

    # Select tools and system prompt based on mode
    is_agent = mode == "agent"
    tools = AGENT_TOOLS if is_agent else ASK_TOOLS
    system_prompt = _SYSTEM_PROMPT_AGENT if is_agent else _SYSTEM_PROMPT_ASK
    max_rounds = 15 if is_agent else 6  # agent may need more rounds for multi-file edits

    # Prepend system message
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    try:
        for _round in range(max_rounds):
            response = await client.chat.completions.create(
                model=model,
                messages=full_messages,
                tools=tools,
                tool_choice="auto",
                stream=True,
            )

            tool_calls_acc: dict[int, dict] = {}  # index → {id, name, arguments}
            has_tool_calls = False
            finish_reason = None

            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                finish_reason = chunk.choices[0].finish_reason if chunk.choices else finish_reason

                if delta is None:
                    continue

                # Text content
                if delta.content:
                    yield json.dumps({"type": "delta", "content": delta.content}) + "\n"

                # Tool calls (streamed in parts)
                if delta.tool_calls:
                    has_tool_calls = True
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_acc[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_acc[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments

            if not has_tool_calls:
                # No tool calls — we're done
                break

            # Process tool calls
            # Add assistant message with tool_calls to conversation
            assistant_msg = {"role": "assistant", "content": None, "tool_calls": []}
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                assistant_msg["tool_calls"].append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                })
            full_messages.append(assistant_msg)

            # Execute each tool and add results
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}

                yield json.dumps({
                    "type": "tool_call",
                    "name": tc["name"],
                    "args": args,
                    "id": tc["id"],
                }) + "\n"

                result = dispatch_tool(tc["name"], args, graph_dict)

                yield json.dumps({
                    "type": "tool_result",
                    "id": tc["id"],
                    "name": tc["name"],
                    "content": result,
                }) + "\n"

                # Emit file_change event if the tool modified files
                try:
                    result_data = json.loads(result)
                    fc = result_data.get("file_change")
                    if fc:
                        yield json.dumps({
                            "type": "file_change",
                            "change_type": fc["type"],
                            "path": fc["path"],
                        }) + "\n"
                except (json.JSONDecodeError, KeyError):
                    pass

                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # Loop again to let the model respond to tool results

        yield json.dumps({"type": "done"}) + "\n"

    except Exception as e:
        yield json.dumps({"type": "error", "content": str(e)}) + "\n"

