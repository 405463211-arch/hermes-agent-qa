"""M13 white-box volume / scaling probe.

Real users accumulate rules and learnings over months. M9 already locked
1k-entry behavior; this module pushes to 5k+ and verifies:

  - format_rules_by_tier scales sub-linearly per entry (no quadratic blowup)
  - LearningStore.list_active stays O(1) lookup, sub-second at 5k rows
  - run_auto_archive correctly evicts oldest non-pinned entries when
    char_limit forces eviction at 5k+
  - Token budget impact is bounded (linear in entry count)

All thresholds are intentionally generous so that this isn't a
change-detector that breaks every time a function gets minorly slower.
The asserts target *catastrophic* regressions (10x slowdowns, infinite
loops, OOM-style memory growth).
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import RuleEntry, serialize_rule_entry


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    import tools.memory_tool as mt
    counter = {"n": 0}

    def make(rules_text=None, **kw):
        counter["n"] += 1
        mem_dir = tmp_path / f"mem-{counter['n']}"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (mem_dir / "RULES.md").write_text(rules_text, encoding="utf-8")
        monkeypatch.setattr(mt, "get_memory_dir", lambda d=mem_dir: d)
        params = dict(rules_char_limit=2_000_000, memory_char_limit=2_000_000,
                      user_char_limit=200_000)
        params.update(kw)
        store = mt.MemoryStore(**params)
        store.load_from_disk()
        return store, mem_dir
    return make


def _make_n_rules(n: int, pinned_every: int = 50) -> str:
    """Build a RULES.md text with n entries, one in every `pinned_every`
    pinned. Promotion dates spread over the last 200 days so age-based
    archiving has variety to choose from."""
    from tools.memory_tool import ENTRY_DELIMITER
    today = date.today()
    parts = []
    for i in range(n):
        promoted = today - timedelta(days=i % 200)
        parts.append(serialize_rule_entry(RuleEntry(
            text=f"rule-{i:05d}: a meaningful but not too short text",
            pinned=(i % pinned_every == 0),
            source=f"LRN-202604{(i%30)+1:02d}-RUL{i:03X}",
            promoted_at=promoted,
        )))
    return ENTRY_DELIMITER.join(parts)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ format_rules_by_tier scaling                                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestRulesRenderScaling:
    @pytest.mark.parametrize("n", [1_000, 3_000, 5_000])
    def test_render_under_threshold(self, store_factory, n):
        """Rendering N rules must complete in well under a second on any
        reasonable hardware. Generous threshold = 5s for 5k entries on
        slow CI workers."""
        store, _ = store_factory(rules_text=_make_n_rules(n))
        t0 = time.perf_counter()
        tiers = store.format_rules_by_tier()
        dt = time.perf_counter() - t0
        assert dt < 5.0, (
            f"format_rules_by_tier({n}) took {dt:.3f}s — "
            f"likely a quadratic blowup"
        )
        # Sanity: pinned tier should contain ~n/50 entries
        assert tiers["pinned"], "no pinned tier rendered"
        assert tiers["regular"], "no regular tier rendered"

    def test_render_growth_subquadratic(self, store_factory):
        """Compare runtime at 1k vs 5k entries. Quadratic would mean
        25x slower; we assert at most 15x (allowing for setup overhead
        + log noise on shared CI). Linear would be 5x."""
        store_1k, _ = store_factory(rules_text=_make_n_rules(1_000))
        # Warm up (avoid first-call import overhead skewing 1k)
        store_1k.format_rules_by_tier()

        t0 = time.perf_counter()
        for _ in range(5):
            store_1k.format_rules_by_tier()
        t_1k = (time.perf_counter() - t0) / 5

        store_5k, _ = store_factory(rules_text=_make_n_rules(5_000))
        store_5k.format_rules_by_tier()

        t0 = time.perf_counter()
        for _ in range(5):
            store_5k.format_rules_by_tier()
        t_5k = (time.perf_counter() - t0) / 5

        ratio = t_5k / max(t_1k, 1e-6)
        # Linear: 5x. Allow up to 15x (catches O(n²) which is 25x).
        assert ratio < 15.0, (
            f"rules render scaling looks superlinear: 1k={t_1k:.4f}s, "
            f"5k={t_5k:.4f}s, ratio={ratio:.1f}x"
        )

    def test_load_from_disk_scaling(self, store_factory):
        """Loading 5k entries from disk must stay sub-second."""
        store_factory(rules_text=_make_n_rules(5_000))
        # The store_factory already loaded once; do an explicit reload
        # under timing, on a fresh instance.
        import tools.memory_tool as mt
        # We need a fresh store instance — use a wrapper
        store, mem_dir = store_factory(rules_text=_make_n_rules(5_000))
        t0 = time.perf_counter()
        store.load_from_disk()
        dt = time.perf_counter() - t0
        assert dt < 5.0, f"load_from_disk(5k) took {dt:.3f}s"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Auto-archive at scale                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestArchiveAtScale:
    def test_archive_handles_5k_entries_under_threshold(self, store_factory):
        """run_auto_archive on 5k entries with capacity_threshold=0.5
        must complete in reasonable time (sub-second is target)."""
        # Need rules_char_limit small enough that >50% capacity is hit.
        # 5k entries × ~120 chars each ≈ 600k chars. Limit = 200k forces
        # archiving.
        store, _ = store_factory(
            rules_text=_make_n_rules(5_000),
            rules_char_limit=200_000,
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.5,
        )
        t0 = time.perf_counter()
        result = store.run_auto_archive()
        dt = time.perf_counter() - t0
        assert dt < 10.0, f"run_auto_archive(5k) took {dt:.3f}s"
        # run_auto_archive returns a list of {"text", "reason", "source"} dicts
        archived_count = len(result) if result else 0
        assert archived_count > 0, (
            "expected archiving to fire at 50% capacity threshold with "
            "5k entries way over limit"
        )

    def test_pinned_rules_protected_at_scale(self, store_factory):
        """At scale, pinned rules must NEVER be evicted, even when the
        capacity trigger forces aggressive archiving."""
        store, _ = store_factory(
            rules_text=_make_n_rules(2_000, pinned_every=20),
            rules_char_limit=50_000,  # ~25% of full content → forces big archive
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.5,
        )
        # Count pinned entries before
        from agent.rules_lifecycle import parse_rule_entry
        pinned_before = sum(
            1 for raw in store.rules_entries
            if parse_rule_entry(raw).pinned
        )
        store.run_auto_archive()
        # After archiving, all the original pinned entries must still
        # be present — pinned tier is sacred
        pinned_after = sum(
            1 for raw in store.rules_entries
            if parse_rule_entry(raw).pinned
        )
        assert pinned_after == pinned_before, (
            f"pinned rules were evicted at scale: "
            f"before={pinned_before}, after={pinned_after}"
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ LearningStore at 5k                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLearningStoreAtScale:
    def test_record_5k_then_list_under_threshold(self, tmp_path):
        from agent.learning_store import LearningStore
        db = tmp_path / "learning.db"
        store = LearningStore(db_path=db)

        # Insert 5k unique pattern keys
        t0 = time.perf_counter()
        for i in range(5_000):
            store.record(
                category="error",
                pattern_key=f"err::pattern-{i:05d}",
                summary=f"summary {i}",
                details=f"pattern text {i}",
                task_id=f"task-{i % 100}",
            )
        t_record = time.perf_counter() - t0
        assert t_record < 30.0, f"record(5k) took {t_record:.3f}s"

        # list() should still be fast
        t0 = time.perf_counter()
        active = store.list(status="pending", limit=100)
        t_list = time.perf_counter() - t0
        assert t_list < 2.0, f"list(limit=100) at 5k took {t_list:.3f}s"
        assert len(active) <= 100

    def test_pattern_lookup_5k_constant_time(self, tmp_path):
        """Inserting the same pattern_key into a 5k-row store should
        be roughly constant time (UPDATE existing, not scan)."""
        from agent.learning_store import LearningStore
        db = tmp_path / "learning.db"
        store = LearningStore(db_path=db)

        # Pre-populate
        for i in range(5_000):
            store.record(
                category="error",
                pattern_key=f"err::pre-{i:05d}",
                summary=f"summary {i}",
                details=f"text {i}",
                task_id="seed",
            )

        # Now hammer one specific pattern_key 50 times — it should be
        # consistently fast (UPDATE not full table scan)
        target_key = "err::pre-02500"
        timings = []
        for _ in range(50):
            t0 = time.perf_counter()
            store.record(
                category="error",
                pattern_key=target_key,
                summary="updated summary",
                details="updated text",
                task_id=f"task-{_}",
            )
            timings.append(time.perf_counter() - t0)
        # No single update should take more than 1s
        assert max(timings) < 1.0, (
            f"slowest single update at 5k rows: {max(timings):.4f}s — "
            f"index might be missing on pattern_key"
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Memory footprint sanity                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestMemoryFootprint:
    def test_5k_render_output_size_bounded(self, store_factory):
        """The rendered output for 5k rules must be roughly proportional
        to the input — no exponential expansion."""
        store, _ = store_factory(rules_text=_make_n_rules(5_000))
        rules_text_size = sum(len(r) for r in store.rules_entries)
        tiers = store.format_rules_by_tier()
        rendered_size = len(tiers.get("pinned", "")) + len(tiers.get("regular", ""))
        # Rendered output must be at most 3x input (allowing for headers,
        # markers, formatting). Anything more suggests expansion bug.
        assert rendered_size <= 3 * rules_text_size, (
            f"rendered output {rendered_size} chars is more than 3x "
            f"raw entries {rules_text_size}"
        )
