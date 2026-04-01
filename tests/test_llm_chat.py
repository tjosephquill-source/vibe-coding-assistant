"""Tests for backend.llm_chat — tool dispatch and implementations.

Covers:
  - Tool dispatch routing
  - read_file, list_files, search_code implementations
  - write_file, edit_file, delete_file implementations (agent mode)
  - get_graph_summary tool
  - navigate_to_node and open_file_in_editor (UI commands)
  - Path safety (no escaping codebase root)
"""

import os
import json
import tempfile
import shutil
from pathlib import Path

import pytest

from backend.llm_chat import (
    dispatch_tool,
    set_codebase_root,
    set_hier_descriptions,
    _tool_read_file,
    _tool_list_files,
    _tool_search_code,
    _tool_write_file,
    _tool_edit_file,
    _tool_delete_file,
    _tool_get_graph_summary,
    _tool_navigate_to_node,
    _tool_open_file_in_editor,
    _tool_get_architecture_overview,
    ASK_TOOLS,
    AGENT_TOOLS,
)


@pytest.fixture
def codebase(tmp_path):
    """Create a temporary codebase with a few files."""
    (tmp_path / "main.py").write_text("class Main:\n    def run(self):\n        pass\n")
    (tmp_path / "utils.py").write_text("def helper():\n    return 42\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("class Nested:\n    pass\n")
    set_codebase_root(str(tmp_path))
    yield tmp_path
    set_codebase_root(None)


# ════════════════════════════════════════════════════════════════════
# Tool sets
# ════════════════════════════════════════════════════════════════════


class TestToolSets:

    def test_ask_tools_are_read_only(self):
        tool_names = {t["function"]["name"] for t in ASK_TOOLS}
        assert "read_file" in tool_names
        assert "list_files" in tool_names
        assert "search_code" in tool_names
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "delete_file" not in tool_names

    def test_agent_tools_include_write(self):
        tool_names = {t["function"]["name"] for t in AGENT_TOOLS}
        assert "write_file" in tool_names
        assert "edit_file" in tool_names
        assert "delete_file" in tool_names
        # Also includes read tools
        assert "read_file" in tool_names
        assert "search_code" in tool_names


# ════════════════════════════════════════════════════════════════════
# Read tools
# ════════════════════════════════════════════════════════════════════


class TestReadFile:

    def test_read_existing_file(self, codebase):
        result = json.loads(_tool_read_file("main.py"))
        assert "content" in result
        assert "class Main" in result["content"]

    def test_read_nested_file(self, codebase):
        result = json.loads(_tool_read_file("sub/nested.py"))
        assert "class Nested" in result["content"]

    def test_read_nonexistent_file(self, codebase):
        result = json.loads(_tool_read_file("nonexistent.py"))
        assert "error" in result

    def test_path_traversal_blocked(self, codebase):
        result = json.loads(_tool_read_file("../../etc/passwd"))
        assert "error" in result


class TestListFiles:

    def test_lists_code_files(self, codebase):
        result = json.loads(_tool_list_files())
        assert "files" in result
        files = result["files"]
        assert "main.py" in files
        assert "utils.py" in files

    def test_includes_nested(self, codebase):
        result = json.loads(_tool_list_files())
        files = result["files"]
        nested = [f for f in files if "nested" in f]
        assert len(nested) >= 1

    def test_count_matches(self, codebase):
        result = json.loads(_tool_list_files())
        assert result["count"] == len(result["files"])


class TestSearchCode:

    def test_finds_match(self, codebase):
        result = json.loads(_tool_search_code("class Main"))
        assert len(result["matches"]) >= 1
        assert result["matches"][0]["file"] == "main.py"

    def test_case_insensitive(self, codebase):
        result = json.loads(_tool_search_code("CLASS MAIN"))
        assert len(result["matches"]) >= 1

    def test_no_matches(self, codebase):
        result = json.loads(_tool_search_code("zzzzzznotfound"))
        assert len(result["matches"]) == 0

    def test_matches_include_line_numbers(self, codebase):
        result = json.loads(_tool_search_code("helper"))
        for match in result["matches"]:
            assert "line" in match
            assert "file" in match
            assert "text" in match


# ════════════════════════════════════════════════════════════════════
# Write tools (agent mode)
# ════════════════════════════════════════════════════════════════════


class TestWriteFile:

    def test_create_new_file(self, codebase):
        result = json.loads(_tool_write_file("new_file.py", "print('hello')\n"))
        assert result["status"] == "ok"
        assert result["file_change"]["type"] == "created"
        assert (codebase / "new_file.py").read_text() == "print('hello')\n"

    def test_overwrite_existing_file(self, codebase):
        result = json.loads(_tool_write_file("main.py", "# rewritten\n"))
        assert result["status"] == "ok"
        assert result["file_change"]["type"] == "modified"
        assert (codebase / "main.py").read_text() == "# rewritten\n"

    def test_creates_parent_dirs(self, codebase):
        result = json.loads(_tool_write_file("deep/nested/file.py", "x = 1\n"))
        assert result["status"] == "ok"
        assert (codebase / "deep" / "nested" / "file.py").exists()

    def test_path_traversal_blocked(self, codebase):
        result = json.loads(_tool_write_file("../../escape.py", "bad"))
        assert "error" in result


class TestEditFile:

    def test_successful_edit(self, codebase):
        result = json.loads(_tool_edit_file(
            "main.py",
            "    def run(self):\n        pass",
            "    def run(self):\n        print('running')",
        ))
        assert result["status"] == "ok"
        assert result["replacements"] == 1
        content = (codebase / "main.py").read_text()
        assert "print('running')" in content

    def test_old_text_not_found(self, codebase):
        result = json.loads(_tool_edit_file(
            "main.py", "nonexistent text", "replacement",
        ))
        assert "error" in result

    def test_ambiguous_match(self, codebase):
        # Write a file with duplicate content
        (codebase / "dup.py").write_text("x = 1\nx = 1\n")
        result = json.loads(_tool_edit_file("dup.py", "x = 1", "x = 2"))
        assert "error" in result
        assert "2 locations" in result["error"]

    def test_edit_nonexistent_file(self, codebase):
        result = json.loads(_tool_edit_file("nope.py", "old", "new"))
        assert "error" in result

    def test_edit_includes_diff(self, codebase):
        result = json.loads(_tool_edit_file(
            "utils.py", "return 42", "return 99",
        ))
        assert "diff" in result


class TestDeleteFile:

    def test_delete_existing(self, codebase):
        assert (codebase / "utils.py").exists()
        result = json.loads(_tool_delete_file("utils.py"))
        assert result["status"] == "ok"
        assert result["file_change"]["type"] == "deleted"
        assert not (codebase / "utils.py").exists()

    def test_delete_nonexistent(self, codebase):
        result = json.loads(_tool_delete_file("nope.py"))
        assert "error" in result

    def test_delete_path_traversal(self, codebase):
        result = json.loads(_tool_delete_file("../../important.py"))
        assert "error" in result


# ════════════════════════════════════════════════════════════════════
# Graph summary tool
# ════════════════════════════════════════════════════════════════════


class TestGraphSummary:

    def test_with_graph_data(self):
        graph_dict = {
            "nodes": [
                {"name": "A", "kind": "class"},
                {"name": "B", "kind": "class"},
                {"name": "m", "kind": "method"},
            ],
            "edges": [
                {"kind": "inherits"},
                {"kind": "calls"},
            ],
        }
        result = json.loads(_tool_get_graph_summary(graph_dict))
        assert result["total_nodes"] == 3
        assert result["total_edges"] == 2
        assert result["nodes_by_kind"]["class"] == 2
        assert result["nodes_by_kind"]["method"] == 1

    def test_without_graph_data(self):
        result = json.loads(_tool_get_graph_summary(None))
        assert "error" in result


# ════════════════════════════════════════════════════════════════════
# UI command tools
# ════════════════════════════════════════════════════════════════════


class TestUITools:

    def test_navigate_to_node(self):
        result = json.loads(_tool_navigate_to_node("MyClass"))
        assert result["action"] == "navigate_to_node"
        assert result["node_name"] == "MyClass"

    def test_open_file_in_editor(self):
        result = json.loads(_tool_open_file_in_editor("main.py", 5, 10))
        assert result["action"] == "open_file_in_editor"
        assert result["path"] == "main.py"
        assert result["line_start"] == 5
        assert result["line_end"] == 10

    def test_open_file_no_lines(self):
        result = json.loads(_tool_open_file_in_editor("main.py"))
        assert result["path"] == "main.py"
        assert result["line_start"] is None


# ════════════════════════════════════════════════════════════════════
# Architecture overview tool
# ════════════════════════════════════════════════════════════════════


class TestArchitectureOverview:

    def test_no_descriptions(self):
        set_hier_descriptions(None)
        result = json.loads(_tool_get_architecture_overview())
        assert "error" in result

    def test_with_descriptions(self):
        descs = {
            "__root__": {
                "description": "A web app",
                "children_ids": ["comp1"],
                "name": "Root",
                "kind": "root",
            },
            "comp1": {
                "description": "The API layer",
                "children_ids": [],
                "name": "API",
                "kind": "group",
            },
        }
        set_hier_descriptions(descs)
        result = json.loads(_tool_get_architecture_overview("high"))
        assert "codebase_overview" in result
        assert result["codebase_overview"] == "A web app"
        assert len(result["components"]) == 1
        set_hier_descriptions(None)


# ════════════════════════════════════════════════════════════════════
# Dispatch routing
# ════════════════════════════════════════════════════════════════════


class TestDispatch:

    def test_unknown_tool(self, codebase):
        result = json.loads(dispatch_tool("nonexistent_tool", {}))
        assert "error" in result

    def test_dispatch_read_file(self, codebase):
        result = json.loads(dispatch_tool("read_file", {"path": "main.py"}))
        assert "content" in result

    def test_dispatch_list_files(self, codebase):
        result = json.loads(dispatch_tool("list_files", {}))
        assert "files" in result

    def test_dispatch_search_code(self, codebase):
        result = json.loads(dispatch_tool("search_code", {"query": "class"}))
        assert "matches" in result

    def test_dispatch_write_file(self, codebase):
        result = json.loads(dispatch_tool("write_file", {
            "path": "dispatch_test.py", "content": "x = 1\n",
        }))
        assert result["status"] == "ok"

    def test_dispatch_navigate(self, codebase):
        result = json.loads(dispatch_tool("navigate_to_node", {"node_name": "Foo"}))
        assert result["action"] == "navigate_to_node"

