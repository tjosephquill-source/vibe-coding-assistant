"""FastAPI server — serves the analysis graph and the interactive frontend."""

import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from backend.analyzer import analyze_codebase, abstract_graph

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

    graph = analyze_codebase(abs_path)
    if abstract:
        graph = abstract_graph(graph, threshold=abstract_threshold)

    payload = graph.to_dict()
    _graph_cache[_cache_key(abs_path, abstract, abstract_threshold)] = payload
    return {"status": "ok", "nodes": len(payload["nodes"]), "edges": len(payload["edges"])}


@app.get("/api/graph")
def get_graph(
    abstract: bool = Query(default=False),
    abstract_threshold: int = Query(default=8, ge=2, le=500),
):
    """Return a cached graph (or auto-analyze mock_codebase2)."""
    mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mock_codebase2")
    target_path = mock_path if os.path.isdir(mock_path) else "mock_codebase"
    key = _cache_key(target_path, abstract, abstract_threshold)

    if key not in _graph_cache:
        abs_path = os.path.abspath(target_path)
        if not os.path.isdir(abs_path):
            return JSONResponse({"error": "No analysis run yet. POST /api/analyze first."}, status_code=404)
        graph = analyze_codebase(abs_path)
        if abstract:
            graph = abstract_graph(graph, threshold=abstract_threshold)
        _graph_cache[key] = graph.to_dict()

    return _graph_cache[key]


# Serve the frontend
_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.is_dir():
    @app.get("/", response_class=HTMLResponse)
    def serve_index():
        return (_frontend_dir / "index.html").read_text()

    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
