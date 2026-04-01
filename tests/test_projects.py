"""Tests for backend.projects — project registry.

Covers:
  - Project creation, listing, selection, deletion
  - Persistence to disk
  - Edge cases (duplicate paths, invalid IDs)
"""

import tempfile

import pytest

from backend.projects import (
    list_projects,
    create_project,
    delete_project,
    get_current_project,
    get_current_project_id,
    set_current_project,
    _projects_file,
    _load_all,
    _save_all,
)



class TestProjectCRUD:

    def test_create_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = create_project("Test Project", tmpdir)
            assert proj["name"] == "Test Project"
            assert proj["path"] == tmpdir
            assert "project_id" in proj
            assert "created_at" in proj

    def test_create_project_invalid_path(self):
        with pytest.raises(ValueError):
            create_project("Bad", "/tmp/nonexistent_vca_path_999")

    def test_list_projects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            create_project("P1", tmpdir)
            projects = list_projects()
            names = [p["name"] for p in projects]
            assert "P1" in names

    def test_delete_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = create_project("ToDelete", tmpdir)
            pid = proj["project_id"]
            assert delete_project(pid) is True
            # Should no longer be in list
            projects = list_projects()
            ids = [p["project_id"] for p in projects]
            assert pid not in ids

    def test_delete_nonexistent(self):
        result = delete_project("fake-id-12345")
        assert result is False

    def test_select_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = create_project("Selectable", tmpdir)
            pid = proj["project_id"]
            result = set_current_project(pid)
            assert result is True
            assert get_current_project_id() == pid

    def test_select_nonexistent(self):
        result = set_current_project("nonexistent-id")
        assert result is False

    def test_get_current_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = create_project("Current", tmpdir)
            pid = proj["project_id"]
            set_current_project(pid)
            current = get_current_project()
            assert current is not None
            assert current["project_id"] == pid
            assert current["name"] == "Current"


class TestProjectPersistence:

    def test_save_and_load(self):
        """Projects should persist across save/load cycles."""
        original = [
            {"project_id": "id1", "name": "P1", "path": "/tmp", "created_at": "2025-01-01"},
        ]
        _save_all(original)
        loaded = _load_all()
        assert len(loaded) == 1
        assert loaded[0]["name"] == "P1"

    def test_load_empty(self):
        """Loading from a missing file should return empty list."""
        pf = _projects_file()
        if pf.exists():
            pf.unlink()
        result = _load_all()
        assert result == []
