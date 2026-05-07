"""Tests for tools/obsidian_tool.py — the agent-facing tool surface."""

import json
from pathlib import Path

import pytest

# Importing the tool module triggers registry.register() at module load.
import tools.obsidian_tool  # noqa: F401  (registers tools)
from tools.obsidian_tool import (
    check_obsidian_requirements,
    obsidian_save,
    obsidian_search,
    obsidian_view,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "Vault"
    v.mkdir()
    (v / "hermes").mkdir()
    (v / "hermes" / "ingest").mkdir()
    (v / "hermes" / "rules.md").write_text(
        "Use pytest for everything.", encoding="utf-8"
    )
    (v / "hermes" / "ingest" / "redis.md").write_text(
        "Use scan instead of keys *", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_OBSIDIAN_VAULT", str(v))
    monkeypatch.delenv("HERMES_OBSIDIAN_SCOPE", raising=False)
    return v


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_unavailable_when_no_vault(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "x"))
        (tmp_path / "x").mkdir()
        assert check_obsidian_requirements() is False

    def test_available_with_vault(self, vault):
        assert check_obsidian_requirements() is True


# ---------------------------------------------------------------------------
# obsidian_search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_returns_json(self, vault):
        out = obsidian_search("pytest")
        data = json.loads(out)
        assert data["success"] is True
        assert data["query"] == "pytest"
        assert data["hit_count"] >= 1

    def test_empty_query_errors(self, vault):
        out = obsidian_search("")
        data = json.loads(out)
        assert data["success"] is False

    def test_unconfigured_returns_helpful_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "x"))
        (tmp_path / "x").mkdir()
        out = obsidian_search("anything")
        data = json.loads(out)
        assert data["success"] is False
        assert "obsidian" in data["error"].lower()


# ---------------------------------------------------------------------------
# obsidian_view
# ---------------------------------------------------------------------------

class TestView:
    def test_view_mirror_file(self, vault):
        out = obsidian_view("hermes/rules.md")
        data = json.loads(out)
        assert data["success"] is True
        assert "pytest" in data["content"]

    def test_view_traversal_refused(self, vault):
        out = obsidian_view("../../etc/passwd")
        data = json.loads(out)
        assert data["success"] is False


# ---------------------------------------------------------------------------
# obsidian_save
# ---------------------------------------------------------------------------

class TestSave:
    def test_writes_into_notes(self, vault):
        out = obsidian_save("debug-2026.md", "# Debug session\nfindings")
        data = json.loads(out)
        assert data["success"] is True
        assert (vault / "hermes" / "notes" / "debug-2026.md").is_file()

    def test_refuses_outside_hermes(self, vault):
        out = obsidian_save("../../学习笔记/x.md", "evil")
        data = json.loads(out)
        assert data["success"] is False
