"""FastAPI server — serves the analysis graph and the interactive frontend."""

import os
import json
import hashlib
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # project root .env

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
from backend.projects import (
    list_projects, create_project, delete_project,
    get_current_project, get_current_project_id,
    set_current_project,
)

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


# ── Active project helper ───────────────────────────────────────────

def _active_codebase_path() -> Optional[str]:
    """Return the absolute path of the currently active project's codebase.

    Falls back to the legacy mock_codebase2 / mock_codebase directory
    when no project has been selected yet.
    """
    proj = get_current_project()
    if proj:
        p = proj["path"]
        if os.path.isdir(p):
            return os.path.abspath(p)

    # Legacy fallback
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    if os.path.isdir(mock_path):
        return os.path.abspath(mock_path)
    fallback = os.path.abspath("mock_codebase")
    if os.path.isdir(fallback):
        return fallback
    return None


# ── Project management endpoints ────────────────────────────────────

@app.get("/api/projects")
def api_list_projects():
    """Return all registered projects and the currently active ID."""
    projects = list_projects()
    return {
        "projects": projects,
        "current_id": get_current_project_id(),
    }


@app.post("/api/projects")
async def api_create_project(request: Request):
    """Create a new project from a filesystem path.  Body: {name, path}."""
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "Path is required."}, status_code=400)
    try:
        proj = create_project(name or os.path.basename(path), path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Clear in-memory graph caches so the new project gets analysed fresh
    _graph_cache.clear()
    return proj


@app.post("/api/projects/select")
async def api_select_project(request: Request):
    """Switch the active project.  Body: {project_id}."""
    body = await request.json()
    pid = body.get("project_id", "")
    if not pid:
        return JSONResponse({"error": "project_id is required."}, status_code=400)
    if not set_current_project(pid):
        return JSONResponse({"error": "Project not found."}, status_code=404)
    # Clear in-memory caches so endpoints serve the new project
    _graph_cache.clear()
    _node_desc_cache.clear()
    return {"status": "ok", "current_id": pid}


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    """Delete a project from the registry."""
    if not delete_project(project_id):
        return JSONResponse({"error": "Project not found."}, status_code=404)
    _graph_cache.clear()
    return {"status": "ok"}


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
    """Silence Chrome DevTools probe 404s."""
    return Response(status_code=204)


# ── Filesystem browser endpoint ─────────────────────────────────────

_BROWSE_IGNORE = {
    "__pycache__", "node_modules", ".git", ".svn", ".hg",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", ".next", ".nuxt", "coverage", ".nyc_output",
    "bower_components", ".venv", "venv", "env",
}


@app.get("/api/browse")
def browse_dirs(path: str = Query(default="")):
    """List directories at *path* for the folder-picker UI.

    Returns ``{current, parent, dirs: [{name, path}]}``.
    When *path* is empty the user's home directory is used as the
    starting point.
    """
    if not path:
        path = str(Path.home())
    path = os.path.abspath(os.path.expanduser(path))

    if not os.path.isdir(path):
        return JSONResponse({"error": f"Not a directory: {path}"}, status_code=400)

    parent = os.path.dirname(path) if path != os.path.dirname(path) else None

    dirs: list[dict] = []
    try:
        for entry in sorted(os.listdir(path), key=str.lower):
            if entry.startswith(".") and entry not in (".",):
                continue
            if entry in _BROWSE_IGNORE:
                continue
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                dirs.append({"name": entry, "path": full})
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    return {"current": path, "parent": parent, "dirs": dirs}


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
    """Return the analysis graph for the active project."""
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected. Create or select a project first."}, status_code=404)

    payload = _get_or_analyze(abs_path, abstract)
    return payload


@app.get("/api/insights")
def get_insights():
    """Run pattern detection rules against the full (non-abstract) graph."""
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected."}, status_code=404)

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
    ".html", ".htm",
    ".java", ".kt", ".scala",
    ".c", ".h", ".cpp", ".hpp",
    ".go", ".rs", ".rb", ".php",
    ".cs", ".swift", ".m", ".mm",
}

_IGNORE_DIRS = {
    "__pycache__", "node_modules", ".git", ".svn", ".hg",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", ".idea", ".vscode",
    "venv", ".venv", "env", ".env",
    ".next", ".nuxt", "coverage", ".nyc_output",
    "bower_components", "vendor", ".cache", ".parcel-cache",
    "target", "out", "bin", "obj",
}

# File extensions to exclude even if they exist in the tree
_IGNORE_EXTENSIONS = {
    ".env", ".md", ".txt", ".rst", ".log", ".lock",
    ".yml", ".yaml", ".toml", ".cfg", ".ini", ".conf",
    ".json", ".xml", ".csv", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".map", ".min.js", ".min.css", ".LICENSE",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".dylib",
}

# File names to always exclude
_IGNORE_FILES = {
    ".env", ".gitignore", ".gitattributes", ".editorconfig",
    ".dockerignore", "Dockerfile", "docker-compose.yml",
    "Makefile", "Procfile", "Vagrantfile",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "README.md", "README.rst", "README.txt", "README",
    "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock", "composer.lock", "Gemfile.lock",
    "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml",
    "package.json", "tsconfig.json", "webpack.config.js",
    ".babelrc", ".eslintrc", ".eslintrc.json", ".eslintrc.js",
    ".prettierrc", ".prettierrc.json", ".prettierignore",
    "jest.config.js", "jest.config.ts",
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
        # Skip ignored and hidden directories
        if name in _IGNORE_DIRS or (name.startswith(".") and name != "."):
            return None
        # Also skip dirs whose name ends with common build suffixes
        if name.endswith(".egg-info") or name.endswith(".dist-info"):
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
                # Skip ignored file names
                if entry in _IGNORE_FILES:
                    continue
                ext = os.path.splitext(entry)[1].lower()
                # Skip ignored extensions, only show code extensions
                if ext in _IGNORE_EXTENSIONS or ext not in _CODE_EXTENSIONS:
                    continue
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
    """Return a directory tree of the active project's codebase."""
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected."}, status_code=404)

    tree = _build_file_tree(abs_path)
    return tree


@app.get("/api/source")
def get_source(path: str = Query(..., description="Relative file path within the codebase")):
    """Return the source code of a file within the active project."""
    abs_root = _active_codebase_path()
    if not abs_root:
        return JSONResponse({"error": "No project selected."}, status_code=404)

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

# ── Node description endpoint (GPT-powered hover descriptions) ──────

_node_desc_cache: dict[str, str] = {}  # content_hash → description


@app.post("/api/node-description")
async def node_description(request: Request):
    """Generate a 1-2 sentence AI description of a node's purpose.

    Body: {node_name, node_kind, file_path, line_start, line_end, content_hash?}

    The server caches results keyed on a hash of the source code so
    descriptions are regenerated only when the underlying code changes.
    """
    body = await request.json()
    node_name = body.get("node_name", "")
    node_kind = body.get("node_kind", "")
    file_path = body.get("file_path", "")
    line_start = body.get("line_start", 0)
    line_end = body.get("line_end", 0)
    members = body.get("members")  # for island chain / group nodes

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "OPENAI_API_KEY not set"}, status_code=500)

    abs_root = _active_codebase_path()
    if not abs_root:
        return JSONResponse({"error": "No project selected"}, status_code=404)

    # Build the source snippet for leaf nodes
    snippet = ""
    if file_path and line_start and line_end:
        full = os.path.normpath(os.path.join(abs_root, file_path))
        if os.path.isfile(full):
            try:
                lines = Path(full).read_text(encoding="utf-8", errors="replace").splitlines()
                snippet = "\n".join(lines[max(0, line_start - 1):line_end])
                # Cap snippet size
                if len(snippet) > 4000:
                    snippet = snippet[:4000] + "\n… [truncated]"
            except OSError:
                pass

    # Build cache key from content hash
    if snippet:
        content_hash = hashlib.md5(snippet.encode()).hexdigest()[:16]
    elif members:
        content_hash = hashlib.md5(json.dumps(members, sort_keys=True).encode()).hexdigest()[:16]
    else:
        content_hash = hashlib.md5(f"{node_name}:{node_kind}".encode()).hexdigest()[:16]

    cache_key = f"{node_name}|{node_kind}|{content_hash}"

    # Check cache
    if cache_key in _node_desc_cache:
        return {"description": _node_desc_cache[cache_key], "cached": True}

    # Build prompt
    if snippet:
        user_msg = (
            f"Describe the purpose of this {node_kind} named '{node_name}' in 1-2 brief sentences. "
            f"Focus on what it does and its role in the codebase. Be concise.\n\n```\n{snippet}\n```"
        )
    elif members:
        user_msg = (
            f"This is a '{node_kind}' group node named '{node_name}' that contains these members: "
            f"{', '.join(members[:20])}. "
            f"Describe its purpose in 1-2 brief sentences. Focus on what this grouping represents."
        )
    else:
        user_msg = (
            f"Describe the likely purpose of a {node_kind} named '{node_name}' in a codebase, "
            f"in 1-2 brief sentences."
        )

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a concise code documentation assistant. Respond with exactly 1-2 sentences describing the purpose of the given code element. No markdown, no bullet points."},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=120,
            temperature=0.3,
        )
        description = resp.choices[0].message.content.strip()
        _node_desc_cache[cache_key] = description
        return {"description": description, "cached": False, "content_hash": content_hash}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
    abs_path = _active_codebase_path()
    graph_dict = None
    if abs_path:
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