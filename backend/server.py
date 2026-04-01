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
from backend.descriptors import generate_hierarchical_descriptions, _load_desc_cache
from backend.insights import detect_insights
from backend.llm_chat import stream_chat, set_codebase_root, set_hier_descriptions
from backend.projects import (
    list_projects, create_project, delete_project,
    get_current_project, get_current_project_id,
    set_current_project,
)

app = FastAPI(title="VibeCodingAssistant", version="0.1.0")

# ── LLM model from environment ──────────────────────────────────────

def _llm_model() -> str:
    """Return the configured LLM model name from LLM_MODEL env var."""
    return os.environ.get("LLM_MODEL", "gpt-5.4-mini")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache of analysis results by options
_graph_cache: dict[str, dict] = {}
_hier_desc_cache: dict[str, dict] = {}  # abs_path → hierarchical descriptions


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
    _preview_cache.clear()
    _tts_audio_cache.clear()
    _enrich_cache.clear()
    _hier_desc_cache.clear()
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


@app.get("/favicon.ico")
def favicon():
    """Return empty response for favicon requests."""
    return Response(status_code=204)


# ── Filesystem browser endpoint ─────────────────────────────────────

_BROWSE_IGNORE = {
    "__pycache__", "node_modules", ".git", ".svn", ".hg",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", ".next", ".nuxt", "coverage", ".nyc_output",
    "bower_components", ".venv", "venv", "env",
}


@app.post("/api/mkdir")
async def api_mkdir(request: Request):
    """Create a new directory.  Body: {path}.

    Returns ``{path}`` with the absolute path of the created directory.
    """
    body = await request.json()
    raw_path = body.get("path", "").strip()
    if not raw_path:
        return JSONResponse({"error": "Path is required."}, status_code=400)

    abs_path = os.path.abspath(os.path.expanduser(raw_path))

    # Don't allow creating a directory that already exists
    if os.path.exists(abs_path):
        return JSONResponse({"error": f"Already exists: {abs_path}"}, status_code=409)

    # Parent must exist
    parent = os.path.dirname(abs_path)
    if not os.path.isdir(parent):
        return JSONResponse({"error": f"Parent directory not found: {parent}"}, status_code=400)

    try:
        os.makedirs(abs_path, exist_ok=False)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return {"path": abs_path}


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


@app.post("/api/graph/refresh")
def refresh_graph():
    """Lightweight graph refresh — clears only graph and position caches,
    then re-runs the analyzer.  Descriptions, enrichments, and insights
    are left intact.  Designed for real-time updates when the AI agent
    creates / edits / deletes files.
    """
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected."}, status_code=404)

    # Clear graph memory caches for this project
    keys_to_remove = [k for k in _graph_cache if k.startswith(abs_path + "|")]
    for k in keys_to_remove:
        del _graph_cache[k]

    # Clear graph + position disk caches for this project
    try:
        cache_dir = _ensure_cache_dir()
        dir_hash_10 = hashlib.md5(abs_path.encode()).hexdigest()[:10]
        for f in cache_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            # Analyzer graph caches: {dir_hash_10}_{tag}.json
            if name.startswith(dir_hash_10 + "_"):
                f.unlink(missing_ok=True)
            # Position caches: positions_{hash}.json
            elif name.startswith("positions_"):
                f.unlink(missing_ok=True)
    except Exception:
        pass

    # Re-run analysis (both abstract and full)
    payload_full = _get_or_analyze(abs_path, abstract=False)
    payload_abstract = _get_or_analyze(abs_path, abstract=True)

    return {
        "status": "ok",
        "nodes": len(payload_full["nodes"]),
        "edges": len(payload_full["edges"]),
        "abstract_nodes": len(payload_abstract["nodes"]),
    }


@app.get("/api/insights")
def get_insights():
    """Run pattern detection rules against the full (non-abstract) graph."""
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected."}, status_code=404)

    graph_dict = _get_or_analyze(abs_path, abstract=False)
    insights = detect_insights(graph_dict)
    return {"insights": insights, "count": len(insights)}


@app.post("/api/reanalyze")
def reanalyze():
    """Clear all caches and re-run analysis for the active project.

    This forces a full re-parse of the codebase and regeneration of
    all derived data (graph, descriptions, insights, enrichments).
    """
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected."}, status_code=404)

    # Clear all in-memory caches
    _graph_cache.clear()
    _node_desc_cache.clear()
    _preview_cache.clear()
    _tts_audio_cache.clear()
    _enrich_cache.clear()
    _hier_desc_cache.clear()

    # Clear disk caches for this project
    try:
        cache_dir = _ensure_cache_dir()
        dir_hash_10 = hashlib.md5(abs_path.encode()).hexdigest()[:10]
        dir_hash_12 = hashlib.md5(abs_path.encode()).hexdigest()[:12]
        for f in cache_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            # Analyzer graph caches: {dir_hash_10}_{tag}.json
            if name.startswith(dir_hash_10 + "_"):
                f.unlink(missing_ok=True)
            # Description caches: descriptions_{dir_hash_12}.json
            elif name.startswith(f"descriptions_{dir_hash_12}"):
                f.unlink(missing_ok=True)
    except Exception:
        pass  # best-effort cleanup

    # Re-run analysis
    payload = _get_or_analyze(abs_path, abstract=False)
    _ = _get_or_analyze(abs_path, abstract=True)

    return {
        "status": "ok",
        "nodes": len(payload["nodes"]),
        "edges": len(payload["edges"]),
    }


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


@app.get("/api/fs/checksum")
def fs_checksum():
    """Return a lightweight hash of all code files in the active project.

    Walks the codebase collecting ``(relative_path, mtime, size)`` for every
    code file, then hashes the sorted list.  The frontend polls this to
    detect external filesystem changes (files created / edited / deleted
    outside the app).
    """
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected."}, status_code=404)

    entries: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(abs_path):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
            for fname in sorted(filenames):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _CODE_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, abs_path)
                try:
                    st = os.stat(full)
                    entries.append(f"{rel}|{st.st_mtime_ns}|{st.st_size}")
                except OSError:
                    entries.append(f"{rel}|?|?")
    except OSError:
        pass

    digest = hashlib.md5("\n".join(entries).encode()).hexdigest()
    return {"checksum": digest, "file_count": len(entries)}


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
    member_methods = body.get("member_methods")  # loose functions
    edges_summary = body.get("edges_summary", "")  # for graph overview

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
        hash_input = json.dumps(members, sort_keys=True)
        if edges_summary:
            hash_input += "|" + edges_summary
        content_hash = hashlib.md5(hash_input.encode()).hexdigest()[:16]
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
    elif node_kind == "graph_overview":
        parts = [f"This is an architecture graph view called '{node_name}'."]
        if members:
            parts.append(f"It contains these components: {', '.join(members[:30])}.")
        if member_methods:
            parts.append(f"It also includes these functions: {', '.join(member_methods[:20])}.")
        if edges_summary:
            parts.append(f"Relationships between components: {edges_summary}.")
        parts.append(
            "Describe the overall architecture and purpose of this part of the codebase "
            "in 2-3 brief sentences. Focus on what these components do together and how they relate."
        )
        user_msg = " ".join(parts)
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
        max_tokens = 180 if node_kind == "graph_overview" else 120
        system_msg = (
            "You are a concise code documentation assistant. "
            "Respond with a brief description of the given code element or architecture. "
            "No markdown, no bullet points."
        )
        resp = await client.chat.completions.create(
            model=_llm_model(),
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=max_tokens,
            temperature=0.3,
        )
        description = resp.choices[0].message.content.strip()
        _node_desc_cache[cache_key] = description
        return {"description": description, "cached": False, "content_hash": content_hash}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Hierarchical description endpoints ───────────────────────────────

import asyncio as _asyncio

# Background task state
_desc_task: _asyncio.Task | None = None
_desc_progress: dict = {"status": "idle", "done": 0, "total": 0, "current_node": ""}


def _build_merged_graph(abs_path: str) -> dict:
    """Build the merged graph dict needed for hierarchical descriptions."""
    full_graph = _get_or_analyze(abs_path, abstract=False)
    abstract_graph_dict = _get_or_analyze(abs_path, abstract=True)

    merged = {
        "nodes": abstract_graph_dict.get("nodes", []),
        "edges": abstract_graph_dict.get("edges", []),
        "intermediate_nodes": (
            abstract_graph_dict.get("intermediate_nodes", []) +
            full_graph.get("nodes", [])
        ),
        "intermediate_edges": (
            abstract_graph_dict.get("intermediate_edges", []) +
            full_graph.get("edges", [])
        ),
    }

    seen_ids = {n["id"] for n in merged["nodes"]}
    deduped = []
    for n in merged["intermediate_nodes"]:
        if n["id"] not in seen_ids:
            deduped.append(n)
            seen_ids.add(n["id"])
    merged["intermediate_nodes"] = deduped
    return merged


async def _run_describe_background(abs_path: str, api_key: str, force: bool):
    """Background coroutine that generates descriptions and updates progress."""
    global _desc_progress

    async def _progress(done, total, name):
        _desc_progress["done"] = done
        _desc_progress["total"] = total
        _desc_progress["current_node"] = name

    try:
        _desc_progress = {"status": "running", "done": 0, "total": 0, "current_node": "analysing codebase…"}

        # Run synchronous graph building in a thread to avoid blocking event loop
        merged = await _asyncio.to_thread(_build_merged_graph, abs_path)

        _desc_progress["current_node"] = "computing hashes…"

        descriptions = await generate_hierarchical_descriptions(
            root_dir=abs_path,
            graph_dict=merged,
            api_key=api_key,
            force=force,
            progress_callback=_progress,
        )

        _hier_desc_cache[abs_path] = descriptions
        _desc_progress = {
            "status": "done",
            "done": _desc_progress.get("total", len(descriptions)),
            "total": _desc_progress.get("total", len(descriptions)),
            "current_node": "",
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _desc_progress = {"status": "error", "done": 0, "total": 0,
                          "current_node": "", "error": str(exc)}


@app.post("/api/describe")
async def describe_hierarchy(request: Request):
    """Kick off hierarchical description generation in the background.

    Body: {force?: bool}

    Returns immediately with {status: "started"} or {status: "already_running"}.
    Poll GET /api/describe/status for progress.
    When done, GET /api/describe returns the descriptions.
    """
    global _desc_task
    body = await request.json()
    force = body.get("force", False)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "OPENAI_API_KEY not set"}, status_code=500)

    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected"}, status_code=404)

    # If already running, don't start another
    if _desc_task is not None and not _desc_task.done():
        return {"status": "already_running", "progress": _desc_progress}

    # Set progress to running immediately (before the task coroutine starts)
    # to avoid a race where status returns "idle" right after task creation
    _desc_progress.update({"status": "running", "done": 0, "total": 0, "current_node": "starting…"})

    _desc_task = _asyncio.create_task(
        _run_describe_background(abs_path, api_key, force)
    )
    return {"status": "started"}


@app.get("/api/describe/status")
def describe_status():
    """Return the current description-generation progress."""
    running = _desc_task is not None and not _desc_task.done()
    return {
        "running": running,
        **_desc_progress,
    }


@app.get("/api/describe")
def get_descriptions():
    """Return cached hierarchical descriptions (disk cache) without regenerating."""
    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected"}, status_code=404)

    # Check memory cache first
    if abs_path in _hier_desc_cache:
        descs = _hier_desc_cache[abs_path]
        return {
            "descriptions": descs,
            "cached": True,
            "generating": _desc_task is not None and not _desc_task.done(),
        }

    # Fall back to disk cache
    cached = _load_desc_cache(abs_path)
    if cached:
        _hier_desc_cache[abs_path] = cached
        return {
            "descriptions": cached,
            "cached": True,
            "generating": _desc_task is not None and not _desc_task.done(),
        }

    return {
        "descriptions": {},
        "cached": False,
        "generating": _desc_task is not None and not _desc_task.done(),
    }


# ── Preview walkthrough endpoint ─────────────────────────────────────

_preview_cache: dict[str, list] = {}  # (fingerprint|level) → scenes


_CONTENT_TYPE_MAP = {
    "database": "💾", "auth": "🔐", "network": "🌐", "user": "👤",
    "email": "✉️", "payment": "💳", "file_io": "📁", "config": "🔧",
    "logging": "📊", "test": "🧪", "error": "🚨", "cache": "⚡",
    "scheduling": "⏰", "ui": "🖼️", "serialization": "🔄",
    "search": "🔍", "cli": "⌨️", "ml": "🤖", "geo": "📍",
    "math": "🧮", "deploy": "🚀", "service": "🎛️", "utility": "🔨",
    "event": "📢", "data_model": "📋", "general": ">_",
}

_CONTENT_TYPES_LIST = list(_CONTENT_TYPE_MAP.keys())


@app.post("/api/preview")
async def generate_preview(request: Request):
    """Generate a scripted architecture walkthrough driven by the description hierarchy.

    Body: {level: "high"|"medium"|"low", focus_node_id?: str}

    High-level:  root + direct children → explains what the codebase *does*
    Medium-level: children + grandchildren → explains key subsystems / classes
    Low-level:   grandchildren + leaf methods → explains specific behaviour

    If focus_node_id is given, the walkthrough starts from that node's
    context in the hierarchy (its parent for framing, its children for detail).

    Returns: {scenes: [...], level: str}
    """
    body = await request.json()
    level = body.get("level", "high")
    focus_node_id = body.get("focus_node_id")
    if level not in ("high", "medium", "low"):
        level = "high"

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "OPENAI_API_KEY not set"}, status_code=500)

    abs_path = _active_codebase_path()
    if not abs_path:
        return JSONResponse({"error": "No project selected"}, status_code=404)

    # Load hierarchical descriptions — these are the ground truth
    hier_descs = _hier_desc_cache.get(abs_path) or _load_desc_cache(abs_path)
    if not hier_descs:
        return JSONResponse(
            {"error": "Descriptions not yet generated. Please wait for description generation to complete."},
            status_code=409,
        )

    # Load graph data for node IDs that the frontend can focus on
    if level == "low":
        graph_dict = _get_or_analyze(abs_path, abstract=False)
    else:
        graph_dict = _get_or_analyze(abs_path, abstract=True)

    nodes = graph_dict.get("nodes", [])
    edges = graph_dict.get("edges", [])
    node_id_set = {n["id"] for n in nodes}

    # ── Build the description context for the LLM based on level ────

    def _desc_entry(nid: str) -> dict | None:
        """Return a compact description dict for a node, or None."""
        entry = hier_descs.get(nid)
        if not entry:
            return None
        return {
            "id": nid,
            "name": entry.get("name", "?"),
            "kind": entry.get("kind", "?"),
            "description": entry.get("description", ""),
            "level": entry.get("level", 0),
            "children_ids": entry.get("children_ids", []),
        }

    def _collect_children(nid: str, depth: int = 1) -> list[dict]:
        """Recursively collect description entries for children up to depth."""
        entry = hier_descs.get(nid, {})
        kids = entry.get("children_ids", [])
        result = []
        for kid in kids:
            child_entry = _desc_entry(kid)
            if child_entry:
                result.append(child_entry)
                if depth > 1:
                    result.extend(_collect_children(kid, depth - 1))
        return result

    root_entry = _desc_entry("__root__")
    context_nodes = []  # description entries to send to LLM
    walkthrough_scope = ""  # human-readable scope description

    if focus_node_id and focus_node_id in hier_descs:
        # ── Focused walkthrough: start from a specific node ─────────
        focus_entry = _desc_entry(focus_node_id)
        if focus_entry:
            # Find the parent for framing context
            parent_entry = None
            for nid, entry in hier_descs.items():
                if focus_node_id in entry.get("children_ids", []):
                    parent_entry = _desc_entry(nid)
                    break

            if parent_entry:
                context_nodes.append(parent_entry)
            context_nodes.append(focus_entry)
            # Add children based on level
            child_depth = {"high": 1, "medium": 2, "low": 3}.get(level, 1)
            context_nodes.extend(_collect_children(focus_node_id, child_depth))
            walkthrough_scope = (
                f"Focused on '{focus_entry['name']}' ({focus_entry['kind']}). "
                f"Explain its purpose in the broader system, then walk through "
                f"its internals at {'high' if level == 'high' else 'detailed'} level."
            )
    else:
        # ── Full codebase walkthrough ───────────────────────────────
        if level == "high":
            # Root + children + grandchildren for richer context
            if root_entry:
                context_nodes.append(root_entry)
            context_nodes.extend(_collect_children("__root__", depth=2))
            walkthrough_scope = (
                "Give a high-level overview of what this SOFTWARE does for its USERS. "
                "Many of the descriptions below describe infrastructure components "
                "(UI layout, HTTP servers, window managers, file I/O, caching). "
                "Those are HOW the software works internally — ignore them. "
                "Instead, find the components that describe the software's actual "
                "PURPOSE and CAPABILITIES — the thing a user launches this app to do. "
                "Think: what problem does this software solve? What does it produce? "
                "Who uses it and why?"
            )

        elif level == "medium":
            # Root + children + grandchildren → subsystems & key classes
            if root_entry:
                context_nodes.append(root_entry)
            context_nodes.extend(_collect_children("__root__", depth=2))
            walkthrough_scope = (
                "Walk through the codebase's key subsystems and important classes. "
                "Start with a brief recap of the codebase's purpose, then dive into "
                "each major area: what it handles, its key classes, and how data flows "
                "between subsystems. The audience understands the high-level purpose — "
                "now they want to understand the architecture."
            )

        else:  # low
            # Full depth → methods, call chains, data flow
            if root_entry:
                context_nodes.append(root_entry)
            context_nodes.extend(_collect_children("__root__", depth=4))
            walkthrough_scope = (
                "Walk through the codebase at implementation level: specific methods, "
                "call chains, and data flow. Start with a one-sentence purpose recap, "
                "then trace how data enters the system, is processed, and exits. "
                "Name specific methods, parameters, and return values."
            )

    # ── Cache key ───────────────────────────────────────────────────
    ctx_ids = sorted(e["id"] for e in context_nodes)
    cache_input = f"{','.join(ctx_ids)}|{level}|{focus_node_id or ''}"
    cache_key = hashlib.md5(cache_input.encode()).hexdigest()[:16]
    if cache_key in _preview_cache:
        return {"scenes": _preview_cache[cache_key], "level": level, "cached": True}

    # ── Build LLM context ──────────────────────────────────────────
    # Provide descriptions as a hierarchy the LLM can read
    desc_lines = []
    for entry in context_nodes:
        indent = "  " * entry.get("level", 0)
        kids = entry.get("children_ids", [])
        kid_names = []
        for kid in kids[:10]:
            k = hier_descs.get(kid, {})
            kid_names.append(k.get("name", kid))
        kid_str = f" [contains: {', '.join(kid_names)}]" if kid_names else ""
        desc_lines.append(
            f"{indent}• [{entry['id']}] {entry['name']} ({entry['kind']}){kid_str}\n"
            f"{indent}  {entry['description'][:500]}"
        )

    descriptions_context = "\n\n".join(desc_lines)

    # Also provide the graph node IDs so the LLM can reference them for camera focus
    visible_nodes = []
    for n in nodes:
        visible_nodes.append({"id": n["id"], "name": n["name"], "kind": n.get("kind", "")})

    edges_summary = []
    for e in edges[:40]:
        src = e["source"] if isinstance(e["source"], str) else e["source"].get("id", "")
        tgt = e["target"] if isinstance(e["target"], str) else e["target"].get("id", "")
        edges_summary.append({"source": src, "target": tgt, "kind": e.get("kind", "")})

    graph_ref = json.dumps({"visible_nodes": visible_nodes, "edges": edges_summary}, indent=1)
    if len(graph_ref) > 6000:
        graph_ref = graph_ref[:6000] + "\n… [truncated]"

    # ── Build level-specific system prompt ──────────────────────────

    _scene_schema = """\
Scene schema:
{
  "narration": "1-3 sentences",
  "focus_node_ids": ["node_id"],
  "secondary_node_ids": ["node_id"],
  "edge_ids": [{"source": "src_id", "target": "tgt_id"}],
  "camera": "zoom_to_focus" | "pan_to_focus" | "zoom_out_all" | "hold",
  "duration_hint": 6
}"""

    _shared_rules = """\
- Output a JSON object: {"scenes": [...]}
- Reference nodes ONLY by their exact id from VISIBLE GRAPH NODES
- focus_node_ids must exist in VISIBLE GRAPH NODES
- duration_hint in seconds (5-10 per scene)"""

    # ── Static UI terminology (always valid — describes the visualisation tool) ──
    _tool_ui_terms = """\
You are narrating inside an interactive architecture-graph visualisation tool. Use these UI terms — they are what the user sees on screen:
- "node" = a box on the graph representing a class, method, or group of related classes
- "edge" = a line connecting two nodes showing a relationship (calls, inherits, imports, instantiates, uses_type)
- "graph" = the interactive architecture visualisation the user is looking at right now
- "drill down" / "dig down" = double-clicking a node to zoom into its internal structure (methods, sub-classes)
- "island chain" = a cluster of related classes grouped together by shared relationships
- "abstract view" = the high-level grouped view showing island chains and groups
- "full view" = the detailed view showing every individual class
- "walkthrough" = the animated narrated tour you are generating right now
- "insights" = detected architectural patterns and anti-patterns (circular dependencies, god classes, orphan modules)
- "node description" = the AI-generated description shown when hovering over a node
- "breadcrumb" = the navigation trail showing the current drill-down path (e.g. Overview > Services > UserService)
- "edges" have kinds: "calls" (method invocation), "inherits" (class inheritance), "imports" (module import), "contains" (parent→child), "instantiates" (object creation), "uses_type" (type reference)

Use these terms naturally in your narration. The user is looking at this graph UI while listening."""

    # ── Dynamic glossary built from the analysed codebase's own descriptions ──
    def _build_target_app_glossary(descs: dict) -> str:
        """Build a glossary of the TARGET codebase from its hierarchical descriptions.

        This extracts the root overview and top-level subsystem names so the LLM
        knows what the analysed software is about — works for ANY codebase.
        """
        lines = []
        root = descs.get("__root__", {})
        root_desc = root.get("description", "").strip()
        root_name = root.get("name", "Unknown")
        if root_desc:
            lines.append(f"The software being analysed is: {root_name}")
            lines.append(f"Overview: {root_desc[:600]}")

        # Top-level subsystems / components
        root_children = root.get("children_ids", [])
        if root_children:
            subsystem_lines = []
            for cid in root_children[:15]:
                child = descs.get(cid, {})
                cname = child.get("name", cid)
                cdesc = child.get("description", "").strip()
                if cdesc:
                    subsystem_lines.append(f"  • {cname}: {cdesc[:200]}")
                else:
                    subsystem_lines.append(f"  • {cname}")
            if subsystem_lines:
                lines.append("Key subsystems/components:")
                lines.extend(subsystem_lines)

        if not lines:
            return "No prior knowledge of this software is available — rely on the DESCRIPTIONS provided below."

        return "\n".join(lines)

    _target_glossary = _build_target_app_glossary(hier_descs)

    if level == "high":
        scene_count = "4-6"
        system_msg = f"""You are narrating a high-level product overview of a software system displayed as an interactive architecture graph. Your audience has never seen this codebase. Explain what this software IS and what it DOES — like a senior engineer explaining the product to a new team member while pointing at the graph on screen.

{_tool_ui_terms}

── ABOUT THE ANALYSED SOFTWARE ──
{_target_glossary}

You will be given DESCRIPTIONS of components. READ them, UNDERSTAND the purpose they serve, and SYNTHESIZE a clear explanation of the software's purpose and capabilities.

Use the graph UI terminology (nodes, edges, graphs, drill down, etc.) when describing how to explore the codebase. But do NOT name internal Python/JS class names or method names at this level — refer to capabilities and subsystems instead.

{_scene_schema}

Rules:
{_shared_rules}
- Generate {scene_count} scenes
- {walkthrough_scope}
- Opening scene: "zoom_out_all" — one clear sentence stating what the software is and does, referencing the graph the user is seeing
- Subsequent scenes: walk through the major CAPABILITIES — what can it do? Point out the relevant nodes/groups on the graph as you explain
- Final scene: "zoom_out_all" — summarize and mention how the user can drill down into any node to explore further
- Tone: clear, confident, professional. Every sentence must convey information.
- Refer to what the user can SEE on the graph — "this node represents...", "the edges between these nodes show...", "you can drill down into this group to see..."
- Do NOT just list component names — explain what each area DOES and how they connect"""

    elif level == "medium":
        scene_count = "6-12"
        system_msg = f"""You are narrating an architecture walkthrough for a developer who already knows what this software does and now wants to understand how it is built. Be direct and precise. Short sentences. No filler.

{_tool_ui_terms}

── ABOUT THE ANALYSED SOFTWARE ──
{_target_glossary}

You will be given DESCRIPTIONS of components at various levels. Use them as ground truth to explain the architecture: what the major subsystems are, what each one is responsible for, and how data flows between them. Name the nodes and groups visible on the graph. Refer to edges to explain relationships.

{_scene_schema}

Rules:
{_shared_rules}
- Generate {scene_count} scenes
- {walkthrough_scope}
- Opening scene: "zoom_out_all" — one sentence recapping the software's purpose, then transition to architecture
- Walk through logically: entry points → core processing → data layer → output
- Name the nodes and groups on the graph and explain what each handles
- Reference edges to explain how data flows between subsystems
- Mention where the user can drill down for more detail
- Final scene: "zoom_out_all" — summarize the architectural pattern
- Tone: direct, technical but accessible. Like a senior engineer briefing a new teammate while pointing at the architecture graph."""

    else:  # low
        scene_count = "10-18"
        system_msg = f"""You are narrating a detailed code walkthrough for a developer who wants to understand the implementation. Be precise and specific. Name methods, parameters, and return types. Trace call chains and data flow.

{_tool_ui_terms}

── ABOUT THE ANALYSED SOFTWARE ──
{_target_glossary}

You will be given DESCRIPTIONS of components including leaf-level methods. Use them to trace how specific operations work end-to-end. Reference the actual nodes and edges visible on the graph.

{_scene_schema}

Rules:
{_shared_rules}
- Generate {scene_count} scenes
- {walkthrough_scope}
- Opening scene: "zoom_out_all" — one sentence on the software's purpose
- Then trace specific operations: how does a request enter? What methods process it? What gets returned?
- Name specific methods, their parameters, and return values — point to the nodes on the graph
- Show call chains via edge_ids where relevant, explaining "this node calls that node"
- Mention drill-down paths: "if you drill down into this node, you'll see..."
- Final scene: "zoom_out_all" — key implementation patterns
- Tone: precise, technical. Like a senior engineer walking through the code while pointing at the architecture graph."""

    user_msg = (
        f"Generate a {level}-level architecture walkthrough.\n\n"
        f"── DESCRIPTIONS (ground truth) ──\n{descriptions_context}\n\n"
        f"── VISIBLE GRAPH NODES (for camera focus) ──\n{graph_ref}"
    )

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)

    try:
        resp = await client.chat.completions.create(
            model=_llm_model(),
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.4,
            max_completion_tokens=4000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        scenes = data.get("scenes", data.get("script", []))
        if isinstance(scenes, dict):
            scenes = [scenes]
        _preview_cache[cache_key] = scenes
        return {"scenes": scenes, "level": level, "cached": False}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Preview TTS audio endpoint ──────────────────────────────────────

_tts_audio_cache: dict[str, bytes] = {}  # text_hash → mp3 bytes


@app.post("/api/preview/audio")
async def preview_audio(request: Request):
    """Convert narration text to speech via OpenAI TTS.

    Body: {text: str}
    Returns: audio/mpeg stream
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "OPENAI_API_KEY not set"}, status_code=500)

    # Cache on text hash to avoid re-generating identical narrations
    text_hash = hashlib.md5(text.encode()).hexdigest()[:16]
    if text_hash in _tts_audio_cache:
        return Response(
            content=_tts_audio_cache[text_hash],
            media_type="audio/mpeg",
            headers={"Cache-Control": "max-age=3600"},
        )

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)

    try:
        resp = await client.audio.speech.create(
            model="tts-1",
            voice="onyx",          # deep, authoritative male voice
            input=text,
            speed=1.05,            # slightly faster = clipped military cadence
            response_format="mp3",
        )
        audio_bytes = resp.content
        _tts_audio_cache[text_hash] = audio_bytes
        return Response(
            content=audio_bytes,
            media_type="audio/mpeg",
            headers={"Cache-Control": "max-age=3600"},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Content classification enrichment endpoint ──────────────────────

_enrich_cache: dict[str, dict] = {}  # fingerprint → {node_id: content_type}


@app.post("/api/enrich")
async def enrich_nodes(request: Request):
    """Batch-classify nodes into content categories using the LLM.

    Body: {nodes: [{id, name, kind, file_path?, methods?, members?, docstring?}]}

    Returns: {classifications: {node_id: "icon_emoji"}, cached: bool}
    """
    body = await request.json()
    req_nodes = body.get("nodes", [])
    if not req_nodes:
        return {"classifications": {}, "cached": True}

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "OPENAI_API_KEY not set"}, status_code=500)

    # Build fingerprint for cache
    fp_input = json.dumps(
        sorted([{"id": n.get("id", ""), "name": n.get("name", "")} for n in req_nodes],
               key=lambda x: x["id"]),
        sort_keys=True,
    )
    fp = hashlib.md5(fp_input.encode()).hexdigest()[:16]
    if fp in _enrich_cache:
        return {"classifications": _enrich_cache[fp], "cached": True}

    abs_root = _active_codebase_path()

    # Build node descriptions for the LLM — include source snippets for leaf nodes
    elements = []
    for i, n in enumerate(req_nodes[:80]):  # cap at 80 nodes per batch
        parts = [f'{i+1}. id="{n.get("id", "")}", name="{n.get("name", "")}", kind={n.get("kind", "")}']
        if n.get("file_path"):
            parts.append(f'file={n["file_path"]}')
        if n.get("methods"):
            parts.append(f'methods=[{", ".join(n["methods"][:8])}]')
        if n.get("members"):
            parts.append(f'members=[{", ".join(n["members"][:8])}]')
        if n.get("member_methods"):
            parts.append(f'functions=[{", ".join(n["member_methods"][:8])}]')
        if n.get("docstring"):
            parts.append(f'doc="{n["docstring"][:80]}"')
        if n.get("bases"):
            parts.append(f'bases=[{", ".join(n["bases"][:4])}]')

        # Try to read a small snippet for leaf nodes
        if abs_root and n.get("file_path") and n.get("line_start") and n.get("line_end"):
            full = os.path.normpath(os.path.join(abs_root, n["file_path"]))
            if os.path.isfile(full):
                try:
                    lines = Path(full).read_text(encoding="utf-8", errors="replace").splitlines()
                    snippet_lines = lines[max(0, n["line_start"] - 1):min(n["line_end"], n["line_start"] + 15)]
                    snippet = "\n".join(snippet_lines).strip()
                    if snippet and len(snippet) < 400:
                        parts.append(f'code_preview="""{snippet}"""')
                except OSError:
                    pass

        elements.append(", ".join(parts))

    content_types_str = ", ".join(_CONTENT_TYPES_LIST)

    system_msg = f"""You are a code classification expert. Classify each code element into exactly one content category.

Valid categories: {content_types_str}

Output a JSON object with a single key "classifications" mapping each node id to its category string.
Example: {{"classifications": {{"MyRepo": "database", "AuthService": "auth", "utils": "utility"}}}}

Rules:
- Use ONLY the exact category strings listed above
- When uncertain, choose the most dominant purpose of the code
- "general" is the fallback when nothing else fits
- Be precise — look at method names, base classes, file paths, and code previews for clues"""

    user_msg = "Classify these code elements:\n\n" + "\n".join(elements)

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)

    try:
        resp = await client.chat.completions.create(
            model=_llm_model(),
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        classifications_raw = data.get("classifications", {})

        # Map category strings to emoji icons
        classifications = {}
        for node_id, category in classifications_raw.items():
            category = category.lower().strip()
            icon = _CONTENT_TYPE_MAP.get(category, ">_")
            classifications[node_id] = icon

        _enrich_cache[fp] = classifications
        return {"classifications": classifications, "cached": False}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/chat/models")
def get_chat_models():
    """Return available LLM models for the chat panel."""
    configured = _llm_model()
    models = [
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
    ]
    for m in models:
        m["default"] = m["id"] == configured
    return {"models": models}


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Stream an LLM chat response with tool-use. Body: {messages, model?, mode?}."""
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", _llm_model())
    mode = body.get("mode", "ask")  # "ask" or "agent"
    if mode not in ("ask", "agent"):
        mode = "ask"

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

    # Provide codebase context to chat tool implementations
    set_codebase_root(abs_path)
    hier_descs = _hier_desc_cache.get(abs_path) or (_load_desc_cache(abs_path) if abs_path else None)
    set_hier_descriptions(hier_descs)

    return StreamingResponse(
        stream_chat(messages, model=model, graph_dict=graph_dict, mode=mode),
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