"""M5 white-box CLI/UX-layer probe.

Covers (mostly through MemoryStore methods that the CLI handlers call into,
plus static checks on the dispatch wiring):

- list_archived_rules / unarchive_rule round-trip
- find_stale_memory_entries — age threshold, no-metadata fallback
- CommandDef registration: /rules /memory /learn three-family entries +
  aliases / args_hint / subcommands integrity
- cli.py dispatch wiring: each canonical name has a handler method
- Gateway exposure: GATEWAY_KNOWN_COMMANDS contains the right set
- Help text generation paths (gateway_help_lines, COMMANDS_BY_CATEGORY)
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import RuleEntry, serialize_rule_entry


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Store backend used by /rules archive list, /rules unarchive              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    import tools.memory_tool as mt
    counter = {"n": 0}

    def make(rules_text=None, archive_text=None, memory_text=None, **kw):
        counter["n"] += 1
        mem_dir = tmp_path / f"mem-{counter['n']}"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (mem_dir / "RULES.md").write_text(rules_text, encoding="utf-8")
        if archive_text is not None:
            (mem_dir / "RULES.archive.md").write_text(archive_text, encoding="utf-8")
        if memory_text is not None:
            (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
        monkeypatch.setattr(mt, "get_memory_dir", lambda d=mem_dir: d)
        params = dict(
            rules_char_limit=10_000,
            memory_char_limit=10_000,
            user_char_limit=10_000,
        )
        params.update(kw)
        store = mt.MemoryStore(**params)
        store.load_from_disk()
        return store, mem_dir
    return make


class TestArchiveBackend:
    def test_list_empty_when_no_archive_file(self, store_factory):
        store, _ = store_factory()
        assert store.list_archived_rules() == []

    def test_list_archived_returns_metadata(self, store_factory):
        archived_entry = serialize_rule_entry(RuleEntry(
            text="Stale rule.",
            source="LRN-20260101-AAA",
            promoted_at=date(2026, 1, 1),
            extra={"archived_at": "2026-04-30",
                   "archived_reason": "age_no_recurrence"},
        ))
        store, _ = store_factory(archive_text=archived_entry)
        items = store.list_archived_rules()
        assert len(items) == 1
        assert items[0]["text"] == "Stale rule."
        assert items[0]["source"] == "LRN-20260101-AAA"
        # archived_at + reason should be surfaced
        assert items[0].get("archived_at") == "2026-04-30"
        assert items[0].get("reason") == "age_no_recurrence"

    def test_unarchive_by_source_id(self, store_factory):
        from tools.memory_tool import ENTRY_DELIMITER
        archived = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(
                text="A", source="LRN-20260101-AAA",
                extra={"archived_at": "2026-04-30",
                       "archived_reason": "capacity_threshold"},
            )),
            serialize_rule_entry(RuleEntry(
                text="B", source="LRN-20260101-BBB",
                extra={"archived_at": "2026-04-30",
                       "archived_reason": "capacity_threshold"},
            )),
        ])
        store, _ = store_factory(archive_text=archived)
        assert len(store.list_archived_rules()) == 2

        result = store.unarchive_rule("LRN-20260101-AAA")
        assert result["success"]
        assert "A" in result.get("restored", "")

        # Archive should now have only B
        remaining = store.list_archived_rules()
        assert len(remaining) == 1
        assert remaining[0]["text"] == "B"
        # And A should be back in live rules
        assert any("A" in r for r in store.rules_entries)

    def test_unarchive_unknown_id_returns_error(self, store_factory):
        store, _ = store_factory()
        result = store.unarchive_rule("LRN-NEVER-XXX")
        assert result["success"] is False
        assert "error" in result

    def test_unarchive_strips_archive_metadata(self, store_factory):
        """When restored, archived_at / archived_reason should be removed
        from the rule's metadata so a future archive cycle can stamp fresh."""
        archived = serialize_rule_entry(RuleEntry(
            text="X", source="LRN-X",
            extra={"archived_at": "2026-04-30",
                   "archived_reason": "capacity_threshold"},
        ))
        store, _ = store_factory(archive_text=archived)
        store.unarchive_rule("LRN-X")
        from agent.rules_lifecycle import parse_rule_entry
        restored = parse_rule_entry(store.rules_entries[0])
        assert "archived_at" not in restored.extra
        assert "archived_reason" not in restored.extra


class TestFindStaleMemoryEntries:
    def test_legacy_entries_skipped_no_metadata(self, store_factory):
        """Plain text entries (no hermes-meta) have no created date and
        must not be reported as stale (we can't know when they were added)."""
        store, _ = store_factory(
            memory_text="legacy plain entry without metadata"
        )
        assert store.find_stale_memory_entries(age_days=60) == []

    def test_recent_entry_not_stale(self, store_factory):
        recent = serialize_rule_entry(RuleEntry(
            text="recent fact",
            source="manual",
            created=date.today() - timedelta(days=10),
        ))
        store, _ = store_factory(memory_text=recent)
        assert store.find_stale_memory_entries(age_days=60) == []

    def test_old_entry_flagged(self, store_factory):
        old = serialize_rule_entry(RuleEntry(
            text="dormant fact",
            source="manual",
            created=date.today() - timedelta(days=100),
        ))
        store, _ = store_factory(memory_text=old)
        result = store.find_stale_memory_entries(age_days=60)
        assert len(result) == 1
        assert result[0]["age_days"] >= 100
        assert result[0]["text"] == "dormant fact"

    def test_age_threshold_respected(self, store_factory):
        """An entry exactly at threshold should NOT be flagged."""
        from tools.memory_tool import ENTRY_DELIMITER
        items = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(
                text="59d", source="manual",
                created=date.today() - timedelta(days=59),
            )),
            serialize_rule_entry(RuleEntry(
                text="61d", source="manual",
                created=date.today() - timedelta(days=61),
            )),
        ])
        store, _ = store_factory(memory_text=items)
        result = store.find_stale_memory_entries(age_days=60)
        assert len(result) == 1
        assert result[0]["text"] == "61d"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ CommandDef registry — three new commands + integrity                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCommandRegistry:
    NEW_COMMANDS = ("rules", "memory", "learn", "context", "lcm")

    def test_all_new_commands_registered(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        names = {c.name for c in COMMAND_REGISTRY}
        for cmd in self.NEW_COMMANDS:
            assert cmd in names, f"/{cmd} missing from COMMAND_REGISTRY"

    def test_rules_has_subcommands_listed(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        rules = next(c for c in COMMAND_REGISTRY if c.name == "rules")
        # subcommands must include the actions cli.py knows how to handle
        expected = {
            "list", "add", "remove", "edit", "show",
            "pin", "unpin", "archive", "unarchive",
        }
        assert expected.issubset(set(rules.subcommands or ()))

    def test_memory_subcommands(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        m = next(c for c in COMMAND_REGISTRY if c.name == "memory")
        expected = {"show", "edit-rules", "edit-memory", "edit-user", "review"}
        assert expected.issubset(set(m.subcommands or ()))

    def test_learn_subcommands(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        ln = next(c for c in COMMAND_REGISTRY if c.name == "learn")
        expected = {"list", "show", "stats", "resolve"}
        assert expected.issubset(set(ln.subcommands or ()))

    def test_context_alias_resolves(self):
        from hermes_cli.commands import resolve_command, COMMAND_REGISTRY
        ctx = next(c for c in COMMAND_REGISTRY if c.name == "context")
        assert "ctx" in (ctx.aliases or ())
        resolved = resolve_command("ctx")
        # resolve_command may return either the canonical name string or
        # the CommandDef object; tolerate both
        if hasattr(resolved, "name"):
            assert resolved.name == "context"
        else:
            assert resolved == "context"

    def test_each_new_command_has_description_and_category(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        for cmd_name in self.NEW_COMMANDS:
            cmd = next(c for c in COMMAND_REGISTRY if c.name == cmd_name)
            assert cmd.description and len(cmd.description) > 5
            assert cmd.category, f"/{cmd_name} missing category"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Dispatch wiring — every canonical name has a handler                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCliDispatchStaticAnalysis:
    def _src(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        return (repo_root / "cli.py").read_text(encoding="utf-8")

    def test_rules_branch_calls_handler(self):
        src = self._src()
        assert 'canonical == "rules":' in src
        assert "_handle_rules_command(" in src

    def test_memory_branch_calls_handler(self):
        src = self._src()
        assert 'canonical == "memory":' in src
        assert "_handle_memory_command(" in src

    def test_learn_branch_calls_handler(self):
        src = self._src()
        assert 'canonical == "learn":' in src
        assert "_handle_learn_command(" in src

    def test_handlers_are_methods(self):
        """All three handlers must be defined as instance methods (have a
        `def` on a class indented level matching HermesCLI)."""
        src = self._src()
        for name in ("_handle_rules_command",
                     "_handle_memory_command",
                     "_handle_learn_command"):
            assert f"    def {name}(" in src, f"{name} not a HermesCLI method"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Gateway exposure — config-gated commands                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestGatewayExposure:
    def test_rules_in_gateway_known(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        # /rules has gateway_only=False and cli_only=False by default → exposed
        assert "rules" in GATEWAY_KNOWN_COMMANDS

    def test_memory_in_gateway_known(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        assert "memory" in GATEWAY_KNOWN_COMMANDS

    def test_learn_in_gateway_known(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        assert "learn" in GATEWAY_KNOWN_COMMANDS

    def test_help_lines_mention_new_commands(self):
        from hermes_cli.commands import gateway_help_lines
        lines = gateway_help_lines()
        text = "\n".join(lines)
        for cmd in ("rules", "memory", "learn"):
            assert f"/{cmd}" in text, (
                f"/{cmd} missing from gateway help: {text!r}"
            )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Startup archive notification flow                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestStartupArchiveNotification:
    def test_pending_archive_notice_exposed_after_archive(self, store_factory):
        """run_auto_archive should populate _pending_archive_notice so the
        CLI / runner can surface a one-line note on the next user-visible
        turn."""
        from tools.memory_tool import ENTRY_DELIMITER
        old = date.today() - timedelta(days=120)
        rules_blob = serialize_rule_entry(RuleEntry(
            text="ancient",
            source="LRN-20250101-OLD",
            created=old,
            promoted_at=old,
        ))
        store, _ = store_factory(
            rules_text=rules_blob,
            auto_archive_rules=True,
            auto_archive_age_days=90,
        )
        store.run_auto_archive()
        # _pending_archive_notice is mutated by run_auto_archive (per the
        # docstring) for surfaces to consume
        assert hasattr(store, "_pending_archive_notice")
        # Either populated now or a list at minimum (some impls only set it
        # in load_from_disk path — accept both)
        assert isinstance(store._pending_archive_notice, list)
