"""M3 white-box agent-integration probe.

Covers:
- MemoryStore.format_rules_by_tier() — pinned + regular sections + [NEW] markers
- MemoryStore.run_auto_archive() — end-to-end: writes RULES.md, creates
  RULES.archive.md with metadata, returns notice payload
- run_agent _invoke_tool dispatch — verify both _invoke_tool (main path) and
  worker-loop branches forward store=self._memory_store to learning_record
  and project_knowledge_promote (no auto-promote without store)
- System prompt assembly — pinned tier comes BEFORE regular tier, and BOTH
  come BEFORE memory + user blocks (cache-stability invariant)
- Token-budget sanity — enormous rules don't blow out into infinity
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import (
    RuleEntry,
    parse_rule_entry,
    serialize_rule_entry,
)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ format_rules_by_tier — two-tier rendering                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """Factory for fresh MemoryStores rooted in isolated tmp_paths."""
    import tools.memory_tool as mt

    counter = {"n": 0}

    def make(rules_text=None, **kwargs):
        counter["n"] += 1
        mem_dir = tmp_path / f"mem-{counter['n']}"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (mem_dir / "RULES.md").write_text(rules_text, encoding="utf-8")
        monkeypatch.setattr(mt, "get_memory_dir", lambda d=mem_dir: d)
        params = dict(
            rules_char_limit=10_000,
            memory_char_limit=10_000,
            user_char_limit=10_000,
        )
        params.update(kwargs)  # caller wins
        store = mt.MemoryStore(**params)
        store.load_from_disk()
        return store, mem_dir

    return make


class TestFormatRulesByTier:
    def test_empty_returns_empty_blocks(self, store_factory):
        store, _ = store_factory()
        tiers = store.format_rules_by_tier()
        assert tiers["pinned"] == ""
        assert tiers["regular"] == ""

    def test_pinned_only(self, store_factory):
        rule = serialize_rule_entry(RuleEntry(
            text="Pinned rule.", pinned=True, source="manual"
        ))
        store, _ = store_factory(rules_text=rule)
        tiers = store.format_rules_by_tier()
        assert "Pinned rule." in tiers["pinned"]
        assert "PINNED" in tiers["pinned"].upper()
        assert tiers["regular"] == ""

    def test_both_tiers_separated(self, store_factory):
        # 1 pinned + 2 regular
        from tools.memory_tool import ENTRY_DELIMITER
        items = [
            serialize_rule_entry(RuleEntry(
                text="P1.", pinned=True, source="manual"
            )),
            serialize_rule_entry(RuleEntry(
                text="R1.", pinned=False, source="manual"
            )),
            serialize_rule_entry(RuleEntry(
                text="R2.", pinned=False, source="manual"
            )),
        ]
        store, _ = store_factory(rules_text=ENTRY_DELIMITER.join(items))
        tiers = store.format_rules_by_tier()
        assert "P1." in tiers["pinned"]
        assert "P1." not in tiers["regular"]
        assert "R1." in tiers["regular"] and "R2." in tiers["regular"]
        assert "R1." not in tiers["pinned"]

    def test_new_marker_only_in_regular_tier(self, store_factory):
        """Pinned rules already stand out; [NEW] tag only goes on regular tier."""
        from tools.memory_tool import ENTRY_DELIMITER
        today = date.today()
        promoted = today  # promoted today → within 7-day NEW window
        items = [
            serialize_rule_entry(RuleEntry(
                text="Pinned LRN.", pinned=True,
                source="LRN-99999999-PIN", promoted_at=promoted,
            )),
            serialize_rule_entry(RuleEntry(
                text="Regular LRN.", pinned=False,
                source="LRN-99999999-REG", promoted_at=promoted,
            )),
        ]
        store, _ = store_factory(rules_text=ENTRY_DELIMITER.join(items))
        tiers = store.format_rules_by_tier()
        assert "[NEW" not in tiers["pinned"], (
            "Pinned tier must NOT show [NEW] marker"
        )
        assert "[NEW" in tiers["regular"], (
            f"Regular tier must show [NEW] marker; got: {tiers['regular']!r}"
        )

    def test_old_promotion_no_new_marker(self, store_factory):
        """Past the NEW window, regular tier shows no marker."""
        old = date.today() - timedelta(days=30)
        rule = serialize_rule_entry(RuleEntry(
            text="Old rule.", source="LRN-20260101-XYZ", promoted_at=old,
        ))
        store, _ = store_factory(rules_text=rule)
        tiers = store.format_rules_by_tier()
        assert "[NEW" not in tiers["regular"]

    def test_deterministic_output_for_same_input(self, store_factory):
        """**Cache-stability invariant**: same store contents must always
        produce byte-identical tier output. Critical for prefix caching."""
        from tools.memory_tool import ENTRY_DELIMITER
        rules_blob = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(text="A", pinned=True)),
            serialize_rule_entry(RuleEntry(text="B", pinned=False)),
        ])
        store, _ = store_factory(rules_text=rules_blob)
        first = store.format_rules_by_tier()
        second = store.format_rules_by_tier()
        assert first == second


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ run_auto_archive — end-to-end                                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestRunAutoArchive:
    def test_disabled_when_flag_off(self, store_factory):
        from tools.memory_tool import ENTRY_DELIMITER
        rules_blob = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(text="X" * 500, pinned=False))
            for _ in range(20)
        ])
        store, _ = store_factory(
            rules_text=rules_blob,
            auto_archive_rules=False,  # explicitly disabled
        )
        assert store.run_auto_archive() == []

    def test_capacity_trigger_writes_archive_file(
        self, store_factory
    ):
        """Build a RULES.md that exceeds 80% of a small limit, run archive,
        verify RULES.archive.md gets created with the evicted rules."""
        from tools.memory_tool import ENTRY_DELIMITER
        # 20 rules × ~500 chars each = ~10k. Limit=2000 → 80% = 1600.
        items = [
            serialize_rule_entry(RuleEntry(
                text=f"Rule {i}: " + ("x" * 500),
                pinned=False,
                source="manual",
                created=date(2026, 1, 1) + timedelta(days=i),
            )) for i in range(20)
        ]
        rules_blob = ENTRY_DELIMITER.join(items)
        store, mem_dir = store_factory(
            rules_text=rules_blob,
            rules_char_limit=2000,
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.80,
            auto_archive_age_days=0,  # disable age trigger
        )
        # Force load: store_factory already calls load_from_disk; reload
        # directly to be safe in case rules_char_limit was overridden post-load
        store.rules_char_limit = 2000
        result = store.run_auto_archive()

        assert result, "expected some entries to be archived"
        assert all(r["reason"] == "capacity_threshold" for r in result)

        archive_path = mem_dir / "RULES.archive.md"
        assert archive_path.exists(), "RULES.archive.md must be created"
        archive_text = archive_path.read_text()
        # Each archived block contains its own metadata with archived_at
        assert "archived_at=" in archive_text
        assert "archived_reason=capacity_threshold" in archive_text

        # And the live RULES.md must have shrunk
        rules_path = mem_dir / "RULES.md"
        assert rules_path.exists()
        new_size = len(rules_path.read_text())
        assert new_size < len(rules_blob), (
            f"RULES.md not pruned: was {len(rules_blob)}, now {new_size}"
        )

    def test_age_trigger_archives_dormant_lrn(self, store_factory):
        """A 100-day-old LRN rule with no recurrence should get evicted."""
        from tools.memory_tool import ENTRY_DELIMITER
        old = date.today() - timedelta(days=100)
        items = [
            serialize_rule_entry(RuleEntry(
                text="recently edited",
                source="LRN-20250101-001",
                created=old,
                promoted_at=old,
                last_edited=date.today(),  # protected
            )),
            serialize_rule_entry(RuleEntry(
                text="stale and dormant",
                source="LRN-20250101-002",
                created=old,
                promoted_at=old,
            )),
        ]
        store, _ = store_factory(
            rules_text=ENTRY_DELIMITER.join(items),
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.80,
            auto_archive_age_days=90,
            auto_archive_recurrence_window=30,
        )
        result = store.run_auto_archive()
        assert len(result) == 1
        assert result[0]["text"] == "stale and dormant"
        assert result[0]["reason"] == "age_no_recurrence"

    def test_pinned_never_archived_under_capacity(self, store_factory):
        from tools.memory_tool import ENTRY_DELIMITER
        items = [
            serialize_rule_entry(RuleEntry(
                text=("z" * 800),
                pinned=(i == 0),
                source="manual",
                created=date(2026, 1, 1) + timedelta(days=i),
            )) for i in range(10)
        ]
        store, _ = store_factory(
            rules_text=ENTRY_DELIMITER.join(items),
            rules_char_limit=2000,
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.80,
            auto_archive_age_days=0,
        )
        store.rules_char_limit = 2000
        result = store.run_auto_archive()
        # Pinned rule should have survived
        for r in result:
            assert r["text"] != ("z" * 800) or "pinned" not in r.get("source", "").lower()
        # Live RULES.md must still contain the pinned rule's text
        rules_text = "\n".join(store.rules_entries)
        # pinned rule's text is just z*800 — present in survivors
        assert "pinned=true" in rules_text


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Dispatch routing — both main path and worker path forward `store`        ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestDispatchRoutingStaticAnalysis:
    """The dispatch wiring is replicated in TWO places (main _invoke_tool
    and the concurrent-worker block). This test makes sure neither
    branch silently lost its `store=self._memory_store` kwarg."""

    def _read_run_agent(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        return (repo_root / "run_agent.py").read_text(encoding="utf-8")

    def test_invoke_tool_passes_store_to_learning_record(self):
        text = self._read_run_agent()
        # _invoke_tool main path
        idx = text.find('elif function_name == "learning_record":')
        assert idx > 0, "learning_record dispatch missing in run_agent.py"
        # Look for the next ~400 chars; must contain `store=self._memory_store`
        window = text[idx : idx + 600]
        assert "store=self._memory_store" in window, (
            "main-path learning_record dispatch lost the store kwarg!\n"
            f"window:\n{window!r}"
        )

    def test_worker_loop_passes_store_to_learning_record(self):
        text = self._read_run_agent()
        # The worker path has a UNIQUE marker — it's followed by the
        # _vprint(_get_cute_tool_message_impl('learning_record', ...)) call.
        worker_marker = "_get_cute_tool_message_impl('learning_record'"
        idx = text.find(worker_marker)
        assert idx > 0, (
            "worker-path learning_record dispatch missing — the UI message "
            "block isn't there"
        )
        # Walk backwards to find the elif and verify store kwarg exists in
        # the 600 chars BEFORE the UI message.
        window = text[max(0, idx - 800) : idx]
        assert "store=self._memory_store" in window, (
            "worker-path learning_record dispatch lost the store kwarg!"
        )

    def test_invoke_tool_passes_store_to_project_knowledge_promote(self):
        text = self._read_run_agent()
        idx = text.find('elif function_name == "project_knowledge_promote":')
        assert idx > 0
        # First occurrence is the main path
        window = text[idx : idx + 600]
        assert "store=self._memory_store" in window

    def test_worker_path_passes_store_to_project_knowledge_promote(self):
        text = self._read_run_agent()
        # Find both occurrences — worker is the second one
        first = text.find('elif function_name == "project_knowledge_promote":')
        assert first > 0
        second = text.find(
            'elif function_name == "project_knowledge_promote":',
            first + 1,
        )
        assert second > 0, "worker-path PK promote dispatch missing!"
        window = text[second : second + 800]
        assert "store=self._memory_store" in window


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ System prompt assembly — order invariants                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestSystemPromptOrderInvariants:
    """We can't easily build a full AIAgent in unit tests, but we CAN
    verify the order rule statically: the source code of _build_system_prompt
    must do (pinned, regular, ...other..., memory, user) in that exact order."""

    def _src(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        return (repo_root / "run_agent.py").read_text(encoding="utf-8")

    def test_pinned_appended_before_regular(self):
        src = self._src()
        i_pinned = src.find('prompt_parts.append(tiers["pinned"])')
        i_regular = src.find('prompt_parts.append(tiers["regular"])')
        assert 0 < i_pinned < i_regular, (
            f"pinned must be appended before regular (got {i_pinned}, {i_regular})"
        )

    def test_rules_tiers_appended_before_memory_block(self):
        src = self._src()
        i_regular = src.find('prompt_parts.append(tiers["regular"])')
        i_mem = src.find('format_for_system_prompt("memory")')
        assert 0 < i_regular < i_mem, (
            "regular rules tier must be appended before memory block "
            f"(got {i_regular}, {i_mem})"
        )

    def test_rules_tiers_appended_before_user_block(self):
        src = self._src()
        i_regular = src.find('prompt_parts.append(tiers["regular"])')
        i_user = src.find('format_for_system_prompt("user")')
        assert 0 < i_regular < i_user


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Token / size budget sanity                                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestTokenBudgetSanity:
    def test_rules_block_size_bounded_by_input(self, store_factory):
        """Output tier strings should be roughly proportional to input rules
        — definitely no exponential blowup."""
        from tools.memory_tool import ENTRY_DELIMITER
        items = [
            serialize_rule_entry(RuleEntry(
                text=f"Rule body {i}.", pinned=(i % 3 == 0), source="manual"
            )) for i in range(50)
        ]
        rules_blob = ENTRY_DELIMITER.join(items)
        store, _ = store_factory(rules_text=rules_blob)
        tiers = store.format_rules_by_tier()
        total = len(tiers["pinned"]) + len(tiers["regular"])
        # Output should be at most 5x the input (headers + delimiters) — anything
        # higher signals an exponential bug.
        assert total < 5 * len(rules_blob), (
            f"rules block expanded {total/max(1, len(rules_blob)):.1f}x its input"
        )

    def test_format_for_system_prompt_uses_snapshot(self, store_factory):
        """Cache-stability red line: even after an in-memory mutation,
        format_for_system_prompt('rules') returns the SNAPSHOT, not live state."""
        from tools.memory_tool import ENTRY_DELIMITER
        original = serialize_rule_entry(
            RuleEntry(text="Original.", pinned=False, source="manual")
        )
        store, _ = store_factory(rules_text=original)
        first = store.format_for_system_prompt("rules")

        # Mutate live entries (simulate a tool call adding a rule mid-session)
        store.rules_entries.append(
            serialize_rule_entry(
                RuleEntry(text="Added mid-session.", pinned=False)
            )
        )
        second = store.format_for_system_prompt("rules")
        assert first == second, (
            "format_for_system_prompt MUST return the frozen snapshot to "
            "preserve prefix caching. Got divergent output:\n"
            f"first:\n{first}\nsecond:\n{second}"
        )
