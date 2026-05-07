"""Tests for agent/learning_store.py — SQLite-backed learning ledger."""

from __future__ import annotations

import time

import pytest

from agent.learning_store import (
    LearningStore,
    PromotionRule,
    is_eligible_for_promotion,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Fresh in-process store backed by a tmp SQLite file."""
    s = LearningStore(db_path=tmp_path / "learning_store.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# is_eligible_for_promotion (pure function)
# ---------------------------------------------------------------------------


class TestEligibility:
    def _entry(self, **overrides):
        now = time.time()
        base = {
            "status": "pending",
            "promoted_to": None,
            "recurrence_count": 5,
            "distinct_tasks": 3,
            "first_seen": now - 5 * 86400,
            "last_seen": now,
        }
        base.update(overrides)
        return base

    def test_default_eligible(self):
        assert is_eligible_for_promotion(self._entry()) is True

    def test_below_recurrence_threshold(self):
        assert is_eligible_for_promotion(self._entry(recurrence_count=2)) is False

    def test_below_distinct_tasks_threshold(self):
        assert is_eligible_for_promotion(self._entry(distinct_tasks=1)) is False

    def test_outside_time_window_blocks(self):
        # Span > 30 days → ineligible (too old/spread out, not a recent burst).
        now = time.time()
        entry = self._entry(first_seen=now - 60 * 86400, last_seen=now)
        assert is_eligible_for_promotion(entry) is False

    def test_already_promoted_blocks(self):
        assert is_eligible_for_promotion(self._entry(status="promoted")) is False
        assert is_eligible_for_promotion(self._entry(promoted_to="rules")) is False

    def test_resolved_blocks(self):
        assert is_eligible_for_promotion(self._entry(status="resolved")) is False

    def test_custom_promotion_rule(self):
        # With min_recurrence=2 the borderline entry now qualifies.
        rule = PromotionRule(min_recurrence=2, min_distinct_tasks=1)
        assert (
            is_eligible_for_promotion(
                self._entry(recurrence_count=2, distinct_tasks=1), rule
            )
            is True
        )


# ---------------------------------------------------------------------------
# LearningStore.record — insert + dedupe
# ---------------------------------------------------------------------------


class TestRecord:
    def test_inserts_new_entry_with_recurrence_one(self, store):
        result = store.record(
            "learning",
            pattern_key="agent.scope.unconfirmed",
            summary="Confirm scope before bulk edit",
        )
        assert result["recurrence_count"] == 1
        assert result["distinct_tasks"] == 1
        assert result["status"] == "pending"
        assert result["category"] == "learning"
        assert result["id"].startswith("LRN-")
        assert result["eligible_for_promotion"] is False

    def test_same_pattern_key_increments_recurrence(self, store):
        first = store.record(
            "learning",
            pattern_key="agent.scope.unconfirmed",
            summary="Confirm scope before bulk edit",
        )
        second = store.record(
            "learning",
            pattern_key="agent.scope.unconfirmed",
            summary="Same pattern, different wording",
        )
        assert first["id"] == second["id"]
        assert second["recurrence_count"] == 2

    def test_distinct_task_id_bumps_distinct_tasks(self, store):
        store.record(
            "learning",
            pattern_key="x.y.z",
            summary="thing",
            task_id="task-A",
        )
        result = store.record(
            "learning",
            pattern_key="x.y.z",
            summary="thing",
            task_id="task-B",
        )
        assert result["distinct_tasks"] == 2

    def test_same_task_id_does_not_bump_distinct(self, store):
        store.record(
            "learning",
            pattern_key="x.y.z",
            summary="thing",
            task_id="task-A",
        )
        result = store.record(
            "learning",
            pattern_key="x.y.z",
            summary="thing",
            task_id="task-A",
        )
        assert result["distinct_tasks"] == 1

    def test_id_prefix_matches_category(self, store):
        learning = store.record(
            "learning", pattern_key="a", summary="s",
        )
        error = store.record(
            "error", pattern_key="b", summary="s",
        )
        feat = store.record(
            "feature_request", pattern_key="c", summary="s",
        )
        assert learning["id"].startswith("LRN-")
        assert error["id"].startswith("ERR-")
        assert feat["id"].startswith("FEAT-")

    def test_invalid_category_raises(self, store):
        with pytest.raises(ValueError):
            store.record(
                "nonsense", pattern_key="x", summary="s",
            )

    def test_empty_pattern_key_raises(self, store):
        with pytest.raises(ValueError):
            store.record("learning", pattern_key="", summary="s")

    def test_empty_summary_raises(self, store):
        with pytest.raises(ValueError):
            store.record("learning", pattern_key="x", summary="")

    def test_eligible_flag_flips_after_threshold(self, store):
        # Default rule: 3 recurrences + 2 distinct tasks within 30 days.
        store.record("learning", pattern_key="k", summary="s", task_id="t1")
        store.record("learning", pattern_key="k", summary="s", task_id="t2")
        result = store.record("learning", pattern_key="k", summary="s", task_id="t3")
        assert result["recurrence_count"] == 3
        assert result["distinct_tasks"] == 3
        assert result["eligible_for_promotion"] is True


# ---------------------------------------------------------------------------
# list / get / mark_promoted / mark_resolved / stats
# ---------------------------------------------------------------------------


class TestQueryOps:
    def test_get_returns_existing_row(self, store):
        rec = store.record("learning", pattern_key="k", summary="s")
        assert store.get(rec["id"])["id"] == rec["id"]

    def test_get_returns_none_for_missing(self, store):
        assert store.get("LRN-99999999-XXX") is None

    def test_list_filters_by_status(self, store):
        store.record("learning", pattern_key="a", summary="s")
        rec = store.record("learning", pattern_key="b", summary="s")
        store.mark_resolved(rec["id"])

        pending = store.list(status="pending")
        resolved = store.list(status="resolved")
        assert len(pending) == 1
        assert len(resolved) == 1
        assert pending[0]["pattern_key"] == "a"

    def test_list_filters_by_category(self, store):
        store.record("learning", pattern_key="l", summary="s")
        store.record("error", pattern_key="e", summary="s")
        learnings = store.list(category="learning")
        errors = store.list(category="error")
        assert len(learnings) == 1
        assert len(errors) == 1

    def test_list_safe_default_order_by(self, store):
        store.record("learning", pattern_key="a", summary="s")
        store.record("learning", pattern_key="b", summary="s")
        # Bogus order_by silently falls back to default — must not raise SQL err.
        rows = store.list(order_by="; DROP TABLE learnings; --")
        assert len(rows) == 2

    def test_mark_promoted_to_rules(self, store):
        rec = store.record("learning", pattern_key="k", summary="s")
        out = store.mark_promoted(rec["id"], target="rules")
        assert out["success"] is True
        assert store.get(rec["id"])["status"] == "promoted"
        assert store.get(rec["id"])["promoted_to"] == "rules"

    def test_mark_promoted_to_skill_uses_skill_status(self, store):
        rec = store.record("learning", pattern_key="k", summary="s")
        store.mark_promoted(rec["id"], target="skill:my-skill")
        assert store.get(rec["id"])["status"] == "promoted_to_skill"

    def test_mark_promoted_unknown_id(self, store):
        out = store.mark_promoted("LRN-99999999-XXX", target="rules")
        assert out["success"] is False

    def test_mark_resolved_with_notes(self, store):
        rec = store.record("learning", pattern_key="k", summary="s")
        store.mark_resolved(rec["id"], notes="fixed in commit abc")
        row = store.get(rec["id"])
        assert row["status"] == "resolved"
        assert row["resolution_notes"] == "fixed in commit abc"

    def test_stats(self, store):
        store.record("learning", pattern_key="a", summary="s")
        store.record("error", pattern_key="b", summary="s")
        rec = store.record("feature_request", pattern_key="c", summary="s")
        store.mark_resolved(rec["id"])

        stats = store.stats()
        assert stats["total"] == 3
        assert stats["by_category"] == {
            "learning": 1, "error": 1, "feature_request": 1
        }
        assert stats["by_status"]["pending"] == 2
        assert stats["by_status"]["resolved"] == 1
        assert stats["recent_24h"] == 3

    def test_eligible_pending_filters_correctly(self, store):
        # One eligible (3+ recurrences, 2+ tasks) and one not.
        for tid in ("t1", "t2", "t3"):
            store.record("learning", pattern_key="hot", summary="s", task_id=tid)
        store.record("learning", pattern_key="cold", summary="s", task_id="t1")

        eligible = store.eligible_pending()
        assert len(eligible) == 1
        assert eligible[0]["pattern_key"] == "hot"


# ---------------------------------------------------------------------------
# Persistence across reopen
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_data_survives_close_and_reopen(self, tmp_path):
        path = tmp_path / "ls.db"
        s1 = LearningStore(db_path=path)
        rec = s1.record("learning", pattern_key="k", summary="s")
        s1.close()

        s2 = LearningStore(db_path=path)
        try:
            assert s2.get(rec["id"])["id"] == rec["id"]
        finally:
            s2.close()
