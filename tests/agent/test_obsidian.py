"""Tests for agent/obsidian.py — vault path resolution, scope, search, save."""

from pathlib import Path

import pytest

from agent import obsidian as ob


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Create a fake vault and point HERMES_OBSIDIAN_VAULT at it."""
    vault_dir = tmp_path / "MyVault"
    vault_dir.mkdir()
    (vault_dir / ".obsidian").mkdir()  # marker
    # Some user notes outside hermes/
    (vault_dir / "学习笔记").mkdir()
    (vault_dir / "学习笔记" / "asyncio.md").write_text(
        "# Asyncio gather\n用 asyncio.gather 并发执行多个 coroutine。\n",
        encoding="utf-8",
    )
    # Hermes-managed subdir
    (vault_dir / "hermes").mkdir()
    (vault_dir / "hermes" / "ingest").mkdir()
    (vault_dir / "hermes" / "notes").mkdir()
    (vault_dir / "hermes" / "rules.md").write_text(
        "# Hermes Rules\nAlways write tests.\n", encoding="utf-8"
    )
    # An ingest file the user explicitly opted in
    (vault_dir / "hermes" / "ingest" / "postgres.md").write_text(
        "# Postgres tuning\nset work_mem to 32MB for analytics.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("HERMES_OBSIDIAN_SCOPE", raising=False)
    return vault_dir


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Provide an isolated HERMES_HOME for export tests."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Config / path resolution
# ---------------------------------------------------------------------------

class TestConfig:
    def test_disabled_when_no_env_or_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)
        # Ensure load_config can't pick up a real user config.yaml
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty_home"))
        (tmp_path / "empty_home").mkdir()
        assert not ob.is_enabled()

    def test_env_var_overrides_config(self, vault):
        assert ob.is_enabled()
        assert ob.get_vault_path() == vault

    def test_get_hermes_dir_creates_subdir(self, tmp_path, monkeypatch):
        v = tmp_path / "freshVault"
        v.mkdir()
        monkeypatch.setenv("HERMES_OBSIDIAN_VAULT", str(v))
        hermes = ob.get_hermes_dir()
        assert hermes is not None
        assert hermes.is_dir()
        assert hermes == v / "hermes"

    def test_default_scope(self, vault):
        assert ob.get_search_scope() == ob.SCOPE_HERMES_SUBDIR


# ---------------------------------------------------------------------------
# Path containment safety
# ---------------------------------------------------------------------------

class TestPathSafety:
    def test_resolve_inside_vault_rejects_absolute(self, vault):
        assert ob._resolve_inside_vault("/etc/passwd") is None

    def test_resolve_inside_vault_rejects_traversal(self, vault):
        assert ob._resolve_inside_vault("../outside.md") is None
        assert ob._resolve_inside_vault("hermes/../../escape.md") is None

    def test_resolve_inside_vault_accepts_normal(self, vault):
        result = ob._resolve_inside_vault("hermes/rules.md")
        assert result is not None
        assert result.is_file()

    def test_resolve_inside_hermes_subdir_rejects_escape(self, vault):
        # Traversal segments that would escape hermes/ get refused
        assert ob._resolve_inside_hermes_subdir("../学习笔记/asyncio.md") is None
        assert ob._resolve_inside_hermes_subdir("../../etc/passwd") is None

    def test_resolve_inside_hermes_subdir_rejects_absolute(self, vault):
        assert ob._resolve_inside_hermes_subdir("/学习笔记/asyncio.md") is None

    def test_resolve_inside_hermes_subdir_accepts_hermes_path(self, vault):
        result = ob._resolve_inside_hermes_subdir("notes/free.md")
        assert result is not None
        assert result.parent == vault / "hermes" / "notes"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_default_scope_excludes_user_notes(self, vault):
        # Default scope = hermes_subdir → 学习笔记 invisible
        hits = ob.search("asyncio")
        assert hits == []

    def test_scope_all_finds_user_notes(self, vault):
        hits = ob.search("asyncio", scope=ob.SCOPE_ALL)
        assert any("asyncio" in h.preview.lower() or "asyncio" in h.relpath
                   for h in hits)

    def test_scope_hermes_subdir_finds_mirror(self, vault):
        hits = ob.search("Always write tests")
        assert len(hits) >= 1
        assert hits[0].relpath.startswith("hermes/")

    def test_scope_ingest_finds_ingest(self, vault):
        hits = ob.search("work_mem", scope=ob.SCOPE_INGEST)
        assert any("postgres" in h.relpath for h in hits)

    def test_empty_query_returns_empty(self, vault):
        assert ob.search("") == []

    def test_max_results_capped(self, vault):
        # The fixture has limited content; this just exercises the cap path
        hits = ob.search("hermes", max_results=1)
        assert len(hits) <= 1


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class TestView:
    def test_view_existing_file(self, vault):
        result = ob.view("hermes/rules.md")
        assert result["success"] is True
        assert "Always write tests" in result["content"]

    def test_view_rejects_path_outside_scope(self, vault):
        # Default scope only allows vault/hermes/, so 学习笔记/asyncio.md is rejected
        result = ob.view("学习笔记/asyncio.md")
        assert result["success"] is False
        assert "scope" in result["error"].lower()

    def test_view_with_scope_all_succeeds(self, vault):
        result = ob.view("学习笔记/asyncio.md", scope=ob.SCOPE_ALL)
        assert result["success"] is True

    def test_view_traversal_blocked(self, vault):
        result = ob.view("../escape.md")
        assert result["success"] is False

    def test_view_missing_file(self, vault):
        result = ob.view("hermes/nonexistent.md")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

class TestSave:
    def test_save_writes_inside_notes(self, vault):
        result = ob.save("session-A.md", "# Session A\nbody")
        assert result["success"] is True
        target = vault / "hermes" / "notes" / "session-A.md"
        assert target.is_file()
        assert "Session A" in target.read_text(encoding="utf-8")

    def test_save_refuses_path_outside_hermes(self, vault):
        # subdir defaults to "notes" → relpath "../../学习笔记/x.md" should
        # resolve outside vault/hermes/ and be refused
        result = ob.save("../../学习笔记/x.md", "evil")
        assert result["success"] is False

    def test_save_append_mode(self, vault):
        ob.save("log.md", "first\n", mode="write")
        ob.save("log.md", "second\n", mode="append")
        target = vault / "hermes" / "notes" / "log.md"
        assert target.read_text(encoding="utf-8") == "first\nsecond\n"

    def test_save_with_empty_subdir_writes_to_hermes_root(self, vault):
        # subdir="" lets export operations write to vault/hermes/<file>
        result = ob.save("user.md", "# User\n", subdir="")
        assert result["success"] is True
        assert (vault / "hermes" / "user.md").is_file()

    def test_save_rejects_oversized_content(self, vault):
        big = "x" * (ob.MAX_SAVE_CHARS + 1)
        result = ob.save("big.md", big)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_mirrors_memory_files(self, vault, hermes_home):
        mem_dir = hermes_home / "memories"
        mem_dir.mkdir(parents=True)
        (mem_dir / "RULES.md").write_text("Always run tests.", encoding="utf-8")
        (mem_dir / "MEMORY.md").write_text("Project uses pytest.", encoding="utf-8")
        (mem_dir / "USER.md").write_text("Prefers concise replies.", encoding="utf-8")

        result = ob.export_memory_files()
        assert result.error is None
        assert len(result.files_written) == 3

        # Each mirror has the managed marker
        rules_path = vault / "hermes" / "rules.md"
        assert rules_path.is_file()
        body = rules_path.read_text(encoding="utf-8")
        assert ob.HERMES_MANAGED_MARKER in body
        assert "Always run tests" in body

    def test_export_skips_missing_sources(self, vault, hermes_home):
        # No memories dir at all → all three skipped
        result = ob.export_memory_files()
        assert result.error is None
        assert result.files_written == []
        assert len(result.skipped) == 3


# ---------------------------------------------------------------------------
# Import — staging rules
# ---------------------------------------------------------------------------

class TestImportRulesFromStaging:
    def test_import_bullets_into_store(self, vault, hermes_home):
        staging = vault / "hermes" / "rules-staging.md"
        staging.write_text(
            "<!-- staged rules -->\n"
            "- 写代码注释一律用英文\n"
            "- PR 必须有 testing section\n"
            "\n"
            "## ignored heading\n",
            encoding="utf-8",
        )

        # Use a fresh MemoryStore against the isolated HERMES_HOME
        from tools.memory_tool import MemoryStore
        store = MemoryStore()
        store.load_from_disk()

        result = ob.import_rules_from_staging(store=store)
        assert result.error is None
        assert len(result.rules_added) == 2
        assert any("英文" in r for r in result.rules_added)

        # Staging file should be reset to a fresh placeholder when nothing was skipped
        assert "Add new rules below" in staging.read_text(encoding="utf-8")

        # The rules are in the store
        store.load_from_disk()
        assert any("英文" in r for r in store.rules_entries)

    def test_no_staging_file_returns_empty(self, vault, hermes_home):
        # No staging file
        result = ob.import_rules_from_staging()
        assert result.rules_added == []
        assert result.error is None


# ---------------------------------------------------------------------------
# Import — vault → project knowledge
# ---------------------------------------------------------------------------

class TestImportNotesToPK:
    def test_copies_markdown_to_pk_dir(self, vault, hermes_home):
        # Drop two notes in ingest/
        ingest = vault / "hermes" / "ingest"
        (ingest / "a.md").write_text("# A", encoding="utf-8")
        sub = ingest / "sub"
        sub.mkdir()
        (sub / "b.md").write_text("# B", encoding="utf-8")
        # And a binary that should be skipped
        (ingest / "data.bin").write_bytes(b"\x00\x01")

        result = ob.import_notes_to_pk("demo")
        assert result.error is None
        assert "a.md" in result.notes_imported
        assert "sub/b.md" in result.notes_imported
        assert "data.bin" not in result.notes_imported

        from agent.project_knowledge import get_project_dir
        pk = get_project_dir("demo")
        assert (pk / "a.md").is_file()
        assert (pk / "sub" / "b.md").is_file()
        assert not (pk / "data.bin").exists()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        info = ob.status()
        assert info["enabled"] is False

    def test_status_reports_counts(self, vault):
        info = ob.status()
        assert info["enabled"] is True
        assert info["vault_exists"] is True
        assert info["ingest_files"] >= 1
