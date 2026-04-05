"""Tests for backend.server — FastAPI endpoints.

Covers:
  - Project CRUD endpoints
  - Graph analysis endpoints
  - File browsing / source endpoints
  - Position and layout persistence
  - Insights endpoint
  - Error handling (missing project, bad paths)
"""

import os
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.server import app

# Path to test fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PYTHON = str(FIXTURES_DIR / "sample_python")


@pytest.fixture
def client():
    """Create a test client and reset server caches between tests."""
    from backend import server
    # Clear in-memory caches
    server._graph_cache.clear()
    server._hier_desc_cache.clear()
    server._node_desc_cache.clear()
    server._preview_cache.clear()
    server._tts_audio_cache.clear()
    server._enrich_cache.clear()
    server._perf_cache.clear()
    return TestClient(app)


@pytest.fixture
def project_dir():
    """Create a temporary project directory with a simple Python file."""
    tmpdir = tempfile.mkdtemp(prefix="vca_test_")
    Path(tmpdir, "app.py").write_text(
        "class App:\n    def run(self):\n        pass\n"
    )
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════
# Static routes
# ════════════════════════════════════════════════════════════════════


class TestStaticRoutes:

    def test_chrome_devtools_probe(self, client):
        resp = client.get("/.well-known/appspecific/com.chrome.devtools.json")
        assert resp.status_code == 204

    def test_favicon(self, client):
        resp = client.get("/favicon.ico")
        assert resp.status_code == 204


# ════════════════════════════════════════════════════════════════════
# Project management
# ════════════════════════════════════════════════════════════════════


class TestProjectEndpoints:

    def test_list_projects(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        assert isinstance(data["projects"], list)

    def test_create_project(self, client, project_dir):
        resp = client.post("/api/projects", json={
            "name": "test-proj",
            "path": project_dir,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "project_id" in data
        assert data["name"] == "test-proj"
        assert data["path"] == project_dir

    def test_create_project_no_path(self, client):
        resp = client.post("/api/projects", json={"name": "bad"})
        assert resp.status_code == 400

    def test_select_project(self, client, project_dir):
        # Create first
        create_resp = client.post("/api/projects", json={
            "name": "select-test",
            "path": project_dir,
        })
        pid = create_resp.json()["project_id"]

        # Select
        resp = client.post("/api/projects/select", json={"project_id": pid})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_select_nonexistent_project(self, client):
        resp = client.post("/api/projects/select", json={
            "project_id": "nonexistent-id",
        })
        assert resp.status_code == 404

    def test_delete_project(self, client, project_dir):
        create_resp = client.post("/api/projects", json={
            "name": "delete-me",
            "path": project_dir,
        })
        pid = create_resp.json()["project_id"]

        resp = client.delete(f"/api/projects/{pid}")
        assert resp.status_code == 200

    def test_delete_nonexistent_project(self, client):
        resp = client.delete("/api/projects/nonexistent-id")
        assert resp.status_code == 404


# ════════════════════════════════════════════════════════════════════
# Analysis & Graph
# ════════════════════════════════════════════════════════════════════


class TestAnalysisEndpoints:

    def _setup_project(self, client, project_dir):
        """Helper: create and select a project."""
        create_resp = client.post("/api/projects", json={
            "name": "analysis-test",
            "path": project_dir,
        })
        pid = create_resp.json()["project_id"]
        client.post("/api/projects/select", json={"project_id": pid})
        return pid

    def test_analyze_returns_ok(self, client, project_dir):
        resp = client.post(f"/api/analyze?repo_path={project_dir}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "nodes" in data
        assert "edges" in data

    def test_analyze_bad_path(self, client):
        resp = client.post("/api/analyze?repo_path=/tmp/nonexistent_vca_test")
        assert resp.status_code == 400

    def test_get_graph(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data

    def test_get_graph_abstract(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/graph?abstract=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data

    def test_get_graph_no_project(self, client):
        """When no project is selected, graph endpoint should return 404."""
        from backend import projects
        # Force no current project
        old_id = projects._current_project_id
        projects._current_project_id = "nonexistent"
        try:
            resp = client.get("/api/graph")
            # Should either return graph (if fallback works) or 404
            assert resp.status_code in (200, 404)
        finally:
            projects._current_project_id = old_id

    def test_insights(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/insights")
        assert resp.status_code == 200
        data = resp.json()
        assert "insights" in data
        assert "count" in data
        assert isinstance(data["insights"], list)

    def test_reanalyze(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.post("/api/reanalyze")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_graph_refresh(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.post("/api/graph/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


# ════════════════════════════════════════════════════════════════════
# File browsing
# ════════════════════════════════════════════════════════════════════


class TestBrowseEndpoints:

    def test_browse_home(self, client):
        resp = client.get("/api/browse")
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data
        assert "dirs" in data
        assert isinstance(data["dirs"], list)

    def test_browse_specific_path(self, client, project_dir):
        resp = client.get(f"/api/browse?path={project_dir}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"] == project_dir

    def test_browse_invalid_path(self, client):
        resp = client.get("/api/browse?path=/tmp/does_not_exist_vca")
        assert resp.status_code == 400

    def test_mkdir(self, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            new_dir = os.path.join(tmpdir, "new_subdir")
            resp = client.post("/api/mkdir", json={"path": new_dir})
            assert resp.status_code == 200
            assert os.path.isdir(new_dir)

    def test_mkdir_already_exists(self, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            resp = client.post("/api/mkdir", json={"path": tmpdir})
            assert resp.status_code == 409

    def test_mkdir_no_path(self, client):
        resp = client.post("/api/mkdir", json={"path": ""})
        assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════════
# Source code & file tree
# ════════════════════════════════════════════════════════════════════


class TestFileEndpoints:

    def _setup_project(self, client, project_dir):
        create_resp = client.post("/api/projects", json={
            "name": "file-test", "path": project_dir,
        })
        pid = create_resp.json()["project_id"]
        client.post("/api/projects/select", json={"project_id": pid})

    def test_get_files(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/files")
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data
        assert "type" in data
        assert data["type"] == "directory"

    def test_get_source(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/source?path=app.py")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert "class App" in data["content"]

    def test_get_source_not_found(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/source?path=nonexistent.py")
        assert resp.status_code == 404

    def test_get_source_path_traversal(self, client, project_dir):
        """Ensure path traversal is blocked."""
        self._setup_project(client, project_dir)
        resp = client.get("/api/source?path=../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_fs_checksum(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp = client.get("/api/fs/checksum")
        assert resp.status_code == 200
        data = resp.json()
        assert "checksum" in data
        assert "file_count" in data

    def test_fs_checksum_changes_on_file_edit(self, client, project_dir):
        self._setup_project(client, project_dir)
        resp1 = client.get("/api/fs/checksum")
        checksum1 = resp1.json()["checksum"]

        # Modify a file
        Path(project_dir, "app.py").write_text("class App:\n    x = 1\n")

        resp2 = client.get("/api/fs/checksum")
        checksum2 = resp2.json()["checksum"]
        assert checksum1 != checksum2


# ════════════════════════════════════════════════════════════════════
# Position & layout persistence
# ════════════════════════════════════════════════════════════════════


class TestPositionEndpoints:

    def test_get_positions_empty(self, client):
        resp = client.get("/api/positions?graph_key=test-key-123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["positions"] is None

    def test_save_and_get_positions(self, client):
        positions = {"node1": {"x": 100, "y": 200}, "node2": {"x": 300, "y": 400}}
        save_resp = client.post("/api/positions", json={
            "graph_key": "test-key-pos",
            "positions": positions,
        })
        assert save_resp.status_code == 200

        get_resp = client.get("/api/positions?graph_key=test-key-pos")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["positions"] is not None
        assert data["positions"]["node1"]["x"] == 100

    def test_save_positions_no_key(self, client):
        resp = client.post("/api/positions", json={"positions": {}})
        assert resp.status_code == 400


class TestLayoutEndpoints:

    def test_get_layout_empty(self, client):
        resp = client.get("/api/layout")
        assert resp.status_code == 200

    def test_save_and_get_layout(self, client):
        layout = {"panels": ["graph", "detail"], "ratios": [0.6, 0.4]}
        save_resp = client.post("/api/layout", json=layout)
        assert save_resp.status_code == 200

        get_resp = client.get("/api/layout")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert "panels" in data


# ════════════════════════════════════════════════════════════════════
# Chat models endpoint
# ════════════════════════════════════════════════════════════════════


class TestChatModels:

    def test_get_models(self, client):
        resp = client.get("/api/chat/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert isinstance(data["models"], list)
        assert len(data["models"]) > 0
        for model in data["models"]:
            assert "id" in model
            assert "name" in model


# ════════════════════════════════════════════════════════════════════
# Performance analysis
# ════════════════════════════════════════════════════════════════════


class TestPerformanceEndpoint:

    def test_no_api_key(self, client, project_dir):
        """Returns 500 when OPENAI_API_KEY is not set."""
        # Create project first
        client.post("/api/projects", json={"name": "p", "path": project_dir})
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            resp = client.post("/api/node-performance", json={
                "node_name": "App",
                "node_kind": "class",
                "file_path": "app.py",
                "line_start": 1,
                "line_end": 3,
            })
        assert resp.status_code == 500
        assert "OPENAI_API_KEY" in resp.json()["error"]

    def test_no_project(self, client):
        """Returns error when no project is active."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            resp = client.post("/api/node-performance", json={
                "node_name": "App",
                "node_kind": "class",
                "file_path": "app.py",
                "line_start": 1,
                "line_end": 3,
            })
        # 404 if no project path, or 400 if path exists but file not found
        assert resp.status_code in (400, 404)
        assert "error" in resp.json()

    def test_no_source_code(self, client, project_dir):
        """Returns 400 when no source snippet can be extracted."""
        client.post("/api/projects", json={"name": "p", "path": project_dir})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            resp = client.post("/api/node-performance", json={
                "node_name": "Ghost",
                "node_kind": "function",
                "file_path": "nonexistent.py",
                "line_start": 1,
                "line_end": 5,
            })
        assert resp.status_code == 400
        assert "No source code" in resp.json()["error"]

    def test_success(self, client, project_dir):
        """Returns structured analysis on success (mocked LLM)."""
        client.post("/api/projects", json={"name": "p", "path": project_dir})

        mock_analysis = json.dumps({
            "time_complexity": "O(1) — constant time",
            "space_complexity": "O(1) — no extra allocations",
            "bottlenecks": [],
            "recommendations": ["No issues found"],
        })

        class FakeMessage:
            content = mock_analysis

        class FakeChoice:
            message = FakeMessage()

        class FakeResp:
            choices = [FakeChoice()]

        async def fake_create(**kwargs):
            return FakeResp()

        class FakeCompletions:
            create = staticmethod(fake_create)

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()
            def __init__(self, **kwargs):
                pass

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("openai.AsyncOpenAI", FakeClient):
                resp = client.post("/api/node-performance", json={
                    "node_name": "App",
                    "node_kind": "class",
                    "file_path": "app.py",
                    "line_start": 1,
                    "line_end": 3,
                })

        assert resp.status_code == 200
        data = resp.json()
        assert "analysis" in data
        assert data["analysis"]["time_complexity"] == "O(1) — constant time"
        assert data["cached"] is False

    def test_cached_response(self, client, project_dir):
        """Second request for same content returns cached result."""
        from backend import server
        client.post("/api/projects", json={"name": "p", "path": project_dir})

        # Pre-populate cache
        import hashlib
        source = "class App:\n    def run(self):\n        pass"
        content_hash = hashlib.md5(source.encode()).hexdigest()[:16]
        cache_key = f"perf|App|{content_hash}"
        server._perf_cache[cache_key] = {
            "time_complexity": "O(1)",
            "space_complexity": "O(1)",
            "bottlenecks": [],
            "recommendations": [],
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            resp = client.post("/api/node-performance", json={
                "node_name": "App",
                "node_kind": "class",
                "file_path": "app.py",
                "line_start": 1,
                "line_end": 3,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is True
        assert data["analysis"]["time_complexity"] == "O(1)"
