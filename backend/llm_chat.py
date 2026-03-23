"""LLM Chat handler — OpenAI integration with codebase tool-use."""

import os
import json
from typing import AsyncGenerator

from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Tool definitions for OpenAI function-calling
# ---------------------------------------------------------------------------

TOOLS = [
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
            "name": "write_file",
            "description": "Create or overwrite a file in the codebase with the given content.",
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


def _resolve_codebase_root() -> str:
    """Return the absolute path to the target codebase."""
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
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return json.dumps({"status": "ok", "path": path, "bytes": len(content)})
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
        return json.dumps({"status": "ok", "path": path})
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
    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the Vibe Coding Assistant — an AI embedded in an interactive architecture visualization tool.

You have access to the full analysed codebase and can:
• Read, search, create, modify, and delete files
• Navigate the architecture graph to show specific classes/modules
• Open files in the code editor panel with line highlights

The codebase you are analysing is a Python project. When the user asks about code structure, \
architecture patterns, dependencies, or specific classes — use your tools to look things up \
rather than guessing.

Keep responses concise but informative. Use markdown formatting for readability. \
When you reference a class or file, offer to navigate to it or open it.

If you modify files, briefly summarise what you changed.
"""


# ---------------------------------------------------------------------------
# Streaming chat handler
# ---------------------------------------------------------------------------

async def stream_chat(
    messages: list[dict],
    model: str = "gpt-4o-mini",
    graph_dict: dict | None = None,
) -> AsyncGenerator[str, None]:
    """
    Yield newline-delimited JSON events:
      {"type":"delta","content":"..."}          — assistant text chunk
      {"type":"tool_call","name":"...","args":"...","id":"..."}  — tool invocation
      {"type":"tool_result","id":"...","content":"..."}  — tool result
      {"type":"done"}                           — stream finished
      {"type":"error","content":"..."}          — error
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        yield json.dumps({"type": "error", "content": "OPENAI_API_KEY is not set. Please set it as an environment variable."}) + "\n"
        return

    client = AsyncOpenAI(api_key=api_key)

    # Prepend system message
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    try:
        # Up to 5 rounds of tool-use
        for _round in range(6):
            response = await client.chat.completions.create(
                model=model,
                messages=full_messages,
                tools=TOOLS,
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

                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # Loop again to let the model respond to tool results

        yield json.dumps({"type": "done"}) + "\n"

    except Exception as e:
        yield json.dumps({"type": "error", "content": str(e)}) + "\n"

