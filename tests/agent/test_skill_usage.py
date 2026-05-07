"""Tests for agent/skill_usage.py — usage tracking and relevance scoring."""

import json
import time
from pathlib import Path

import pytest

from agent import skill_usage
from agent.skill_usage import (
    MAX_TRACKED_SKILLS,
    USAGE_FILENAME,
    get_usage_stats,
    record_skill_load,
    reset_cache,
    score,
)


@pytest.fixture(autouse=True)
def _isolated_skills_dir(tmp_path, monkeypatch):
    """Point get_skills_dir() at a temp dir for every test in this file."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("agent.skill_usage.get_skills_dir", lambda: skills_dir)
    reset_cache()
    yield skills_dir


# ---------------------------------------------------------------------------
# record / get
# ---------------------------------------------------------------------------

class TestRecordAndGet:
    def test_first_record_creates_file(self, _isolated_skills_dir):
        record_skill_load("git-pr")
        assert (_isolated_skills_dir / USAGE_FILENAME).exists()
        stats = get_usage_stats()
        assert "git-pr" in stats
        assert stats["git-pr"]["load_count"] == 1
        assert stats["git-pr"]["last_loaded_at"] > 0

    def test_repeated_record_increments_count(self):
        record_skill_load("debugging")
        record_skill_load("debugging")
        record_skill_load("debugging")
        stats = get_usage_stats()
        assert stats["debugging"]["load_count"] == 3

    def test_independent_skills_tracked_separately(self):
        record_skill_load("a")
        record_skill_load("b")
        record_skill_load("b")
        stats = get_usage_stats()
        assert stats["a"]["load_count"] == 1
        assert stats["b"]["load_count"] == 2

    def test_empty_name_is_noop(self):
        record_skill_load("")
        record_skill_load(None)  # type: ignore[arg-type]
        assert get_usage_stats() == {}

    def test_persistence_across_cache_reset(self, _isolated_skills_dir):
        record_skill_load("persisted")
        reset_cache()
        stats = get_usage_stats()
        assert "persisted" in stats
        assert stats["persisted"]["load_count"] == 1


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

class TestScore:
    def test_unknown_skill_scores_zero(self):
        assert score("unknown") == 0.0

    def test_recent_load_outscores_old_one(self):
        # Manually insert two entries with different ages
        path = _isolated_skills_dir_path()
        now = time.time()
        path.write_text(json.dumps({
            "fresh": {"load_count": 1, "last_loaded_at": now},
            "stale": {"load_count": 1, "last_loaded_at": now - 30 * 86400},
        }))
        reset_cache()
        s_fresh = score("fresh", now=now)
        s_stale = score("stale", now=now)
        assert s_fresh > s_stale > 0

    def test_high_count_outscores_low_count_at_same_age(self):
        path = _isolated_skills_dir_path()
        now = time.time()
        path.write_text(json.dumps({
            "rarely": {"load_count": 1, "last_loaded_at": now},
            "often": {"load_count": 50, "last_loaded_at": now},
        }))
        reset_cache()
        assert score("often", now=now) > score("rarely", now=now)

    def test_zero_count_yields_zero_score(self):
        path = _isolated_skills_dir_path()
        path.write_text(json.dumps({
            "ghost": {"load_count": 0, "last_loaded_at": time.time()},
        }))
        reset_cache()
        assert score("ghost") == 0.0


def _isolated_skills_dir_path() -> Path:
    """Helper to access the autouse fixture's skills dir from non-fixture tests."""
    return skill_usage.get_skills_dir() / USAGE_FILENAME


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------

class TestEviction:
    def test_caps_at_max_tracked_skills(self, monkeypatch):
        # Lower the cap for the test so we don't have to record 500 entries
        monkeypatch.setattr("agent.skill_usage.MAX_TRACKED_SKILLS", 5)
        # Record 8 distinct skills with monotonically increasing timestamps
        # (record_skill_load uses time.time()) — the 3 oldest must get evicted.
        for i in range(8):
            record_skill_load(f"skill-{i}")
            # tiny sleep to ensure last_loaded_at differs
            time.sleep(0.001)
        stats = get_usage_stats()
        assert len(stats) == 5
        # The five most recent (indices 3..7) survive
        for i in range(3, 8):
            assert f"skill-{i}" in stats
        for i in range(0, 3):
            assert f"skill-{i}" not in stats


# ---------------------------------------------------------------------------
# Robustness — corrupt / missing files must never raise into the caller
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_corrupt_file_resets_cleanly(self, _isolated_skills_dir):
        (_isolated_skills_dir / USAGE_FILENAME).write_text("{not valid json")
        reset_cache()
        # Read returns empty dict; record_skill_load still works
        assert get_usage_stats() == {}
        record_skill_load("recovered")
        assert "recovered" in get_usage_stats()

    def test_record_swallows_oserror(self, monkeypatch, _isolated_skills_dir):
        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("agent.skill_usage._save_locked", boom)
        # Must NOT raise into the agent
        record_skill_load("smoke")  # no exception
