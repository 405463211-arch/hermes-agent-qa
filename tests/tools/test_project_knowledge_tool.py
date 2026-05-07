"""Tests for tools/project_knowledge_tool.py — search / view / save."""

import json
import os
from pathlib import Path

import pytest

from tools.project_knowledge_tool import (
    PK_SEARCH_SCHEMA,
    PK_VIEW_SCHEMA,
    PK_SAVE_SCHEMA,
    project_knowledge_search,
    project_knowledge_view,
    project_knowledge_save,
)


@pytest.fixture()
def pk_dir(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    pk = home / "project-knowledge" / "demo"
    pk.mkdir(parents=True)
    return pk


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_search_schema_requires_query(self):
        assert PK_SEARCH_SCHEMA["parameters"]["required"] == ["query"]

    def test_view_schema_requires_relpath(self):
        assert "relpath" in PK_VIEW_SCHEMA["parameters"]["required"]

    def test_save_schema_requires_relpath_and_content(self):
        req = PK_SAVE_SCHEMA["parameters"]["required"]
        assert "relpath" in req
        assert "content" in req

    def test_save_schema_modes_are_write_and_append(self):
        modes = PK_SAVE_SCHEMA["parameters"]["properties"]["mode"]["enum"]
        assert set(modes) == {"write", "append"}


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_empty_query_rejected(self, pk_dir):
        result = json.loads(project_knowledge_search(query="", project="demo"))
        assert result["success"] is False

    def test_missing_pk_dir_returns_friendly_message(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        result = json.loads(project_knowledge_search(query="anything", project="brand-new"))
        assert result["success"] is True
        assert result["exists"] is False
        assert result["hits"] == []
        assert "No project-knowledge directory" in result["message"]

    def test_finds_matching_lines(self, pk_dir):
        (pk_dir / "a.md").write_text("the quick brown fox\nthe lazy dog\nfoxhound here")
        result = json.loads(project_knowledge_search(query="fox", project="demo"))
        assert result["success"] is True
        assert result["exists"] is True
        assert result["hit_count"] >= 2
        previews = [h["preview"] for h in result["hits"]]
        assert any("brown fox" in p for p in previews)

    def test_max_results_bounded(self, pk_dir):
        for i in range(20):
            (pk_dir / f"f{i:02d}.md").write_text(f"alpha {i}")
        result = json.loads(project_knowledge_search(
            query="alpha", project="demo", max_results=5,
        ))
        assert result["hit_count"] <= 5

    def test_returns_relative_paths(self, pk_dir):
        sub = pk_dir / "distilled"
        sub.mkdir()
        (sub / "x.md").write_text("findme")
        result = json.loads(project_knowledge_search(query="findme", project="demo"))
        # Should NOT include the absolute path components above pk_dir
        for h in result["hits"]:
            assert not h["path"].startswith("/")
            assert "distilled" in h["path"] or h["path"].endswith("x.md")


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

class TestView:
    def test_missing_relpath_rejected(self, pk_dir):
        result = json.loads(project_knowledge_view(relpath="", project="demo"))
        assert result["success"] is False

    def test_reads_existing_file(self, pk_dir):
        (pk_dir / "doc.md").write_text("line1\nline2\nline3\n")
        result = json.loads(project_knowledge_view(
            relpath="doc.md", project="demo",
        ))
        assert result["success"] is True
        assert "line1" in result["content"]
        assert result["total_lines"] == 3

    def test_offset_and_limit_paging(self, pk_dir):
        (pk_dir / "big.md").write_text("\n".join(f"line{i}" for i in range(100)) + "\n")
        result = json.loads(project_knowledge_view(
            relpath="big.md", offset=10, limit=5, project="demo",
        ))
        assert result["success"] is True
        assert result["lines_returned"] == 5
        # 1-indexed offset → first returned line is line9 (index 9)
        assert "line9" in result["content"]
        assert "line13" in result["content"]
        assert "line50" not in result["content"]
        assert result["more_available"] is True

    def test_path_traversal_blocked(self, pk_dir):
        # Create a sensitive file outside the PK dir to ensure it's not
        # readable via "../" tricks
        sensitive = pk_dir.parent / "secret.md"
        sensitive.write_text("password = hunter2")
        result = json.loads(project_knowledge_view(
            relpath="../secret.md", project="demo",
        ))
        assert result["success"] is False
        assert "not inside" in result["error"].lower()

    def test_absolute_path_blocked(self, pk_dir, tmp_path):
        result = json.loads(project_knowledge_view(
            relpath="/etc/passwd", project="demo",
        ))
        assert result["success"] is False

    def test_nonexistent_file_clean_error(self, pk_dir):
        result = json.loads(project_knowledge_view(
            relpath="ghost.md", project="demo",
        ))
        assert result["success"] is False
        assert "Not a file" in result["error"]


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------

class TestSave:
    def test_write_creates_file(self, pk_dir):
        result = json.loads(project_knowledge_save(
            relpath="distilled/notes.md",
            content="# Notes\nSome content",
            project="demo",
        ))
        assert result["success"] is True
        target = pk_dir / "distilled" / "notes.md"
        assert target.is_file()
        assert "Some content" in target.read_text()

    def test_append_grows_file(self, pk_dir):
        path = pk_dir / "log.md"
        path.write_text("first\n")
        result = json.loads(project_knowledge_save(
            relpath="log.md", content="second\n", mode="append", project="demo",
        ))
        assert result["success"] is True
        assert path.read_text() == "first\nsecond\n"

    def test_traversal_rejected(self, pk_dir):
        result = json.loads(project_knowledge_save(
            relpath="../escape.md", content="x", project="demo",
        ))
        assert result["success"] is False

    def test_absolute_path_rejected(self, pk_dir):
        result = json.loads(project_knowledge_save(
            relpath="/etc/payload.md", content="x", project="demo",
        ))
        assert result["success"] is False

    def test_creates_pk_dir_on_first_save(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # Project dir doesn't exist yet — save must bootstrap it
        result = json.loads(project_knowledge_save(
            relpath="first.md", content="Hello", project="brand-new",
        ))
        assert result["success"] is True
        assert (home / "project-knowledge" / "brand-new" / "first.md").is_file()

    def test_invalid_mode_rejected(self, pk_dir):
        result = json.loads(project_knowledge_save(
            relpath="x.md", content="y", mode="overwrite", project="demo",
        ))
        assert result["success"] is False
