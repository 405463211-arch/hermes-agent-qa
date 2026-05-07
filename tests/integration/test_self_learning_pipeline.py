"""End-to-end integration test for the Phase 7 self-learning pipeline.

Threads the full chain together with no agent / no LLM:

    learning_record(error)        → store row recurrence=1, eligible=False
    learning_record(error)        → recurrence=2, still ineligible
    learning_record(error, t3)    → recurrence=3 + 3 distinct tasks → eligible
                                  → auto-promote chain calls add_rule_with_lifecycle
                                  → RULES.md gets the new entry, source=LRN-...
                                  → reading the rendered prompt shows [NEW] marker
    fast-forward 100 days         → run_auto_archive() moves the rule to RULES.archive.md
    consume_archive_notice()      → returns the eviction record once

This is the contract the user signed off on: 5 small optimizations + Phase 7
all tied together via Pattern-Key dedupe, lifecycle metadata, and
double-triggered archive policy.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent.rules_lifecycle import (
    RuleEntry,
    parse_rule_entry,
    serialize_rule_entry,
)
from tools import learning_tool as lt
from tools.memory_tool import MemoryStore


@pytest.fixture
def isolated_pipeline(tmp_path, monkeypatch):
    """Wire learning_tool's singleton store + a fresh MemoryStore in tmp_path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    lt._reset_store_for_tests()
    from agent.learning_store import LearningStore
    fresh_ls = LearningStore(db_path=tmp_path / "ls.db")
    monkeypatch.setattr(lt, "_GLOBAL_STORE", fresh_ls)

    memstore = MemoryStore(
        rules_char_limit=4000,
        auto_archive_rules=True,
        auto_archive_age_days=90,
        auto_archive_recurrence_window=30,
        trial_new_marker_days=7,
    )
    memstore.load_from_disk()

    yield fresh_ls, memstore

    fresh_ls.close()


# ---------------------------------------------------------------------------
# Full chain
# ---------------------------------------------------------------------------


def test_record_recurrence_promotes_to_rules_with_new_marker(isolated_pipeline):
    _, memstore = isolated_pipeline
    import json

    # Three recurrences across three distinct tasks → eligible, auto-promote.
    for tid in ("t1", "t2", "t3"):
        out = json.loads(lt.learning_record_handler(
            {
                "category": "error",
                "pattern_key": "tool.terminal.permission_denied",
                "summary": "permission denied on /etc/foo",
                "suggested_action": "use sudo when writing under /etc",
            },
            store=memstore,
            task_id=tid,
        ))

    assert out["auto_promoted"] is True
    assert out["promoted_to"] == "rules"

    # RULES.md now has exactly one entry, sourced from a learning entry.
    # Source prefix depends on category — ERR- for error, LRN- for learning,
    # FEAT- for feature_request — but all three count as "auto-promoted".
    assert len(memstore.rules_entries) == 1
    parsed = parse_rule_entry(memstore.rules_entries[0])
    assert parsed.is_from_learning() is True
    assert parsed.recurrence == 3
    assert parsed.pattern_key == "tool.terminal.permission_denied"

    # Render the system-prompt block — must include [NEW] for the freshly
    # promoted rule.
    block = memstore._render_block("rules", memstore.rules_entries)
    assert "use sudo when writing under /etc" in block
    assert "[NEW" in block


def test_aged_rule_auto_archives_and_emits_notice(isolated_pipeline):
    _, memstore = isolated_pipeline

    # Manually plant a stale LRN-promoted rule (100 days old, no recurrence).
    old = RuleEntry(
        text="ancient learning rule",
        source="LRN-20260101-001",
        created=date.today() - timedelta(days=120),
        promoted_at=date.today() - timedelta(days=120),
    )
    memstore.rules_entries = [serialize_rule_entry(old)]
    memstore.save_to_disk("rules")

    archived = memstore.run_auto_archive()
    assert len(archived) == 1
    assert archived[0]["reason"] == "age_no_recurrence"
    assert "ancient" in archived[0]["text"]

    # Notice consumed exactly once.
    notice = memstore.consume_archive_notice()
    assert notice == archived
    assert memstore.consume_archive_notice() == []

    # Restore via /rules unarchive equivalent.
    result = memstore.unarchive_rule("LRN-20260101-001")
    assert result["success"] is True
    assert "ancient learning rule" in "\n".join(memstore.rules_entries)


def test_full_pipeline_record_promote_archive_unarchive(isolated_pipeline):
    """The complete loop: record → promote → fast-forward → archive → restore."""
    import json
    _, memstore = isolated_pipeline

    for tid in ("t1", "t2", "t3"):
        json.loads(lt.learning_record_handler(
            {
                "category": "error",
                "pattern_key": "tool.x.y",
                "summary": "thing broke",
                "suggested_action": "do not Y",
            },
            store=memstore,
            task_id=tid,
        ))

    assert len(memstore.rules_entries) == 1
    promoted = parse_rule_entry(memstore.rules_entries[0])

    # Simulate 100 days of dormancy by rewriting created/promoted_at.
    promoted.created = date.today() - timedelta(days=120)
    promoted.promoted_at = date.today() - timedelta(days=120)
    memstore.rules_entries = [serialize_rule_entry(promoted)]
    memstore.save_to_disk("rules")

    archived = memstore.run_auto_archive()
    assert len(archived) == 1
    assert memstore.list_archived_rules()[0]["text"] == "do not Y"

    restored = memstore.unarchive_rule(promoted.source)
    assert restored["success"] is True
    assert "do not Y" in "\n".join(memstore.rules_entries)
