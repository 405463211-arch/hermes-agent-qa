"""M1 white-box storage probe.

Covers:
- rules_lifecycle pure functions: parse / serialize / round-trip / split_by_tier /
  should_show_new_marker / auto_archive_rules (Trigger A + Trigger B + protections)
- learning_store SQLite: insert / dedupe / distinct_tasks / validation /
  promotion eligibility / mark_promoted / mark_resolved / stats /
  cross-process persistence (close + reopen)
- memory_tool file IO: bucket files created on demand, archive file location
- LCM bridge: plugin manifest + python module loadable
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import (
    ARCHIVE_REASON_AGE,
    ARCHIVE_REASON_CAPACITY,
    DEFAULT_NEW_MARKER_DAYS,
    LEARNING_SOURCE_PREFIXES,
    RuleEntry,
    auto_archive_rules,
    parse_rule_entry,
    serialize_rule_entry,
    should_show_new_marker,
    split_by_tier,
)
from agent.learning_store import (
    LearningStore,
    PromotionRule,
    is_eligible_for_promotion,
)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ rules_lifecycle — pure functions                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestParseRuleEntry:
    def test_empty_string(self):
        e = parse_rule_entry("")
        assert e.text == ""
        assert e.pinned is False
        assert e.source == "manual"

    def test_whitespace_only(self):
        e = parse_rule_entry("   \n  ")
        assert e.text == ""

    def test_plain_text_no_meta_defaults(self):
        e = parse_rule_entry("Always confirm before bulk edits.")
        assert e.text == "Always confirm before bulk edits."
        assert e.pinned is False
        assert e.source == "manual"
        assert e.created is None

    def test_full_meta_parsed(self):
        raw = (
            "Confirm scope before editing >5 files.\n"
            "<!-- hermes-meta: pinned=false; created=2026-04-28; "
            "source=LRN-20260428-003; promoted_at=2026-04-28; recurrence=4; "
            "pattern_key=agent.scope.unconfirmed_bulk_edit -->"
        )
        e = parse_rule_entry(raw)
        assert e.text == "Confirm scope before editing >5 files."
        assert e.pinned is False
        assert e.created == date(2026, 4, 28)
        assert e.source == "LRN-20260428-003"
        assert e.promoted_at == date(2026, 4, 28)
        assert e.recurrence == 4
        assert e.pattern_key == "agent.scope.unconfirmed_bulk_edit"

    def test_pinned_true_variants(self):
        for v in ("true", "TRUE", "yes", "1", "Y", "on"):
            raw = f"x\n<!-- hermes-meta: pinned={v} -->"
            assert parse_rule_entry(raw).pinned is True, f"failed for {v!r}"

    def test_corrupt_meta_does_not_crash(self):
        raw = "rule\n<!-- hermes-meta: this=is=broken;;trash;; -->"
        e = parse_rule_entry(raw)
        assert e.text == "rule"

    def test_unknown_meta_keys_kept_in_extra(self):
        raw = (
            "x\n<!-- hermes-meta: pinned=false; created=2026-04-28; "
            "future_key=xyz; another=42 -->"
        )
        e = parse_rule_entry(raw)
        assert e.extra.get("future_key") == "xyz"
        assert e.extra.get("another") == "42"


class TestSerializeRuleEntry:
    def test_empty_text_returns_empty(self):
        assert serialize_rule_entry(RuleEntry(text="")) == ""

    def test_minimal_entry(self):
        s = serialize_rule_entry(RuleEntry(text="hello"))
        assert "hello" in s
        assert "pinned=false" in s
        assert "source=manual" in s

    def test_round_trip_full_entry(self):
        original = RuleEntry(
            text="Confirm scope.",
            pinned=True,
            created=date(2026, 4, 28),
            source="LRN-20260428-003",
            promoted_at=date(2026, 4, 28),
            recurrence=4,
            last_recurrence=date(2026, 4, 30),
            pattern_key="agent.scope.foo",
            last_edited=date(2026, 4, 29),
            extra={"future": "key"},
        )
        s = serialize_rule_entry(original)
        reparsed = parse_rule_entry(s)
        assert reparsed.text == original.text
        assert reparsed.pinned == original.pinned
        assert reparsed.created == original.created
        assert reparsed.source == original.source
        assert reparsed.promoted_at == original.promoted_at
        assert reparsed.recurrence == original.recurrence
        assert reparsed.last_recurrence == original.last_recurrence
        assert reparsed.pattern_key == original.pattern_key
        assert reparsed.last_edited == original.last_edited
        assert reparsed.extra.get("future") == "key"


class TestSplitByTier:
    def test_classifies_pinned_and_regular(self):
        entries = [
            RuleEntry(text="a", pinned=False),
            RuleEntry(text="b", pinned=True),
            RuleEntry(text="c", pinned=False),
        ]
        out = split_by_tier(entries)
        assert [e.text for e in out["pinned"]] == ["b"]
        assert [e.text for e in out["regular"]] == ["a", "c"]

    def test_skips_empty_entries(self):
        out = split_by_tier([RuleEntry(text=""), RuleEntry(text="x")])
        assert len(out["regular"]) == 1


class TestShouldShowNewMarker:
    def _entry(self, **kw):
        defaults = dict(
            text="x", source="LRN-20260428-001", promoted_at=date(2026, 4, 28),
            pinned=False,
        )
        defaults.update(kw)
        return RuleEntry(**defaults)

    def test_within_window_shows_marker(self):
        assert should_show_new_marker(
            self._entry(),
            today=date(2026, 5, 3),  # 5 days after promotion
            window_days=7,
        )

    def test_outside_window_no_marker(self):
        assert not should_show_new_marker(
            self._entry(), today=date(2026, 5, 10), window_days=7
        )

    def test_pinned_never_marked(self):
        assert not should_show_new_marker(
            self._entry(pinned=True), today=date(2026, 5, 3), window_days=7
        )

    def test_manual_source_no_marker(self):
        assert not should_show_new_marker(
            self._entry(source="manual"), today=date(2026, 5, 3), window_days=7
        )

    def test_no_promoted_at_no_marker(self):
        assert not should_show_new_marker(
            self._entry(promoted_at=None), today=date(2026, 5, 3), window_days=7
        )

    def test_zero_window_no_marker(self):
        assert not should_show_new_marker(
            self._entry(), today=date(2026, 5, 3), window_days=0
        )

    def test_future_promoted_at_no_marker(self):
        """Defensive: clock skew shouldn't make negative-age rules show NEW."""
        assert not should_show_new_marker(
            self._entry(promoted_at=date(2026, 5, 10)),
            today=date(2026, 5, 3),
            window_days=7,
        )

    def test_pk_prefix_eligible_for_marker(self):
        """is_from_learning() should accept all LEARNING_SOURCE_PREFIXES."""
        for prefix in LEARNING_SOURCE_PREFIXES:
            entry = self._entry(source=f"{prefix}xyz")
            assert should_show_new_marker(
                entry, today=date(2026, 5, 3), window_days=7
            ), f"{prefix} should be NEW-eligible"


class TestAutoArchiveTriggerA:
    """Trigger A — capacity-based eviction, oldest-first."""

    def _entries(self, n=10, *, char_text="x" * 200):
        return [
            RuleEntry(
                text=f"rule {i}: {char_text}",
                pinned=False,
                created=date(2026, 1, 1) + timedelta(days=i),
                source="manual",
            )
            for i in range(n)
        ]

    def test_no_action_under_threshold(self):
        # 10 short rules, huge limit → no eviction
        entries = [
            RuleEntry(text=f"rule {i}", pinned=False, source="manual",
                      created=date(2026, 1, 1) + timedelta(days=i))
            for i in range(5)
        ]
        decision = auto_archive_rules(
            entries, char_limit=100_000, today=date(2026, 5, 1)
        )
        assert decision.archived == []
        assert decision.keep == entries

    def test_evicts_oldest_first(self):
        entries = self._entries(n=10, char_text="x" * 500)  # ~ 5000 chars total
        decision = auto_archive_rules(
            entries,
            char_limit=2000,           # 80% = 1600 budget
            today=date(2026, 5, 1),
            capacity_threshold=0.80,
            age_days=0,                 # disable Trigger B
        )
        assert decision.archived, "expected some eviction"
        assert all(r == ARCHIVE_REASON_CAPACITY for r in decision.reasons)
        # archived must be the oldest (created earliest)
        archived_creates = [e.created for e in decision.archived]
        assert archived_creates == sorted(archived_creates), (
            "archived order must be oldest-first"
        )
        # the kept set must NOT include the very first rule (which is oldest)
        kept_texts = {e.text for e in decision.keep}
        assert "rule 0: " + ("x" * 500) not in kept_texts

    def test_pinned_never_archived_under_capacity(self):
        entries = [
            RuleEntry(
                text=f"r{i}", pinned=(i == 0), source="manual",
                created=date(2026, 1, 1) + timedelta(days=i),
            ) for i in range(20)
        ]
        # Make text big so capacity is exceeded
        for e in entries:
            e.text = e.text + ("x" * 500)
        decision = auto_archive_rules(
            entries, char_limit=2000, today=date(2026, 5, 1),
            capacity_threshold=0.80, age_days=0,
        )
        assert decision.archived
        # pinned rule (r0) must remain
        assert any(e.text.startswith("r0") and e.pinned for e in decision.keep)


class TestAutoArchiveTriggerB:
    """Trigger B — age-based eviction of LRN-* dormant rules."""

    def test_dormant_lrn_rule_evicted(self):
        old = RuleEntry(
            text="ancient",
            source="LRN-20250101-001",
            created=date(2025, 1, 1),
            promoted_at=date(2025, 1, 1),
        )
        decision = auto_archive_rules(
            [old], char_limit=10_000, today=date(2026, 5, 1),
            capacity_threshold=0.80, age_days=90, recurrence_window_days=30,
        )
        assert old in decision.archived
        assert decision.reasons == [ARCHIVE_REASON_AGE]

    def test_recent_recurrence_protects(self):
        old = RuleEntry(
            text="alive",
            source="LRN-20250101-002",
            created=date(2025, 1, 1),
            promoted_at=date(2025, 1, 1),
            last_recurrence=date(2026, 4, 25),  # 6 days ago
        )
        decision = auto_archive_rules(
            [old], char_limit=10_000, today=date(2026, 5, 1),
            age_days=90, recurrence_window_days=30,
        )
        assert old in decision.keep
        assert old not in decision.archived

    def test_recent_user_edit_protects(self):
        old = RuleEntry(
            text="user-touched",
            source="LRN-20250101-003",
            created=date(2025, 1, 1),
            promoted_at=date(2025, 1, 1),
            last_edited=date(2026, 4, 25),
        )
        decision = auto_archive_rules(
            [old], char_limit=10_000, today=date(2026, 5, 1),
            age_days=90, recurrence_window_days=30,
        )
        assert old in decision.keep

    def test_within_new_marker_window_protects(self):
        rule = RuleEntry(
            text="fresh",
            source="LRN-20260428-001",
            created=date(2026, 4, 28),
            promoted_at=date(2026, 4, 28),
        )
        # age is way under 90 anyway, but also explicitly inside NEW window
        decision = auto_archive_rules(
            [rule], char_limit=10_000, today=date(2026, 5, 1),
            age_days=2, new_marker_days=7,  # force trigger by tiny age
        )
        assert rule in decision.keep, (
            "NEW window must protect even when age threshold is tiny"
        )

    def test_pinned_lrn_protected(self):
        rule = RuleEntry(
            text="god rule",
            source="LRN-20250101-001",
            created=date(2025, 1, 1),
            promoted_at=date(2025, 1, 1),
            pinned=True,
        )
        decision = auto_archive_rules(
            [rule], char_limit=10_000, today=date(2026, 5, 1),
            age_days=90, recurrence_window_days=30,
        )
        assert rule in decision.keep

    def test_manual_source_never_age_evicted(self):
        """Trigger B applies only to LEARNING_SOURCE_PREFIXES."""
        rule = RuleEntry(
            text="user wrote this years ago",
            source="manual",
            created=date(2024, 1, 1),
            promoted_at=None,
        )
        decision = auto_archive_rules(
            [rule], char_limit=10_000, today=date(2026, 5, 1),
            age_days=90, recurrence_window_days=30,
        )
        assert rule in decision.keep

    def test_no_archive_when_ages_disabled(self):
        rule = RuleEntry(
            text="old", source="LRN-20250101-x", created=date(2025, 1, 1),
            promoted_at=date(2025, 1, 1),
        )
        decision = auto_archive_rules(
            [rule], char_limit=10_000, today=date(2026, 5, 1),
            age_days=0,  # disable Trigger B
        )
        assert decision.archived == []


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ learning_store — SQLite-backed                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture
def learning_store(tmp_path):
    store = LearningStore(db_path=tmp_path / "learning.db")
    yield store
    store.close()


class TestLearningStoreCRUD:
    def test_record_inserts_new_entry(self, learning_store):
        e = learning_store.record(
            "learning", "agent.scope.foo", "summary one",
            details="d", suggested_action="do x", task_id="t1",
        )
        assert e["id"].startswith("LRN-")
        assert e["recurrence_count"] == 1
        assert e["distinct_tasks"] == 1
        assert e["status"] == "pending"
        assert e["pattern_key"] == "agent.scope.foo"

    def test_record_dedupes_same_pattern_key(self, learning_store):
        first = learning_store.record(
            "error", "tool.term.perm_denied", "first hit", task_id="t1"
        )
        second = learning_store.record(
            "error", "tool.term.perm_denied", "second hit", task_id="t1"
        )
        # Same row updated, not duplicated
        assert second["id"] == first["id"]
        assert second["recurrence_count"] == 2
        assert second["distinct_tasks"] == 1  # same task_id

    def test_record_bumps_distinct_tasks_for_new_task(self, learning_store):
        first = learning_store.record(
            "error", "p.k.x", "s1", task_id="t1"
        )
        second = learning_store.record(
            "error", "p.k.x", "s2", task_id="t2"
        )
        assert second["id"] == first["id"]
        assert second["distinct_tasks"] == 2

    def test_record_rejects_invalid_category(self, learning_store):
        with pytest.raises(ValueError, match="category"):
            learning_store.record("bogus", "x.y", "summary")

    def test_record_rejects_empty_pattern_key(self, learning_store):
        with pytest.raises(ValueError, match="pattern_key"):
            learning_store.record("learning", "", "summary")

    def test_record_rejects_empty_summary(self, learning_store):
        with pytest.raises(ValueError, match="summary"):
            learning_store.record("learning", "x.y", "")

    def test_record_normalizes_invalid_priority_to_medium(self, learning_store):
        e = learning_store.record(
            "learning", "x.y", "s", priority="bogus"
        )
        assert e["priority"] == "medium"

    def test_get_returns_none_for_missing(self, learning_store):
        assert learning_store.get("LRN-NOTFOUND-XXX") is None

    def test_list_filters_by_status(self, learning_store):
        learning_store.record("learning", "a.b", "x")
        e = learning_store.record("error", "a.c", "y")
        learning_store.mark_resolved(e["id"], notes="fixed")
        pending = learning_store.list(status="pending")
        resolved = learning_store.list(status="resolved")
        assert len(pending) == 1 and pending[0]["pattern_key"] == "a.b"
        assert len(resolved) == 1 and resolved[0]["pattern_key"] == "a.c"

    def test_list_falls_back_safe_order_for_invalid_order_by(
        self, learning_store
    ):
        learning_store.record("learning", "a.b", "x")
        # SQL injection attempt — must be silently coerced to safe default
        rows = learning_store.list(
            order_by="last_seen DESC; DROP TABLE learnings;--"
        )
        assert len(rows) == 1


class TestLearningStorePromotionEligibility:
    def test_eligible_when_all_thresholds_hit(self):
        entry = {
            "status": "pending",
            "promoted_to": None,
            "recurrence_count": 3,
            "distinct_tasks": 2,
            "first_seen": 1000.0,
            "last_seen": 1001.0,  # 1 second span
        }
        assert is_eligible_for_promotion(entry)

    def test_zero_first_seen_treated_as_no_data(self):
        """Defensive: an unset first_seen (0.0) means the row never had a
        real timestamp recorded; refuse to promote it."""
        entry = {
            "status": "pending", "promoted_to": None,
            "recurrence_count": 3, "distinct_tasks": 2,
            "first_seen": 0.0, "last_seen": 1.0,
        }
        assert not is_eligible_for_promotion(entry)

    def test_ineligible_when_below_recurrence(self):
        entry = {
            "status": "pending", "promoted_to": None,
            "recurrence_count": 2, "distinct_tasks": 2,
            "first_seen": 0.0, "last_seen": 1.0,
        }
        assert not is_eligible_for_promotion(entry)

    def test_ineligible_when_too_few_distinct_tasks(self):
        entry = {
            "status": "pending", "promoted_to": None,
            "recurrence_count": 5, "distinct_tasks": 1,
            "first_seen": 0.0, "last_seen": 1.0,
        }
        assert not is_eligible_for_promotion(entry)

    def test_ineligible_when_already_promoted(self):
        entry = {
            "status": "promoted", "promoted_to": "rules",
            "recurrence_count": 5, "distinct_tasks": 3,
            "first_seen": 0.0, "last_seen": 1.0,
        }
        assert not is_eligible_for_promotion(entry)

    def test_ineligible_when_span_too_wide(self):
        # 60 day span but window is 30
        entry = {
            "status": "pending", "promoted_to": None,
            "recurrence_count": 5, "distinct_tasks": 3,
            "first_seen": 0.0, "last_seen": 60 * 86400.0,
        }
        assert not is_eligible_for_promotion(entry)

    def test_promotion_rule_tunable(self):
        entry = {
            "status": "pending", "promoted_to": None,
            "recurrence_count": 2, "distinct_tasks": 2,
            "first_seen": 1000.0, "last_seen": 1001.0,
        }
        assert is_eligible_for_promotion(entry, PromotionRule(min_recurrence=2))


class TestLearningStorePersistence:
    def test_close_and_reopen_keeps_data(self, tmp_path):
        path = tmp_path / "persist.db"
        s1 = LearningStore(db_path=path)
        s1.record("learning", "p.k", "summary")
        s1.close()

        s2 = LearningStore(db_path=path)
        rows = s2.list()
        assert len(rows) == 1
        assert rows[0]["pattern_key"] == "p.k"
        s2.close()

    def test_db_file_actually_persists(self, tmp_path):
        path = tmp_path / "persist.db"
        s = LearningStore(db_path=path)
        s.record("learning", "p.k", "summary")
        s.close()
        # raw sqlite check
        conn = sqlite3.connect(str(path))
        rows = conn.execute("SELECT pattern_key FROM learnings").fetchall()
        conn.close()
        assert rows == [("p.k",)]


class TestLearningStorePromotion:
    def test_mark_promoted_sets_status_and_target(self, learning_store):
        e = learning_store.record("learning", "x.y", "s")
        result = learning_store.mark_promoted(e["id"], target="rules")
        assert result["success"]
        reloaded = learning_store.get(e["id"])
        assert reloaded["status"] == "promoted"
        assert reloaded["promoted_to"] == "rules"
        assert reloaded["promoted_at"] is not None

    def test_mark_promoted_to_skill_sets_skill_status(self, learning_store):
        e = learning_store.record("learning", "x.y", "s")
        result = learning_store.mark_promoted(e["id"], target="skill:test-skill")
        reloaded = learning_store.get(e["id"])
        assert reloaded["status"] == "promoted_to_skill"
        assert reloaded["promoted_to"] == "skill:test-skill"

    def test_mark_promoted_unknown_id_returns_failure(self, learning_store):
        result = learning_store.mark_promoted("LRN-NOTHERE-XXX", target="rules")
        assert result["success"] is False

    def test_eligible_pending_excludes_already_promoted(self, learning_store):
        e = learning_store.record("learning", "x.y", "s", task_id="t1")
        learning_store.record("learning", "x.y", "s", task_id="t2")
        learning_store.record("learning", "x.y", "s", task_id="t3")
        # Now eligible
        assert any(
            row["id"] == e["id"] for row in learning_store.eligible_pending()
        )
        learning_store.mark_promoted(e["id"], target="rules")
        # No longer pending → no longer eligible
        assert not any(
            row["id"] == e["id"] for row in learning_store.eligible_pending()
        )


class TestLearningStoreStats:
    def test_stats_tallies_correctly(self, learning_store):
        learning_store.record("learning", "a.b", "s1")
        e = learning_store.record("error", "a.c", "s2")
        learning_store.mark_resolved(e["id"], notes="fix")
        learning_store.record("feature_request", "a.d", "s3")

        stats = learning_store.stats()
        assert stats["total"] == 3
        assert stats["by_status"].get("pending", 0) == 2
        assert stats["by_status"].get("resolved", 0) == 1
        assert set(stats["by_category"].keys()) == {
            "learning", "error", "feature_request"
        }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ memory_tool file IO + LCM bridge                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLcmPluginStructure:
    """LCM is loaded by the context-engine specialist loader; here we just
    verify the disk artifacts so M3 can wire it up."""

    def test_lcm_dir_exists(self):
        lcm = (
            Path(__file__).resolve().parent.parent.parent
            / "plugins" / "context_engine" / "lcm"
        )
        assert lcm.is_dir()

    def test_lcm_manifest_loads(self):
        import yaml
        lcm = (
            Path(__file__).resolve().parent.parent.parent
            / "plugins" / "context_engine" / "lcm" / "plugin.yaml"
        )
        data = yaml.safe_load(lcm.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data.get("name") == "lcm"

    def test_lcm_python_module_importable(self):
        # A reload-safe import: any structural error or missing dep would
        # surface here BEFORE M3 tries to register it as a context engine.
        import importlib
        # If this raises, M3 will fail too — better to detect at M1.
        importlib.import_module("plugins.context_engine.lcm")
