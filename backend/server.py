"""FastAPI server — serves the analysis graph and the interactive frontend."""

import os
import json
import hashlib
from pathlib import Path

from fastapi import FastAPI, Query, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from backend.analyzer import (
    analyze_codebase, abstract_graph,
    _load_from_cache, _save_to_cache, _ensure_cache_dir,
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


def _cache_key(repo_path: str, abstract: bool, abstract_threshold: int) -> str:
    return f"{os.path.abspath(repo_path)}|{int(abstract)}|{abstract_threshold}"


def _get_or_analyze(repo_path: str, abstract: bool, abstract_threshold: int) -> dict:
    """Return graph dict from memory cache → disk cache → fresh analysis."""
    abs_path = os.path.abspath(repo_path)
    key = _cache_key(abs_path, abstract, abstract_threshold)

    # 1. Memory cache
    if key in _graph_cache:
        return _graph_cache[key]

    # 2. Disk cache
    cached = _load_from_cache(abs_path, abstract, abstract_threshold)
    if cached is not None:
        _graph_cache[key] = cached
        return cached

    # 3. Fresh analysis
    graph = analyze_codebase(abs_path)
    if abstract:
        graph = abstract_graph(graph, threshold=abstract_threshold)

    payload = graph.to_dict()
    _graph_cache[key] = payload
    _save_to_cache(abs_path, abstract, abstract_threshold, payload)
    return payload


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
    """Silence Chrome DevTools probe 404s."""
    return Response(status_code=204)


@app.post("/api/analyze")
def analyze(
    repo_path: str = Query(default="mock_codebase"),
    abstract: bool = Query(default=False),
    abstract_threshold: int = Query(default=8, ge=2, le=500),
):
    """Analyze a codebase and cache the resulting graph."""
    abs_path = os.path.abspath(repo_path)
    if not os.path.isdir(abs_path):
        return JSONResponse({"error": f"Directory not found: {abs_path}"}, status_code=400)

    payload = _get_or_analyze(abs_path, abstract, abstract_threshold)
    return {"status": "ok", "nodes": len(payload["nodes"]), "edges": len(payload["edges"])}


@app.get("/api/graph")
def get_graph(
    abstract: bool = Query(default=False),
    abstract_threshold: int = Query(default=8, ge=2, le=500),
):
    """Return a cached graph (or auto-analyze mock_codebase2)."""
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    abs_path = os.path.abspath(target_path)

    if not os.path.isdir(abs_path):
        return JSONResponse({"error": "No analysis run yet. POST /api/analyze first."}, status_code=404)

    payload = _get_or_analyze(abs_path, abstract, abstract_threshold)
    return payload


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


# Serve the frontend
_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.is_dir():
    @app.get("/", response_class=HTMLResponse)
    def serve_index():
        return (_frontend_dir / "index.html").read_text()

    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")