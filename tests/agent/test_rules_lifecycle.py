"""Tests for agent/rules_lifecycle.py.

Covered behaviour:
* HTML-comment metadata round-trip (parse + serialize)
* Backward compat: missing metadata defaults to stable+manual
* Tier classification (pinned vs regular) preserves order
* [NEW] marker shows for ≤7 days post-promotion only for LRN-source rules
* auto_archive_rules dual triggers (capacity + age) with all four safety rules
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent.rules_lifecycle import (
    ARCHIVE_REASON_AGE,
    ARCHIVE_REASON_CAPACITY,
    DEFAULT_NEW_MARKER_DAYS,
    RuleEntry,
    auto_archive_rules,
    parse_rule_entry,
    serialize_rule_entry,
    should_show_new_marker,
    split_by_tier,
)


# ---------------------------------------------------------------------------
# Parse / serialize
# ---------------------------------------------------------------------------


class TestParseRuleEntry:
    def test_plain_text_no_metadata_defaults_to_stable_manual(self):
        entry = parse_rule_entry("Always run scripts/run_tests.sh")
        assert entry.text == "Always run scripts/run_tests.sh"
        assert entry.pinned is False
        assert entry.source == "manual"
        assert entry.created is None
        assert entry.promoted_at is None
        assert entry.recurrence == 0
        assert entry.is_from_learning() is False
        assert entry.has_lifecycle_meta() is False

    def test_full_metadata_parsed_correctly(self):
        raw = (
            "Confirm scope before editing >5 files.\n"
            "<!-- hermes-meta: pinned=false; created=2026-04-28; "
            "source=LRN-20260428-003; promoted_at=2026-04-28; "
            "recurrence=4; pattern_key=agent.scope.unconfirmed -->"
        )
        entry = parse_rule_entry(raw)
        assert entry.text == "Confirm scope before editing >5 files."
        assert entry.pinned is False
        assert entry.created == date(2026, 4, 28)
        assert entry.source == "LRN-20260428-003"
        assert entry.promoted_at == date(2026, 4, 28)
        assert entry.recurrence == 4
        assert entry.pattern_key == "agent.scope.unconfirmed"
        assert entry.is_from_learning() is True
        assert entry.has_lifecycle_meta() is True

    def test_pinned_true(self):
        raw = "Don't add narrative comments.\n<!-- hermes-meta: pinned=true; source=manual -->"
        entry = parse_rule_entry(raw)
        assert entry.pinned is True

    def test_unknown_keys_preserved_in_extra(self):
        raw = "Some rule\n<!-- hermes-meta: pinned=false; source=manual; future_key=xyz -->"
        entry = parse_rule_entry(raw)
        assert entry.extra == {"future_key": "xyz"}

    def test_empty_input_returns_empty_entry(self):
        assert parse_rule_entry("").text == ""
        assert parse_rule_entry("   \n  ").text == ""

    def test_malformed_dates_default_to_none(self):
        raw = "Rule\n<!-- hermes-meta: created=not-a-date; promoted_at=2026 -->"
        entry = parse_rule_entry(raw)
        assert entry.created is None
        # 2026 alone is not a valid ISO date (needs full YYYY-MM-DD)
        assert entry.promoted_at is None


class TestSerializeRuleEntry:
    def test_minimal_entry_emits_pinned_and_source(self):
        entry = RuleEntry(text="Always X")
        out = serialize_rule_entry(entry)
        assert "Always X" in out
        assert "pinned=false" in out
        assert "source=manual" in out

    def test_round_trip_preserves_all_known_fields(self):
        original = RuleEntry(
            text="Confirm scope before editing >5 files.",
            pinned=False,
            created=date(2026, 4, 28),
            source="LRN-20260428-003",
            promoted_at=date(2026, 4, 28),
            recurrence=4,
            last_recurrence=date(2026, 4, 30),
            pattern_key="agent.scope.unconfirmed",
        )
        serialized = serialize_rule_entry(original)
        reparsed = parse_rule_entry(serialized)
        assert reparsed.text == original.text
        assert reparsed.pinned == original.pinned
        assert reparsed.created == original.created
        assert reparsed.source == original.source
        assert reparsed.promoted_at == original.promoted_at
        assert reparsed.recurrence == original.recurrence
        assert reparsed.last_recurrence == original.last_recurrence
        assert reparsed.pattern_key == original.pattern_key

    def test_empty_text_serializes_to_empty_string(self):
        assert serialize_rule_entry(RuleEntry(text="")) == ""

    def test_unknown_extra_keys_preserved_through_round_trip(self):
        entry = RuleEntry(text="Rule", extra={"future": "v"})
        reparsed = parse_rule_entry(serialize_rule_entry(entry))
        assert reparsed.extra.get("future") == "v"


# ---------------------------------------------------------------------------
# Tier split
# ---------------------------------------------------------------------------


class TestSplitByTier:
    def test_pinned_and_regular_separated_preserves_order(self):
        entries = [
            RuleEntry(text="A", pinned=False),
            RuleEntry(text="B", pinned=True),
            RuleEntry(text="C", pinned=False),
            RuleEntry(text="D", pinned=True),
        ]
        tiers = split_by_tier(entries)
        assert [e.text for e in tiers["pinned"]] == ["B", "D"]
        assert [e.text for e in tiers["regular"]] == ["A", "C"]

    def test_empty_text_entries_filtered_out(self):
        entries = [RuleEntry(text=""), RuleEntry(text="A"), RuleEntry(text="   ")]
        tiers = split_by_tier(entries)
        assert [e.text for e in tiers["regular"]] == ["A"]


# ---------------------------------------------------------------------------
# [NEW] marker
# ---------------------------------------------------------------------------


class TestShouldShowNewMarker:
    def test_lrn_within_7_days_shows_marker(self):
        entry = RuleEntry(
            text="X",
            source="LRN-20260428-003",
            promoted_at=date(2026, 4, 28),
        )
        # Day 0 → yes
        assert should_show_new_marker(entry, today=date(2026, 4, 28)) is True
        # Day 7 → still yes (boundary inclusive)
        assert should_show_new_marker(entry, today=date(2026, 5, 5)) is True

    def test_lrn_after_7_days_no_marker(self):
        entry = RuleEntry(
            text="X",
            source="LRN-20260428-003",
            promoted_at=date(2026, 4, 28),
        )
        assert should_show_new_marker(entry, today=date(2026, 5, 6)) is False

    def test_manual_rule_never_marked_even_if_recent(self):
        entry = RuleEntry(
            text="X",
            source="manual",
            promoted_at=date(2026, 4, 28),
        )
        assert should_show_new_marker(entry, today=date(2026, 4, 29)) is False

    def test_pinned_lrn_rule_no_marker(self):
        entry = RuleEntry(
            text="X",
            source="LRN-20260428-003",
            promoted_at=date(2026, 4, 28),
            pinned=True,
        )
        assert should_show_new_marker(entry, today=date(2026, 4, 29)) is False

    def test_no_promoted_at_no_marker(self):
        entry = RuleEntry(text="X", source="LRN-20260428-003")
        assert should_show_new_marker(entry, today=date(2026, 4, 29)) is False

    def test_window_zero_disables_marker(self):
        entry = RuleEntry(
            text="X",
            source="LRN-20260428-003",
            promoted_at=date(2026, 4, 28),
        )
        assert (
            should_show_new_marker(entry, today=date(2026, 4, 28), window_days=0)
            is False
        )

    def test_default_window_is_seven_days(self):
        # Sanity: explicit constant matches the function's default.
        assert DEFAULT_NEW_MARKER_DAYS == 7


# ---------------------------------------------------------------------------
# Auto-archive: capacity trigger (Trigger A)
# ---------------------------------------------------------------------------


def _mk(text: str, **kw) -> RuleEntry:
    return RuleEntry(text=text, **kw)


class TestAutoArchiveCapacity:
    def test_under_threshold_archives_nothing(self):
        entries = [_mk("rule a"), _mk("rule b")]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=date(2026, 4, 30),
        )
        assert decision.archived == []
        assert [e.text for e in decision.keep] == ["rule a", "rule b"]
        assert bool(decision) is False

    def test_over_threshold_archives_oldest_non_pinned(self):
        entries = [
            _mk("old rule " * 50, created=date(2026, 1, 1)),
            _mk("middle rule " * 50, created=date(2026, 2, 1)),
            _mk("new rule " * 50, created=date(2026, 4, 1)),
        ]
        # char_limit small enough that one of the three must be evicted.
        decision = auto_archive_rules(
            entries,
            char_limit=1500,
            capacity_threshold=0.50,
            today=date(2026, 4, 30),
            age_days=0,  # disable B so we isolate A
        )
        assert len(decision.archived) >= 1
        # Oldest evicted first.
        assert decision.archived[0].text.startswith("old rule")
        assert ARCHIVE_REASON_CAPACITY in decision.reasons

    def test_pinned_protected_even_if_oldest(self):
        entries = [
            _mk("oldest content " * 50, created=date(2026, 1, 1), pinned=True),
            _mk("newer content " * 50, created=date(2026, 4, 1)),
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=1500,
            capacity_threshold=0.50,
            today=date(2026, 4, 30),
            age_days=0,
        )
        # Pinned entry must NOT appear among archived; it survives in keep.
        assert all(not e.pinned for e in decision.archived)
        assert any(e.pinned for e in decision.keep)

    def test_capacity_threshold_zero_disables_trigger_a(self):
        entries = [
            _mk("rule " * 100, created=date(2026, 1, 1)),
            _mk("rule " * 100, created=date(2026, 2, 1)),
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=100,
            capacity_threshold=0.0,
            today=date(2026, 4, 30),
            age_days=0,
        )
        assert decision.archived == []


# ---------------------------------------------------------------------------
# Auto-archive: age-based trigger (Trigger B) + safety rules
# ---------------------------------------------------------------------------


class TestAutoArchiveAge:
    today = date(2026, 4, 30)

    def test_lrn_rule_older_than_age_days_with_no_recurrence_archived(self):
        entries = [
            _mk(
                "old auto-promoted",
                source="LRN-20260101-001",
                created=date(2026, 1, 1),
                promoted_at=date(2026, 1, 1),
                pattern_key="x.y.z",
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=90,
            recurrence_window_days=30,
        )
        assert len(decision.archived) == 1
        assert decision.reasons == [ARCHIVE_REASON_AGE]

    def test_manual_rule_never_archived_by_age(self):
        # Manual rule older than 90 days, but it's not LRN-sourced.
        entries = [
            _mk(
                "ancient manual rule",
                source="manual",
                created=date(2026, 1, 1),
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=90,
        )
        assert decision.archived == []

    def test_pinned_lrn_rule_protected_from_age(self):
        entries = [
            _mk(
                "pinned learning rule",
                source="LRN-20260101-001",
                created=date(2026, 1, 1),
                promoted_at=date(2026, 1, 1),
                pinned=True,
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=90,
        )
        assert decision.archived == []

    def test_recently_recurred_rule_protected(self):
        entries = [
            _mk(
                "still recurring",
                source="LRN-20260101-001",
                created=date(2026, 1, 1),
                promoted_at=date(2026, 1, 1),
                last_recurrence=date(2026, 4, 20),  # 10 days ago
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=90,
            recurrence_window_days=30,
        )
        assert decision.archived == []

    def test_recently_edited_rule_protected(self):
        entries = [
            _mk(
                "edited recently",
                source="LRN-20260101-001",
                created=date(2026, 1, 1),
                promoted_at=date(2026, 1, 1),
                last_edited=date(2026, 4, 20),
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=90,
            recurrence_window_days=30,
        )
        assert decision.archived == []

    def test_lrn_within_new_marker_window_protected(self):
        # Promoted 5 days ago — still in NEW phase, must not archive.
        entries = [
            _mk(
                "freshly promoted",
                source="LRN-20260425-001",
                created=date(2026, 4, 25),
                promoted_at=date(2026, 4, 25),
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=0,  # would archive immediately if not protected
            new_marker_days=7,
        )
        assert decision.archived == []

    def test_age_zero_skips_trigger_b(self):
        entries = [
            _mk(
                "would normally archive",
                source="LRN-20260101-001",
                created=date(2026, 1, 1),
                promoted_at=date(2026, 1, 1),
            )
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=0,
        )
        assert decision.archived == []


class TestAutoArchiveCombined:
    today = date(2026, 4, 30)

    def test_capacity_runs_before_age(self):
        # Mix of an oldest manual rule (eligible for A only) and a stale LRN
        # rule (eligible for B). Both should be archived for distinct reasons.
        entries = [
            _mk(
                "very old manual " * 50,
                source="manual",
                created=date(2026, 1, 1),
            ),
            _mk(
                "stale LRN",
                source="LRN-20260101-001",
                created=date(2026, 1, 1),
                promoted_at=date(2026, 1, 1),
            ),
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=1500,
            capacity_threshold=0.10,  # very tight → A fires
            today=self.today,
            age_days=90,
        )
        # Both archived — A and B for distinct entries.
        archived_sources = [e.source for e in decision.archived]
        assert "manual" in archived_sources
        assert "LRN-20260101-001" in archived_sources
        assert ARCHIVE_REASON_CAPACITY in decision.reasons
        assert ARCHIVE_REASON_AGE in decision.reasons

    def test_returns_keep_in_original_order(self):
        # Make a long set with one obvious target; survivors must keep order.
        entries = [
            _mk(f"rule {i}", source="manual", created=date(2026, 1, 1) + timedelta(days=i))
            for i in range(5)
        ]
        decision = auto_archive_rules(
            entries,
            char_limit=4000,
            today=self.today,
            age_days=0,
        )
        assert [e.text for e in decision.keep] == [f"rule {i}" for i in range(5)]
