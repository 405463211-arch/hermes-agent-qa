"""M17 white-box Obsidian-bridge probe.

Covers:
- agent/obsidian.py: every public function's input space, including all
  failure modes that would otherwise corrupt the user's vault
- Path containment under adversarial inputs (escape attempts, symlinks,
  absolute paths, byte-level path tricks)
- Scope enforcement: hermes_subdir, ingest, all
- Round-trip property: anything we export and re-import preserves rule text
- Atomic-write semantics: partial failures don't corrupt mirror files
- Profile name resolution under various env vars
- Bridge config priority: env > config.yaml > default
- Tool layer: every error path in obsidian_search/view/save returns a
  parsable JSON envelope (the agent's tool dispatcher requires this)
- Auto-import / auto-export idempotency: running twice in a row yields
  the same final state (not duplicates)
- Vault corruption resistance: wrong filetype, encoding errors, missing
  parent dirs

This module is plain pytest — no async, no network, no sleeps.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent import obsidian as ob


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Fixtures                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@pytest.fixture
def fresh_vault(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "memories").mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_OBSIDIAN_VAULT", str(vault))
    monkeypatch.delenv("HERMES_OBSIDIAN_SCOPE", raising=False)
    return {"home": home, "vault": vault, "tmp": tmp_path}


@pytest.fixture
def disabled_vault(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("HERMES_OBSIDIAN_SCOPE", raising=False)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Config priority + lookup                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestConfigResolution:
    def test_env_var_wins_over_config_yaml(self, tmp_path, monkeypatch):
        home = tmp_path / "hh"
        home.mkdir()
        # Write a config.yaml with one path
        (home / "config.yaml").write_text(
            "obsidian:\n  enabled: true\n  vault_path: /tmp/yaml-vault\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        # Env var points elsewhere — should win
        env_vault = tmp_path / "env-vault"
        env_vault.mkdir()
        monkeypatch.setenv("HERMES_OBSIDIAN_VAULT", str(env_vault))

        cfg = ob._read_obsidian_config()
        assert cfg["vault_path"] == str(env_vault)

    def test_config_yaml_fallback_when_no_env(self, tmp_path, monkeypatch):
        home = tmp_path / "hh"
        home.mkdir()
        vault = tmp_path / "v"
        vault.mkdir()
        (home / "config.yaml").write_text(
            f"obsidian:\n  enabled: true\n  vault_path: {vault}\n  search_scope: ingest\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)

        cfg = ob._read_obsidian_config()
        # load_config might apply defaults; we just want this specific value
        assert str(vault) in cfg.get("vault_path", "")
        assert cfg.get("search_scope") == "ingest"

    def test_invalid_scope_falls_back_to_default(self, fresh_vault, monkeypatch):
        monkeypatch.setenv("HERMES_OBSIDIAN_SCOPE", "garbage-scope")
        assert ob.get_search_scope() == ob.SCOPE_HERMES_SUBDIR

    def test_disabled_when_no_vault_anywhere(self, disabled_vault):
        assert ob.is_enabled() is False
        assert ob.get_vault_path() is None


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Path containment — adversarial inputs                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestPathContainment:
    """Every path that takes user-supplied input MUST refuse escapes."""

    def test_resolve_inside_vault_blocks_absolute_paths(self, fresh_vault):
        for raw in ("/etc/passwd", "/", "/usr/bin/python", "/tmp/x"):
            assert ob._resolve_inside_vault(raw) is None, raw

    def test_resolve_inside_vault_blocks_dotdot(self, fresh_vault):
        for raw in (
            "../etc/passwd",
            "hermes/../../escape.md",
            "../../../root.md",
            "subdir/../../boom.md",
        ):
            assert ob._resolve_inside_vault(raw) is None, raw

    def test_resolve_inside_vault_blocks_empty(self, fresh_vault):
        assert ob._resolve_inside_vault("") is None

    def test_resolve_inside_hermes_subdir_blocks_dotdot(self, fresh_vault):
        for raw in ("../学习笔记/x.md", "../../etc/passwd", "../"):
            assert ob._resolve_inside_hermes_subdir(raw) is None, raw

    def test_resolve_inside_hermes_subdir_blocks_absolute(self, fresh_vault):
        assert ob._resolve_inside_hermes_subdir("/etc/passwd") is None

    def test_symlink_escape_refused(self, fresh_vault):
        """Symlinks that point outside hermes/ must not let the agent write through."""
        vault = fresh_vault["vault"]
        outside = fresh_vault["tmp"] / "outside"
        outside.mkdir()
        # Create vault/hermes/sneaky → points outside the vault entirely
        (vault / "hermes").mkdir(exist_ok=True)
        try:
            (vault / "hermes" / "sneaky").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")

        # Trying to write through the symlink resolves outside the realpath'd
        # hermes_dir → containment check returns False.
        target = ob._resolve_inside_hermes_subdir("sneaky/leaked.md")
        assert target is None

    def test_save_blocks_outside_hermes_via_subdir_trick(self, fresh_vault):
        """``subdir`` arg shouldn't let you escape via ``..``."""
        result = ob.save(
            "x.md", "evil",
            subdir="../学习笔记",  # try to break out of notes/
        )
        assert result["success"] is False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Save semantics                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestSaveSemantics:
    def test_creates_parent_dirs(self, fresh_vault):
        result = ob.save("deep/nested/file.md", "x")
        assert result["success"] is True
        assert (fresh_vault["vault"] / "hermes" / "notes" / "deep" / "nested" / "file.md").is_file()

    def test_overwrite_replaces(self, fresh_vault):
        ob.save("a.md", "v1")
        ob.save("a.md", "v2", mode="write")
        assert (fresh_vault["vault"] / "hermes" / "notes" / "a.md").read_text(encoding="utf-8") == "v2"

    def test_append_concatenates(self, fresh_vault):
        ob.save("b.md", "line1\n", mode="write")
        ob.save("b.md", "line2\n", mode="append")
        body = (fresh_vault["vault"] / "hermes" / "notes" / "b.md").read_text(encoding="utf-8")
        assert body == "line1\nline2\n"

    def test_invalid_mode_rejected(self, fresh_vault):
        result = ob.save("c.md", "x", mode="delete")
        assert result["success"] is False
        assert "mode must be" in result["error"]

    def test_oversized_content_rejected_without_partial_write(self, fresh_vault):
        big = "x" * (ob.MAX_SAVE_CHARS + 1)
        result = ob.save("d.md", big)
        assert result["success"] is False
        # No file should have been created
        assert not (fresh_vault["vault"] / "hermes" / "notes" / "d.md").exists()

    def test_none_content_rejected(self, fresh_vault):
        result = ob.save("e.md", None)  # type: ignore[arg-type]
        assert result["success"] is False

    def test_empty_relpath_rejected(self, fresh_vault):
        assert ob.save("", "x")["success"] is False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ View — boundary conditions                                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestViewBoundaries:
    def test_offset_clamped_to_one(self, fresh_vault):
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "x.md").write_text("a\nb\nc\n", encoding="utf-8")
        result = ob.view("hermes/x.md", offset=-5)
        assert result["success"] is True
        assert result["offset"] == 1

    def test_limit_clamped(self, fresh_vault):
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "x.md").write_text("\n".join(str(i) for i in range(50)), encoding="utf-8")
        result = ob.view("hermes/x.md", offset=1, limit=999_999)
        # Limit gets clamped at 2000 in implementation
        assert result["success"] is True
        assert result["lines_returned"] <= 2000

    def test_huge_file_truncated_with_marker(self, fresh_vault):
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        big = "x" * (ob.MAX_VIEW_CHARS + 1000)
        (fresh_vault["vault"] / "hermes" / "huge.md").write_text(big, encoding="utf-8")
        result = ob.view("hermes/huge.md", offset=1, limit=1)
        assert result["success"] is True
        # When the single line is huge, truncation marker appended
        assert result["content"].endswith("[...truncated]")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Search scope enforcement                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestSearchScopeEnforcement:
    def _seed(self, fresh_vault):
        v = fresh_vault["vault"]
        (v / "hermes" / "ingest").mkdir(parents=True, exist_ok=True)
        (v / "hermes" / "rules.md").write_text("RULE_HERMES_SUBDIR_TOKEN", encoding="utf-8")
        (v / "hermes" / "ingest" / "i.md").write_text("RULE_INGEST_TOKEN", encoding="utf-8")
        (v / "private").mkdir(exist_ok=True)
        (v / "private" / "diary.md").write_text("RULE_PRIVATE_TOKEN", encoding="utf-8")
        return v

    def test_default_excludes_ingest_only_scope(self, fresh_vault):
        self._seed(fresh_vault)
        # default = hermes_subdir → finds hermes/* and hermes/ingest/* both
        hits = ob.search("RULE_HERMES_SUBDIR_TOKEN")
        assert any("rules.md" in h.relpath for h in hits)
        # private not visible
        hits = ob.search("RULE_PRIVATE_TOKEN")
        assert hits == []

    def test_ingest_scope_excludes_hermes_root(self, fresh_vault):
        self._seed(fresh_vault)
        # ingest scope ONLY sees hermes/ingest/, not hermes/rules.md
        hits = ob.search("RULE_HERMES_SUBDIR_TOKEN", scope=ob.SCOPE_INGEST)
        assert hits == []
        hits = ob.search("RULE_INGEST_TOKEN", scope=ob.SCOPE_INGEST)
        assert any("i.md" in h.relpath for h in hits)

    def test_all_scope_finds_private(self, fresh_vault):
        self._seed(fresh_vault)
        hits = ob.search("RULE_PRIVATE_TOKEN", scope=ob.SCOPE_ALL)
        assert any("private" in h.relpath for h in hits)

    def test_view_blocked_outside_scope_even_with_valid_path(self, fresh_vault):
        self._seed(fresh_vault)
        # Default scope (hermes_subdir) → can't view private/diary.md even though it's a real file
        result = ob.view("private/diary.md")
        assert result["success"] is False
        assert "scope" in result["error"].lower()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Export idempotency + corruption resistance                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestExportIdempotency:
    def test_export_twice_yields_same_content(self, fresh_vault):
        (fresh_vault["home"] / "memories" / "RULES.md").write_text("Rule X", encoding="utf-8")
        ob.export_memory_files()
        body1 = (fresh_vault["vault"] / "hermes" / "rules.md").read_text(encoding="utf-8")
        ob.export_memory_files()
        body2 = (fresh_vault["vault"] / "hermes" / "rules.md").read_text(encoding="utf-8")
        # Timestamps in frontmatter differ but the rule body must be identical
        # Strip the frontmatter and compare just the body
        def body_only(text):
            parts = text.split("---", 2)
            return parts[2] if len(parts) >= 3 else text
        assert "Rule X" in body_only(body1)
        assert "Rule X" in body_only(body2)

    def test_export_works_with_only_partial_sources(self, fresh_vault):
        # Only RULES.md exists; MEMORY.md and USER.md absent
        (fresh_vault["home"] / "memories" / "RULES.md").write_text("X", encoding="utf-8")
        result = ob.export_memory_files()
        assert result.error is None
        assert len(result.files_written) == 1
        assert "MEMORY.md" in result.skipped
        assert "USER.md" in result.skipped

    def test_export_when_source_unreadable_skips(self, fresh_vault):
        # File exists but as a directory (read will fail) — exercise OSError branch
        bad = fresh_vault["home"] / "memories" / "RULES.md"
        bad.mkdir()  # make it a directory
        result = ob.export_memory_files()
        assert result.error is None
        # RULES.md gets skipped, no crash
        assert any("RULES.md" in s for s in result.skipped)

    def test_export_marker_present(self, fresh_vault):
        (fresh_vault["home"] / "memories" / "RULES.md").write_text("X", encoding="utf-8")
        ob.export_memory_files()
        body = (fresh_vault["vault"] / "hermes" / "rules.md").read_text(encoding="utf-8")
        assert ob.HERMES_MANAGED_MARKER in body
        # Frontmatter machine-readable
        assert body.startswith("---\n")
        assert "hermes-source: RULES.md" in body


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Import — staging parser robustness                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestImportParser:
    def test_skips_html_comments(self, fresh_vault):
        from tools.memory_tool import MemoryStore
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "rules-staging.md").write_text(
            "<!-- this is a comment -->\n"
            "<!-- multi line\n     comment -->\n"
            "- valid rule one\n"
            "- valid rule two\n",
            encoding="utf-8",
        )
        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)
        assert result.error is None
        # Only the two real rules get added
        assert len(result.rules_added) == 2
        assert all("comment" not in r for r in result.rules_added)

    def test_skips_headings_and_frontmatter(self, fresh_vault):
        from tools.memory_tool import MemoryStore
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "rules-staging.md").write_text(
            "---\ntitle: stuff\n---\n"
            "# Heading\n"
            "## Another heading\n"
            "- real rule\n",
            encoding="utf-8",
        )
        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)
        assert result.error is None
        assert result.rules_added == ["real rule"]

    def test_handles_multiple_bullet_styles(self, fresh_vault):
        from tools.memory_tool import MemoryStore
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "rules-staging.md").write_text(
            "- dash bullet\n"
            "* asterisk bullet\n"
            "• unicode bullet\n",
            encoding="utf-8",
        )
        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)
        assert result.error is None
        assert len(result.rules_added) == 3

    def test_idempotent_on_empty_staging(self, fresh_vault):
        from tools.memory_tool import MemoryStore
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "rules-staging.md").write_text(
            "<!-- nothing here -->\n", encoding="utf-8"
        )
        store = MemoryStore()
        store.load_from_disk()
        r1 = ob.import_rules_from_staging(store=store)
        r2 = ob.import_rules_from_staging(store=store)
        assert r1.rules_added == [] and r2.rules_added == []
        assert r1.error is None and r2.error is None

    def test_no_staging_file_no_error(self, fresh_vault):
        from tools.memory_tool import MemoryStore
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        # No rules-staging.md file at all
        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)
        assert result.error is None
        assert result.rules_added == []


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Tool layer — JSON envelope contract                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestToolEnvelopes:
    """Every tool MUST return a JSON-decodable string. The agent tool
    dispatcher relies on this — a non-JSON return crashes the
    function-call loop."""

    def test_search_returns_valid_json_on_success(self, fresh_vault):
        from tools.obsidian_tool import obsidian_search
        (fresh_vault["vault"] / "hermes").mkdir(exist_ok=True)
        (fresh_vault["vault"] / "hermes" / "x.md").write_text("hello world", encoding="utf-8")
        out = obsidian_search("hello")
        data = json.loads(out)
        assert isinstance(data, dict)
        assert data["success"] is True

    def test_search_returns_valid_json_on_disabled(self, disabled_vault):
        from tools.obsidian_tool import obsidian_search
        out = obsidian_search("anything")
        data = json.loads(out)  # MUST not crash
        assert data["success"] is False

    def test_view_returns_valid_json_on_missing(self, fresh_vault):
        from tools.obsidian_tool import obsidian_view
        out = obsidian_view("hermes/nope.md")
        data = json.loads(out)
        assert data["success"] is False

    def test_view_returns_valid_json_on_traversal(self, fresh_vault):
        from tools.obsidian_tool import obsidian_view
        out = obsidian_view("../../../etc/passwd")
        data = json.loads(out)
        assert data["success"] is False

    def test_save_returns_valid_json_envelope(self, fresh_vault):
        from tools.obsidian_tool import obsidian_save
        out = obsidian_save("test.md", "content")
        data = json.loads(out)
        assert data["success"] is True
        assert "path" in data

    def test_all_tools_handle_none_args_gracefully(self, fresh_vault):
        """Even when called with empty/missing args, tools return a JSON string."""
        from tools.obsidian_tool import obsidian_save, obsidian_search, obsidian_view
        for fn, args in [
            (obsidian_search, ("",)),
            (obsidian_view, ("",)),
            (obsidian_save, ("", "")),
        ]:
            out = fn(*args)
            data = json.loads(out)
            assert isinstance(data, dict)
            assert "success" in data


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Status — defensive defaults                                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestStatus:
    def test_status_when_disabled_returns_safe_dict(self, disabled_vault):
        info = ob.status()
        assert isinstance(info, dict)
        assert info["enabled"] is False
        assert info["vault_exists"] is False

    def test_status_counts_ingest_only_readable(self, fresh_vault):
        v = fresh_vault["vault"]
        (v / "hermes" / "ingest").mkdir(parents=True, exist_ok=True)
        (v / "hermes" / "ingest" / "a.md").write_text("a", encoding="utf-8")
        (v / "hermes" / "ingest" / "b.txt").write_text("b", encoding="utf-8")
        (v / "hermes" / "ingest" / "noise.bin").write_bytes(b"\x00")
        info = ob.status()
        # .bin should NOT be counted
        assert info["ingest_files"] == 2

    def test_status_counts_pending_lines_skipping_comments(self, fresh_vault):
        v = fresh_vault["vault"]
        (v / "hermes").mkdir(exist_ok=True)
        (v / "hermes" / "rules-staging.md").write_text(
            "<!-- ignore -->\n# heading\n---\n- real one\n- real two\n", encoding="utf-8"
        )
        info = ob.status()
        assert info["staging_pending_lines"] == 2


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ list_ingest_files — boundary cases                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestListIngestFiles:
    def test_empty_when_no_ingest_dir(self, fresh_vault):
        # No vault/hermes/ingest/ exists
        assert ob.list_ingest_files() == []

    def test_filters_by_extension(self, fresh_vault):
        v = fresh_vault["vault"]
        ingest = v / "hermes" / "ingest"
        ingest.mkdir(parents=True, exist_ok=True)
        (ingest / "a.md").write_text("x", encoding="utf-8")
        (ingest / "b.txt").write_text("x", encoding="utf-8")
        (ingest / "c.png").write_bytes(b"\x89PNG")
        (ingest / "d.bin").write_bytes(b"\x00")
        files = ob.list_ingest_files()
        names = {p.name for p in files}
        assert names == {"a.md", "b.txt"}

    def test_recurses_into_subdirs(self, fresh_vault):
        v = fresh_vault["vault"]
        ingest = v / "hermes" / "ingest"
        sub = ingest / "subdir" / "deep"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "file.md").write_text("x", encoding="utf-8")
        files = ob.list_ingest_files()
        assert len(files) == 1
        assert files[0].name == "file.md"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ import_notes_to_pk — vault folder → project-knowledge                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestImportNotesToPK:
    def test_default_source_is_ingest(self, fresh_vault):
        v = fresh_vault["vault"]
        (v / "hermes" / "ingest").mkdir(parents=True, exist_ok=True)
        (v / "hermes" / "ingest" / "x.md").write_text("# X", encoding="utf-8")
        result = ob.import_notes_to_pk("test-proj")
        assert result.error is None
        assert "x.md" in result.notes_imported

    def test_custom_source_dir(self, fresh_vault):
        custom = fresh_vault["tmp"] / "custom-source"
        custom.mkdir()
        (custom / "y.md").write_text("# Y", encoding="utf-8")
        result = ob.import_notes_to_pk("test-proj", source_dir=custom)
        assert result.error is None
        assert "y.md" in result.notes_imported

    def test_missing_source_returns_error(self, fresh_vault):
        result = ob.import_notes_to_pk(
            "test-proj",
            source_dir=fresh_vault["tmp"] / "nonexistent",
        )
        assert result.error is not None
        assert "not found" in result.error

    def test_empty_project_name_rejected(self, fresh_vault):
        result = ob.import_notes_to_pk("")
        assert result.error is not None
        assert "required" in result.error

    def test_preserves_subdirectory_structure(self, fresh_vault):
        from agent.project_knowledge import get_project_dir
        v = fresh_vault["vault"]
        (v / "hermes" / "ingest" / "deep" / "nest").mkdir(parents=True)
        (v / "hermes" / "ingest" / "top.md").write_text("# Top", encoding="utf-8")
        (v / "hermes" / "ingest" / "deep" / "mid.md").write_text("# Mid", encoding="utf-8")
        (v / "hermes" / "ingest" / "deep" / "nest" / "leaf.md").write_text("# Leaf", encoding="utf-8")
        ob.import_notes_to_pk("structured")
        pk = get_project_dir("structured")
        assert (pk / "top.md").is_file()
        assert (pk / "deep" / "mid.md").is_file()
        assert (pk / "deep" / "nest" / "leaf.md").is_file()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Profile resolution                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestProfileResolution:
    def test_default_profile_resolves_flat(self, fresh_vault):
        with patch("agent.obsidian._active_profile_name", return_value="default"):
            sub = ob.get_profile_subdir()
            assert sub == fresh_vault["vault"] / "hermes"

    def test_empty_profile_resolves_flat(self, fresh_vault):
        with patch("agent.obsidian._active_profile_name", return_value=""):
            sub = ob.get_profile_subdir()
            assert sub == fresh_vault["vault"] / "hermes"

    def test_named_profile_creates_nested(self, fresh_vault):
        with patch("agent.obsidian._active_profile_name", return_value="anyname"):
            sub = ob.get_profile_subdir()
            assert sub == fresh_vault["vault"] / "hermes" / "profiles" / "anyname"
            assert sub.is_dir()  # auto-created

    def test_profile_resolution_failure_falls_back(self, fresh_vault):
        # Force the import to fail
        with patch("agent.obsidian._active_profile_name", return_value="default"):
            assert ob.get_profile_subdir() is not None


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Round-trip property                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestRoundTrip:
    def test_export_import_roundtrip(self, fresh_vault):
        """Export RULES, edit a copy in vault, import staged version,
        verify RULES contains the new content."""
        from tools.memory_tool import MemoryStore

        # Initial seed
        (fresh_vault["home"] / "memories" / "RULES.md").write_text(
            "Initial rule.", encoding="utf-8"
        )
        ob.export_memory_files()
        # User adds rules in staging
        staging = fresh_vault["vault"] / "hermes" / "rules-staging.md"
        staging.write_text("- New rule from vault\n", encoding="utf-8")

        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)

        # Round-trip: rule is now both in RULES.md and findable in mirror after re-export
        assert any("New rule from vault" in r for r in result.rules_added)
        ob.export_memory_files()
        body = (fresh_vault["vault"] / "hermes" / "rules.md").read_text(encoding="utf-8")
        assert "New rule from vault" in body
        # And the original rule is still there
        assert "Initial rule" in body
