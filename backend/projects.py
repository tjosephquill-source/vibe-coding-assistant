"""
Project registry — manages named projects, each pointing at a
filesystem directory to be analysed.

Projects are persisted in `.vca_cache/projects.json`.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from backend.analyzer import _ensure_cache_dir


# ── Data model ──────────────────────────────────────────────────────

@dataclass
class Project:
    project_id: str
    name: str
    path: str          # absolute filesystem path
    created_at: str    # ISO-8601


# ── Persistence ─────────────────────────────────────────────────────

def _projects_file() -> Path:
    return _ensure_cache_dir() / "projects.json"


def _load_all() -> list[dict]:
    path = _projects_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_all(projects: list[dict]) -> None:
    path = _projects_file()
    path.write_text(json.dumps(projects, indent=2), encoding="utf-8")


# ── Current project (in-memory) ────────────────────────────────────

_current_project_id: Optional[str] = None


def _state_file() -> Path:
    return _ensure_cache_dir() / "active_project.json"


def _load_current_id() -> Optional[str]:
    path = _state_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("current_project_id")
    except (json.JSONDecodeError, OSError):
        return None


def _persist_current_id(project_id: Optional[str]) -> None:
    path = _state_file()
    try:
        path.write_text(
            json.dumps({"current_project_id": project_id}), encoding="utf-8"
        )
    except OSError:
        pass


# ── Public API ──────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    """Return all registered projects."""
    return _load_all()


def get_current_project_id() -> Optional[str]:
    """Return the ID of the currently selected project."""
    global _current_project_id
    if _current_project_id is None:
        _current_project_id = _load_current_id()
    return _current_project_id


def get_current_project() -> Optional[dict]:
    """Return the dict for the currently active project, or None."""
    pid = get_current_project_id()
    if pid is None:
        return None
    for p in _load_all():
        if p["project_id"] == pid:
            return p
    return None


def set_current_project(project_id: str) -> bool:
    """Switch the active project.  Returns True if found."""
    global _current_project_id
    projects = _load_all()
    if not any(p["project_id"] == project_id for p in projects):
        return False
    _current_project_id = project_id
    _persist_current_id(project_id)
    return True


def create_project(name: str, path: str) -> dict:
    """Create a new project.  Raises ValueError on bad input."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(abs_path):
        raise ValueError(f"Directory not found: {abs_path}")

    project = Project(
        project_id=uuid.uuid4().hex[:12],
        name=name.strip() or os.path.basename(abs_path),
        path=abs_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    projects = _load_all()
    projects.append(asdict(project))
    _save_all(projects)

    # Auto-select the new project
    set_current_project(project.project_id)
    return asdict(project)


def delete_project(project_id: str) -> bool:
    """Remove a project from the registry.  Returns True if found."""
    global _current_project_id
    projects = _load_all()
    new = [p for p in projects if p["project_id"] != project_id]
    if len(new) == len(projects):
        return False
    _save_all(new)
    if _current_project_id == project_id:
        _current_project_id = new[0]["project_id"] if new else None
        _persist_current_id(_current_project_id)
    return True

