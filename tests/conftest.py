"""Shared pytest configuration and fixtures for the VibeCodingAssistant test suite."""

import os
import tempfile
import shutil
from pathlib import Path

import pytest

# Exclude fixture directories from test collection
collect_ignore_glob = ["fixtures/*"]


@pytest.fixture(autouse=True)
def _restore_project_registry():
    """Save and restore the project registry file around every test.

    This prevents test-created projects from leaking into the real
    registry or affecting other tests.
    """
    from backend.projects import _projects_file, _state_file
    import backend.projects as _proj_mod

    pf = _projects_file()
    sf = _state_file()

    pf_backup = pf.read_text() if pf.exists() else None
    sf_backup = sf.read_text() if sf.exists() else None
    old_pid = _proj_mod._current_project_id

    yield

    # Restore
    if pf_backup is not None:
        pf.write_text(pf_backup)
    elif pf.exists():
        pf.unlink()

    if sf_backup is not None:
        sf.write_text(sf_backup)
    elif sf.exists():
        sf.unlink()

    _proj_mod._current_project_id = old_pid


@pytest.fixture
def sample_python_dir():
    """Path to the sample Python fixture directory."""
    return Path(__file__).parent / "fixtures" / "sample_python"


@pytest.fixture
def sample_js_dir():
    """Path to the sample JavaScript fixture directory."""
    return Path(__file__).parent / "fixtures" / "sample_js"


@pytest.fixture
def tmp_codebase(tmp_path):
    """Create a temporary codebase directory with a few Python files.

    Returns the path to the temporary directory.
    """
    (tmp_path / "app.py").write_text(
        "class App:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "    def stop(self):\n"
        "        pass\n"
    )
    (tmp_path / "utils.py").write_text(
        "class Helper:\n"
        "    def assist(self):\n"
        "        return True\n"
    )
    return tmp_path

