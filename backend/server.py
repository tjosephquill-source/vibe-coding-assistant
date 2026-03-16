"""FastAPI server — serves the analysis graph and the interactive frontend."""

import os
import json
import hashlib
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # load .env from project root (for OPENAI_API_KEY etc.)

from fastapi import FastAPI, Query, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from backend.analyzer import (
    analyze_codebase, abstract_graph,
    _load_from_cache, _save_to_cache, _ensure_cache_dir,
)
from backend.insights import detect_insights
from backend.llm_chat import stream_chat

app = FastAPI(title="VibeCodingAssistant", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache of analysis results by options
_graph_cache: dict[str, dict] = {}


def _cache_key(repo_path: str, abstract: bool) -> str:
    return f"{os.path.abspath(repo_path)}|{int(abstract)}"


def _get_or_analyze(repo_path: str, abstract: bool) -> dict:
    """Return graph dict from memory cache → disk cache → fresh analysis."""
    abs_path = os.path.abspath(repo_path)
    key = _cache_key(abs_path, abstract)

    # 1. Memory cache
    if key in _graph_cache:
        return _graph_cache[key]

    # 2. Disk cache
    cached = _load_from_cache(abs_path, abstract)
    if cached is not None:
        _graph_cache[key] = cached
        return cached

    # 3. Fresh analysis
    graph = analyze_codebase(abs_path)
    if abstract:
        graph = abstract_graph(graph)

    payload = graph.to_dict()
    _graph_cache[key] = payload
    _save_to_cache(abs_path, abstract, payload)
    return payload


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
    """Silence Chrome DevTools probe 404s."""
    return Response(status_code=204)


@app.post("/api/analyze")
def analyze(
    repo_path: str = Query(default="mock_codebase"),
    abstract: bool = Query(default=False),
):
    """Analyze a codebase and cache the resulting graph."""
    abs_path = os.path.abspath(repo_path)
    if not os.path.isdir(abs_path):
        return JSONResponse({"error": f"Directory not found: {abs_path}"}, status_code=400)

    payload = _get_or_analyze(abs_path, abstract)
    return {"status": "ok", "nodes": len(payload["nodes"]), "edges": len(payload["edges"])}


@app.get("/api/graph")
def get_graph(
    abstract: bool = Query(default=False),
):
    """Return a cached graph (or auto-analyze mock_codebase2)."""
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    abs_path = os.path.abspath(target_path)

    if not os.path.isdir(abs_path):
        return JSONResponse({"error": "No analysis run yet. POST /api/analyze first."}, status_code=404)

    payload = _get_or_analyze(abs_path, abstract)
    return payload


@app.get("/api/insights")
def get_insights():
    """Run pattern detection rules against the full (non-abstract) graph."""
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    abs_path = os.path.abspath(target_path)

    if not os.path.isdir(abs_path):
        return JSONResponse({"error": "No analysis run yet."}, status_code=404)

    # Run detection on the full (non-abstract) graph
    graph_dict = _get_or_analyze(abs_path, abstract=False)
    insights = detect_insights(graph_dict)
    return {"insights": insights, "count": len(insights)}


# ── Position cache endpoints ────────────────────────────────────────

def _positions_file(graph_key: str) -> Path:
    """Return the disk path for a position cache file."""
    safe = hashlib.md5(graph_key.encode()).hexdigest()[:16]
    return _ensure_cache_dir() / f"positions_{safe}.json"


@app.get("/api/positions")
def get_positions(graph_key: str = Query(...)):
    """Return cached node positions for a graph identified by graph_key (sorted node-ID hash)."""
    path = _positions_file(graph_key)
    if not path.exists():
        return JSONResponse({"positions": None}, status_code=200)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("graph_key") == graph_key:
            return {"positions": data.get("positions")}
    except (json.JSONDecodeError, OSError):
        pass
    return JSONResponse({"positions": None}, status_code=200)


@app.post("/api/positions")
async def save_positions(request: Request):
    """Save node positions to disk. Body: {graph_key, positions: {id: {x, y}}}."""
    body = await request.json()
    graph_key = body.get("graph_key")
    positions = body.get("positions")
    if not graph_key:
        return JSONResponse({"error": "Missing graph_key"}, status_code=400)
    if not positions:
        # Nothing to save, but not an error
        return {"status": "ok", "skipped": True}
    path = _positions_file(graph_key)
    payload = {"graph_key": graph_key, "positions": positions}
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return JSONResponse({"error": "Failed to write cache"}, status_code=500)
    return {"status": "ok"}


# ── Window layout cache endpoints ───────────────────────────────────

# ── Directory listing endpoint ──────────────────────────────────────

# File extensions considered relevant for code analysis
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
    ".eggs", "*.egg-info",
}


def _build_file_tree(root: str, max_depth: int = 12) -> dict:
    """Walk *root* and return a nested dict representing the file tree.

    Returns:
        {
          "name": "<dir-name>",
          "type": "directory",
          "children": [ ... ],
        }

    Files are only included if they have a recognised code extension.
    Directories that end up empty (no code files at any depth) are pruned.
    """

    def _walk(dirpath: str, depth: int) -> Optional[dict]:
        if depth > max_depth:
            return None
        name = os.path.basename(dirpath)
        if name in _IGNORE_DIRS:
            return None

        children: list[dict] = []

        try:
            entries = sorted(os.listdir(dirpath))
        except PermissionError:
            return None

        dirs_first: list[tuple[str, bool]] = []
        for entry in entries:
            full = os.path.join(dirpath, entry)
            dirs_first.append((entry, os.path.isdir(full)))

        # Sort: directories first, then files, both alphabetical
        dirs_first.sort(key=lambda t: (not t[1], t[0].lower()))

        for entry, is_dir in dirs_first:
            full = os.path.join(dirpath, entry)
            if is_dir:
                subtree = _walk(full, depth + 1)
                if subtree is not None:
                    children.append(subtree)
            else:
                ext = os.path.splitext(entry)[1].lower()
                if ext in _CODE_EXTENSIONS:
                    children.append({
                        "name": entry,
                        "type": "file",
                        "path": os.path.relpath(full, root),
                    })

        if not children:
            return None

        return {
            "name": name,
            "type": "directory",
            "path": os.path.relpath(dirpath, root) if dirpath != root else ".",
            "children": children,
        }

    tree = _walk(root, 0)
    if tree is None:
        tree = {"name": os.path.basename(root), "type": "directory", "path": ".", "children": []}
    return tree


@app.get("/api/files")
def get_files():
    """Return a directory tree of the analysed codebase."""
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    abs_path = os.path.abspath(target_path)

    if not os.path.isdir(abs_path):
        return JSONResponse({"error": "Codebase directory not found."}, status_code=404)

    tree = _build_file_tree(abs_path)
    return tree


@app.get("/api/source")
def get_source(path: str = Query(..., description="Relative file path within the codebase")):
    """Return the source code of a file within the analysed codebase."""
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    abs_root = os.path.abspath(target_path)

    # Resolve and validate the requested path stays within the codebase root
    requested = os.path.normpath(os.path.join(abs_root, path))
    if not requested.startswith(abs_root + os.sep) and requested != abs_root:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not os.path.isfile(requested):
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        content = Path(requested).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return JSONResponse({"error": "Could not read file"}, status_code=500)

    return {"path": path, "content": content}


# ── Window layout cache endpoints (continued) ──────────────────────

def _layout_file() -> Path:
    """Return the disk path for the window layout cache."""
    return _ensure_cache_dir() / "wm_layout.json"


@app.get("/api/layout")
def get_layout():
    """Return the saved window manager layout state."""
    path = _layout_file()
    if not path.exists():
        return JSONResponse({"layout": None}, status_code=200)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"layout": None}, status_code=200)


@app.post("/api/layout")
async def save_layout(request: Request):
    """Save window manager layout state to disk."""
    body = await request.json()
    path = _layout_file()
    try:
        path.write_text(json.dumps(body), encoding="utf-8")
    except OSError:
        return JSONResponse({"error": "Failed to write cache"}, status_code=500)
    return {"status": "ok"}


# ── LLM Chat endpoint ───────────────────────────────────────────────

@app.get("/api/chat/models")
def get_chat_models():
    """Return available LLM models for the chat panel."""
    return {
        "models": [
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "default": True},
            {"id": "gpt-4o", "name": "GPT-4o"},
            {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini"},
            {"id": "gpt-4.1", "name": "GPT-4.1"},
        ]
    }


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Stream an LLM chat response with tool-use. Body: {messages, model?}."""
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "gpt-4o-mini")

    if not messages:
        return JSONResponse({"error": "No messages provided."}, status_code=400)

    # Get the full graph for graph-summary tool
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    abs_path = os.path.abspath(target_path)
    graph_dict = None
    if os.path.isdir(abs_path):
        try:
            graph_dict = _get_or_analyze(abs_path, abstract=False)
        except Exception:
            pass

    return StreamingResponse(
        stream_chat(messages, model=model, graph_dict=graph_dict),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Serve the frontend
_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.is_dir():
    @app.get("/", response_class=HTMLResponse)
    def serve_index():
        return (_frontend_dir / "index.html").read_text()

    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")