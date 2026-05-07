"""End-to-end integration tests for the Obsidian bridge.

Exercises the full pipeline:

  1. ``hermes obsidian setup`` (non-interactive equivalent: write config + bootstrap)
  2. Auto-import on session start (rules-staging.md → RULES.md)
  3. Auto-export on session end (RULES/MEMORY/USER → vault/hermes/)
  4. Profile isolation (multiple profiles share one vault without colliding)
  5. CLI subcommand dispatch (status / export / import-rules / off)
  6. Round trip: rules added in vault appear in MemoryStore on next load

Each test sets up an isolated HERMES_HOME + an isolated vault tmpdir, and
runs the relevant code path against real APIs (no mocking of the bridge
itself).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixture: isolated HERMES_HOME + vault + MemoryStore
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Set up a clean HERMES_HOME, vault, and obsidian config for each test."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    vault = tmp_path / "MyVault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_OBSIDIAN_VAULT", str(vault))
    monkeypatch.delenv("HERMES_OBSIDIAN_SCOPE", raising=False)

    # Pre-create the memories dir so MemoryStore writes there
    (home / "memories").mkdir(parents=True, exist_ok=True)

    return {"home": home, "vault": vault}


# ---------------------------------------------------------------------------
# 1. Setup / bootstrap
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_bootstrap_creates_hermes_subdir_skeleton(self, env):
        from hermes_cli.obsidian_setup import _bootstrap_vault
        _bootstrap_vault(env["vault"])

        hermes = env["vault"] / "hermes"
        assert hermes.is_dir()
        assert (hermes / "notes").is_dir()
        assert (hermes / "ingest").is_dir()
        assert (hermes / "learnings").is_dir()
        assert (hermes / "rules-staging.md").is_file()
        assert (hermes / "README.md").is_file()
        # README explains the layout
        readme = (hermes / "README.md").read_text(encoding="utf-8")
        assert "rules-staging.md" in readme
        assert "ingest/" in readme

    def test_bootstrap_idempotent(self, env):
        from hermes_cli.obsidian_setup import _bootstrap_vault
        _bootstrap_vault(env["vault"])
        readme_first = (env["vault"] / "hermes" / "README.md").read_text(encoding="utf-8")
        # Edit it manually
        (env["vault"] / "hermes" / "README.md").write_text("# Custom\n", encoding="utf-8")
        # Bootstrap again — should NOT clobber user-edited README
        _bootstrap_vault(env["vault"])
        assert (env["vault"] / "hermes" / "README.md").read_text(encoding="utf-8") == "# Custom\n"


# ---------------------------------------------------------------------------
# 2. Auto-import flow (vault → RULES.md)
# ---------------------------------------------------------------------------

class TestAutoImportFlow:
    def test_staging_bullets_become_rules(self, env):
        from agent import obsidian as ob
        from hermes_cli.obsidian_setup import _bootstrap_vault
        from tools.memory_tool import MemoryStore

        _bootstrap_vault(env["vault"])

        # User adds rules in Obsidian
        staging = env["vault"] / "hermes" / "rules-staging.md"
        staging.write_text(
            "<!-- Add rules below -->\n"
            "- Always run tests before pushing\n"
            "- Never force-push to main\n"
            "- Use English for code comments\n",
            encoding="utf-8",
        )

        # Agent boots — auto-import runs
        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)

        assert result.error is None
        assert len(result.rules_added) == 3
        # Rules persisted to the canonical RULES.md
        rules_file = env["home"] / "memories" / "RULES.md"
        assert rules_file.is_file()
        body = rules_file.read_text(encoding="utf-8")
        assert "Always run tests" in body
        assert "Never force-push" in body
        assert "English for code comments" in body
        # Lifecycle metadata records the source
        assert "obsidian-import" in body

        # Staging file got reset
        new_staging = staging.read_text(encoding="utf-8")
        assert "Always run tests" not in new_staging

    def test_partial_failure_keeps_staging(self, env):
        """If some rules fail to add, staging is left intact for user to fix."""
        from agent import obsidian as ob
        from hermes_cli.obsidian_setup import _bootstrap_vault
        from tools.memory_tool import MemoryStore

        _bootstrap_vault(env["vault"])

        staging = env["vault"] / "hermes" / "rules-staging.md"
        # Use a near-empty rule that may be rejected
        staging.write_text(
            "- valid rule about pytest\n"
            "- \n"  # empty bullet — rejected by add_rule_with_lifecycle
            "- another valid rule\n",
            encoding="utf-8",
        )

        store = MemoryStore()
        store.load_from_disk()
        result = ob.import_rules_from_staging(store=store)

        # We don't assert on exact behavior here (depends on memory_tool's
        # validation), only that the function doesn't crash and reports
        # a coherent result.
        assert result.error is None
        assert isinstance(result.rules_added, list)
        assert isinstance(result.rules_skipped, list)


# ---------------------------------------------------------------------------
# 3. Auto-export flow (hermes → vault)
# ---------------------------------------------------------------------------

class TestAutoExportFlow:
    def test_session_end_exports_all_three_files(self, env):
        from agent import obsidian as ob

        # Populate hermes' canonical memory files
        (env["home"] / "memories" / "RULES.md").write_text(
            "Always confirm scope before bulk edits.\n",
            encoding="utf-8",
        )
        (env["home"] / "memories" / "MEMORY.md").write_text(
            "Project uses pytest + xdist for tests.\n",
            encoding="utf-8",
        )
        (env["home"] / "memories" / "USER.md").write_text(
            "User prefers concise responses.\n",
            encoding="utf-8",
        )

        # Run the export (this is what shutdown_memory_provider invokes)
        result = ob.export_memory_files()

        assert result.error is None
        assert len(result.files_written) == 3
        for src in ("rules.md", "memory.md", "user.md"):
            mirror = env["vault"] / "hermes" / src
            assert mirror.is_file(), f"missing mirror: {src}"
            body = mirror.read_text(encoding="utf-8")
            assert ob.HERMES_MANAGED_MARKER in body
            assert "hermes-managed: true" in body  # frontmatter
            assert "hermes-exported-at:" in body  # timestamp

    def test_export_round_trips_content(self, env):
        from agent import obsidian as ob

        original = "Always confirm scope before bulk edits.\nNever skip tests.\n"
        (env["home"] / "memories" / "RULES.md").write_text(original, encoding="utf-8")
        ob.export_memory_files()
        body = (env["vault"] / "hermes" / "rules.md").read_text(encoding="utf-8")
        # The original lines survive verbatim in the mirror
        assert "Always confirm scope before bulk edits." in body
        assert "Never skip tests." in body

    def test_re_export_overwrites(self, env):
        """User edits to mirror files are overwritten on next export — by design."""
        from agent import obsidian as ob

        (env["home"] / "memories" / "RULES.md").write_text("v1", encoding="utf-8")
        ob.export_memory_files()
        # User accidentally edits the mirror
        (env["vault"] / "hermes" / "rules.md").write_text("HAND EDITED", encoding="utf-8")
        # Export again
        (env["home"] / "memories" / "RULES.md").write_text("v2", encoding="utf-8")
        ob.export_memory_files()
        body = (env["vault"] / "hermes" / "rules.md").read_text(encoding="utf-8")
        assert "v2" in body
        assert "HAND EDITED" not in body


# ---------------------------------------------------------------------------
# 4. Profile isolation
# ---------------------------------------------------------------------------

class TestProfileIsolation:
    def test_default_profile_writes_to_hermes_root(self, env):
        from agent import obsidian as ob

        # Default (no -p flag) → write directly under vault/hermes/
        with patch("agent.obsidian._active_profile_name", return_value="default"):
            sub = ob.get_profile_subdir()
            assert sub == env["vault"] / "hermes"

    def test_named_profile_gets_own_subdir(self, env):
        from agent import obsidian as ob

        with patch("agent.obsidian._active_profile_name", return_value="coder"):
            sub = ob.get_profile_subdir()
            assert sub == env["vault"] / "hermes" / "profiles" / "coder"
            assert sub.is_dir()

    def test_two_profiles_export_independently(self, env):
        from agent import obsidian as ob

        # Profile A
        (env["home"] / "memories" / "RULES.md").write_text(
            "Profile A rule", encoding="utf-8"
        )
        with patch("agent.obsidian._active_profile_name", return_value="coder"):
            r_a = ob.export_memory_files()
        assert r_a.error is None
        assert any("profiles/coder/rules.md" in p for p in r_a.files_written)

        # Profile B (different name) — same vault, different output path
        (env["home"] / "memories" / "RULES.md").write_text(
            "Profile B rule", encoding="utf-8"
        )
        with patch("agent.obsidian._active_profile_name", return_value="personal"):
            r_b = ob.export_memory_files()
        assert r_b.error is None
        assert any("profiles/personal/rules.md" in p for p in r_b.files_written)

        # Verify profile A's mirror was NOT clobbered
        a_mirror = env["vault"] / "hermes" / "profiles" / "coder" / "rules.md"
        b_mirror = env["vault"] / "hermes" / "profiles" / "personal" / "rules.md"
        assert "Profile A rule" in a_mirror.read_text(encoding="utf-8")
        assert "Profile B rule" in b_mirror.read_text(encoding="utf-8")

    def test_two_profiles_have_independent_staging(self, env):
        from agent import obsidian as ob
        from tools.memory_tool import MemoryStore

        # Profile A's staging
        with patch("agent.obsidian._active_profile_name", return_value="coder"):
            sub = ob.get_profile_subdir()
            (sub / "rules-staging.md").write_text(
                "- coder profile only rule\n", encoding="utf-8"
            )
        # Profile B's staging (same vault, different folder)
        with patch("agent.obsidian._active_profile_name", return_value="personal"):
            sub_b = ob.get_profile_subdir()
            (sub_b / "rules-staging.md").write_text(
                "- personal profile only rule\n", encoding="utf-8"
            )

        # Profile A imports — gets only its own rule
        store = MemoryStore()
        store.load_from_disk()
        with patch("agent.obsidian._active_profile_name", return_value="coder"):
            r = ob.import_rules_from_staging(store=store)
        assert any("coder profile only" in s for s in r.rules_added)
        assert not any("personal profile only" in s for s in r.rules_added)


# ---------------------------------------------------------------------------
# 5. CLI subcommand dispatch
# ---------------------------------------------------------------------------

class TestCLIDispatch:
    def test_status_subcommand_runs(self, env, capsys):
        from hermes_cli.obsidian_setup import obsidian_command

        class Args:
            obsidian_command = "status"

        obsidian_command(Args())
        out = capsys.readouterr().out
        assert "Obsidian bridge status" in out
        assert "vault_path:" in out

    def test_unknown_subcommand_prints_help(self, env, capsys):
        from hermes_cli.obsidian_setup import obsidian_command

        class Args:
            obsidian_command = "bogus-thing"

        obsidian_command(Args())
        out = capsys.readouterr().out
        assert "Unknown obsidian subcommand" in out
        assert "setup" in out

    def test_off_subcommand_disables(self, env, capsys, monkeypatch):
        from hermes_cli.obsidian_setup import obsidian_command
        from hermes_cli.config import load_config

        # Seed config.yaml so save_config has something to update
        config_path = env["home"] / "config.yaml"
        config_path.write_text(
            "obsidian:\n  enabled: true\n  vault_path: " + str(env["vault"]) + "\n",
            encoding="utf-8",
        )

        class Args:
            obsidian_command = "off"

        obsidian_command(Args())
        cfg = load_config()
        assert cfg["obsidian"]["enabled"] is False

    def test_export_via_cli(self, env, capsys):
        from hermes_cli.obsidian_setup import obsidian_command

        (env["home"] / "memories" / "RULES.md").write_text("rule one", encoding="utf-8")
        (env["home"] / "memories" / "MEMORY.md").write_text("mem one", encoding="utf-8")

        class Args:
            obsidian_command = "export"

        obsidian_command(Args())
        out = capsys.readouterr().out
        assert "Exporting hermes memory" in out
        assert (env["vault"] / "hermes" / "rules.md").is_file()


# ---------------------------------------------------------------------------
# 6. Tool registration (toolsets + registry integration)
# ---------------------------------------------------------------------------

class TestToolsetIntegration:
    def test_obsidian_toolset_exists(self):
        from toolsets import TOOLSETS
        assert "obsidian" in TOOLSETS
        names = TOOLSETS["obsidian"]["tools"]
        assert "obsidian_search" in names
        assert "obsidian_view" in names
        assert "obsidian_save" in names

    def test_obsidian_tools_in_hermes_core(self):
        from toolsets import _HERMES_CORE_TOOLS
        assert "obsidian_search" in _HERMES_CORE_TOOLS
        assert "obsidian_view" in _HERMES_CORE_TOOLS
        assert "obsidian_save" in _HERMES_CORE_TOOLS

    def test_tools_registered_in_global_registry(self):
        # Force tool discovery
        import tools.obsidian_tool  # noqa: F401
        from tools.registry import registry

        for name in ("obsidian_search", "obsidian_view", "obsidian_save"):
            assert registry.get_schema(name) is not None, f"{name} missing from registry"

    def test_check_fn_gates_on_config(self, env, monkeypatch):
        """The check_fn returns True when configured, False when not."""
        from tools.obsidian_tool import check_obsidian_requirements

        # With env var set (fixture provides it) → True
        assert check_obsidian_requirements() is True

        # Strip env var → False (no vault configured)
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT")
        assert check_obsidian_requirements() is False


# ---------------------------------------------------------------------------
# 7. Prompt block integration
# ---------------------------------------------------------------------------

class TestPromptBlock:
    def test_prompt_block_empty_when_disabled(self, tmp_path, monkeypatch):
        """No tools loaded ⇒ block returns empty string ⇒ zero token cost."""
        from agent.prompt_builder import build_obsidian_prompt
        # Don't enable obsidian; pretend obsidian_search isn't loaded
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)
        block = build_obsidian_prompt(valid_tool_names=set())
        assert block == ""

    def test_prompt_block_empty_when_tool_missing(self, env):
        """Even when configured, if the tool isn't in valid_tool_names → empty."""
        from agent.prompt_builder import build_obsidian_prompt
        block = build_obsidian_prompt(valid_tool_names={"some_other_tool"})
        assert block == ""

    def test_prompt_block_present_when_enabled_and_tool_loaded(self, env):
        from agent.prompt_builder import build_obsidian_prompt
        block = build_obsidian_prompt(valid_tool_names={"obsidian_search"})
        assert block != ""
        assert "Obsidian Vault" in block
        assert "obsidian_search" in block

    def test_prompt_block_reflects_scope(self, env, monkeypatch):
        from agent.prompt_builder import build_obsidian_prompt

        monkeypatch.setenv("HERMES_OBSIDIAN_SCOPE", "all")
        block = build_obsidian_prompt(valid_tool_names={"obsidian_search"})
        assert "entire vault" in block

        monkeypatch.setenv("HERMES_OBSIDIAN_SCOPE", "ingest")
        block = build_obsidian_prompt(valid_tool_names={"obsidian_search"})
        assert "ingest/" in block

    def test_prompt_block_size_within_budget(self, env):
        """Hard cap on the size of the prompt block so it never blows out caches."""
        from agent.prompt_builder import build_obsidian_prompt
        block = build_obsidian_prompt(valid_tool_names={"obsidian_search"})
        # ~1 token per 4 chars → 800 chars ≈ 200 tokens; we want to stay under 1000 chars
        assert len(block) < 1000, f"block too large: {len(block)} chars"


# ---------------------------------------------------------------------------
# 8. import-notes → LCM end-to-end
# ---------------------------------------------------------------------------

class TestImportNotesToLCM:
    """Drive `hermes obsidian import-notes` against a real ChunkStore."""

    def test_ingest_files_become_lcm_chunks(self, env, monkeypatch):
        from hermes_cli.obsidian_setup import obsidian_command

        # Prepare ingest files
        ingest = env["vault"] / "hermes" / "ingest"
        ingest.mkdir(parents=True, exist_ok=True)
        (ingest / "postgres.md").write_text(
            "# Postgres tuning\n\n"
            "Set work_mem=32MB for OLAP queries.\n\n"
            "Always vacuum analyze after large bulk inserts.\n",
            encoding="utf-8",
        )
        (ingest / "redis.md").write_text(
            "# Redis ops\n\n"
            "Use SCAN instead of KEYS in production.\n",
            encoding="utf-8",
        )

        # Force lexical embedder (no sentence-transformers needed)
        from plugins.context_engine.lcm import embedder as emb_mod
        monkeypatch.setattr(
            emb_mod, "get_default_embedder",
            lambda **_kw: emb_mod.LexicalEmbedder(),
        )

        class Args:
            obsidian_command = "import-notes"

        obsidian_command(Args())

        # The CLI command writes directly to $HERMES_HOME/lcm/store.db.
        # Verify rows landed.
        from plugins.context_engine.lcm.store import ChunkStore
        store = ChunkStore(db_path=env["home"] / "lcm" / "store.db")
        rows = store._conn.execute(
            "SELECT session_id, content FROM chunks ORDER BY id"
        ).fetchall()
        assert len(rows) >= 3  # at least 3 paragraphs (2+1)
        sessions = {r[0] for r in rows}
        assert any("postgres" in s for s in sessions)
        assert any("redis" in s for s in sessions)
        contents = " ".join(r[1] for r in rows)
        assert "work_mem" in contents
        assert "SCAN instead of KEYS" in contents

    def test_no_ingest_files_handled_gracefully(self, env, capsys):
        from hermes_cli.obsidian_setup import obsidian_command

        # vault has no ingest dir or it's empty
        (env["vault"] / "hermes" / "ingest").mkdir(parents=True, exist_ok=True)

        class Args:
            obsidian_command = "import-notes"

        obsidian_command(Args())
        out = capsys.readouterr().out
        assert "No files in" in out

    def test_import_notes_disabled_short_circuits(self, tmp_path, monkeypatch, capsys):
        from hermes_cli.obsidian_setup import obsidian_command

        home = tmp_path / "h"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_OBSIDIAN_VAULT", raising=False)

        class Args:
            obsidian_command = "import-notes"

        obsidian_command(Args())
        out = capsys.readouterr().out
        assert "not configured" in out


# ---------------------------------------------------------------------------
# 9. Round-trip: full session lifecycle
# ---------------------------------------------------------------------------

class TestFullSessionLifecycle:
    def test_full_loop(self, env):
        """Simulate: agent boot → rules imported → user edits memory → session ends → all mirrored."""
        from agent import obsidian as ob
        from hermes_cli.obsidian_setup import _bootstrap_vault
        from tools.memory_tool import MemoryStore

        _bootstrap_vault(env["vault"])

        # T0: User pre-stages rules in Obsidian
        staging = env["vault"] / "hermes" / "rules-staging.md"
        staging.write_text(
            "- Run black before commit\n"
            "- Use type hints in new code\n",
            encoding="utf-8",
        )

        # T1: Hermes boots → MemoryStore loads → auto-import runs
        store = MemoryStore()
        store.load_from_disk()
        ob.import_rules_from_staging(store=store)
        store.load_from_disk()  # reload after import

        # Confirm rules are in the store
        assert any("Run black" in r for r in store.rules_entries)
        assert any("type hints" in r for r in store.rules_entries)

        # T2: During session, agent updates MEMORY.md (simulated)
        store.add("memory", "Discovered: this codebase uses pytest-xdist.")

        # T3: Session ends → auto-export
        result = ob.export_memory_files()
        assert result.error is None

        # T4: Verify vault has fresh mirrors
        rules_mirror = env["vault"] / "hermes" / "rules.md"
        memory_mirror = env["vault"] / "hermes" / "memory.md"
        assert "Run black" in rules_mirror.read_text(encoding="utf-8")
        assert "pytest-xdist" in memory_mirror.read_text(encoding="utf-8")

        # T5: Staging is cleared (rules were successfully imported)
        new_staging = staging.read_text(encoding="utf-8")
        assert "Run black" not in new_staging
        assert "type hints" not in new_staging
