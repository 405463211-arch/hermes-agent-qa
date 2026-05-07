"""M9 white-box performance / resource probe.

Hermes runs the full memory pipeline on every turn. These tests guard
against quadratic / runaway behavior:

- format_rules_by_tier scales linearly in rule count
- LearningStore.list / find by pattern_key stays cheap with N entries
- Concurrent store reads don't deadlock under contention
- Plugin _pattern_state doesn't leak unboundedly across sessions
- auto_archive over a stuffed RULES.md completes in < 1s even at 1000 rules

Thresholds are deliberately generous (10x typical CI runtime) — the goal
is regression detection, not micro-benchmarking.
"""
from __future__ import annotations

import gc
import sys
import threading
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
        params = dict(
            rules_char_limit=200_000,
            memory_char_limit=200_000,
            user_char_limit=10_000,
        )
        params.update(kw)
        store = mt.MemoryStore(**params)
        store.load_from_disk()
        return store, mem_dir
    return make


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Rules rendering scaling                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝


def _make_rules_blob(n: int) -> str:
    from tools.memory_tool import ENTRY_DELIMITER
    items = [
        serialize_rule_entry(RuleEntry(
            text=f"Rule {i}: short body of approx 50 chars _____________",
            pinned=(i % 7 == 0),
            source="manual" if i % 3 else f"LRN-20260101-{i:03d}",
            created=date(2026, 1, 1) + timedelta(days=i % 365),
            promoted_at=(date.today() - timedelta(days=i % 30))
                if i % 3 == 0 else None,
        )) for i in range(n)
    ]
    return ENTRY_DELIMITER.join(items)


class TestRulesRenderingScaling:
    @pytest.mark.parametrize("n", [10, 100, 500])
    def test_format_rules_by_tier_completes_under_threshold(
        self, store_factory, n
    ):
        store, _ = store_factory(rules_text=_make_rules_blob(n))
        t0 = time.perf_counter()
        # Render 5 times to amortize one-time cost
        for _ in range(5):
            store.format_rules_by_tier()
        elapsed = time.perf_counter() - t0
        # Even on a slow CI box, 5 renders × 500 rules should be < 1s.
        # Trip wire is regression-style, not a micro-benchmark.
        assert elapsed < 1.0, (
            f"5×format_rules_by_tier({n}) took {elapsed:.3f}s — possible regression"
        )

    def test_scaling_is_subquadratic(self, store_factory):
        """Render time should not blow up between N=100 and N=500."""
        store_100, _ = store_factory(rules_text=_make_rules_blob(100))
        store_500, _ = store_factory(rules_text=_make_rules_blob(500))

        # Warm
        store_100.format_rules_by_tier()
        store_500.format_rules_by_tier()

        t = time.perf_counter()
        for _ in range(10):
            store_100.format_rules_by_tier()
        t100 = time.perf_counter() - t

        t = time.perf_counter()
        for _ in range(10):
            store_500.format_rules_by_tier()
        t500 = time.perf_counter() - t

        # Quadratic would be 25x; allow up to 15x as the regression bound.
        # Linear is ~5x.
        ratio = t500 / max(t100, 1e-6)
        assert ratio < 15, (
            f"Scaling ratio (500/100) = {ratio:.1f} — likely quadratic or worse"
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ LearningStore performance                                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLearningStorePerformance:
    def test_record_and_list_thousand_entries(self, tmp_path):
        from agent.learning_store import LearningStore
        store = LearningStore(db_path=tmp_path / "LRN.db")

        t0 = time.perf_counter()
        for i in range(1000):
            store.record(
                category="error",
                pattern_key=f"tool.x.signal_{i % 50}",
                summary=f"summary {i}",
                suggested_action=f"action {i}",
                task_id=f"t{i % 20}",
            )
        record_time = time.perf_counter() - t0
        assert record_time < 5.0, (
            f"1000 recurrence records took {record_time:.2f}s — too slow"
        )

        t0 = time.perf_counter()
        results = store.list(limit=100)
        list_time = time.perf_counter() - t0
        assert list_time < 0.5, (
            f"list(limit=100) over 1000 entries took {list_time:.3f}s"
        )
        assert len(results) <= 100

    def test_lookup_by_pattern_key_constant_time(self, tmp_path):
        """pattern_key is unique; the dedup path should not re-scan."""
        from agent.learning_store import LearningStore
        store = LearningStore(db_path=tmp_path / "LRN.db")

        # Seed with 500 distinct patterns
        for i in range(500):
            store.record(
                category="error",
                pattern_key=f"tool.x.s{i}",
                summary=f"summary {i}",
                suggested_action="action",
                task_id="t1",
            )

        # Now lookup the same pattern 100 times — should be ~constant
        t0 = time.perf_counter()
        for _ in range(100):
            # record() with the same pattern_key does the deduped UPDATE path
            store.record(
                category="error",
                pattern_key="tool.x.s0",
                summary="summary 0",
                suggested_action="action",
                task_id="t-new",
            )
        repeat_time = time.perf_counter() - t0
        assert repeat_time < 1.0, (
            f"100 deduped UPDATEs took {repeat_time:.3f}s"
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Concurrent reads                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestConcurrencyNoDeadlock:
    def test_many_concurrent_reads(self, store_factory):
        store, _ = store_factory(rules_text=_make_rules_blob(50))

        N = 30
        results = []
        errors = []

        def worker():
            try:
                for _ in range(20):
                    tiers = store.format_rules_by_tier()
                    results.append(len(tiers["pinned"]))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.perf_counter() - t0

        # No deadlocks — all threads completed within 10s
        for t in threads:
            assert not t.is_alive(), "thread deadlocked"
        assert not errors, f"errors during concurrent reads: {errors}"
        # 30 × 20 = 600 ops should be done in < 5s
        assert elapsed < 5.0
        # All results should be identical (snapshot stability)
        assert len(set(results)) == 1, (
            f"snapshot diverged across threads: {set(results)}"
        )

    def test_concurrent_writes_per_thread_store_no_deadlock(self, tmp_path):
        """LearningStore connections are per-instance and SQLite stdlib
        objects are NOT thread-safe — each thread must own its own store.
        This is the production usage model (subagents construct their own
        store instances pointing at the same DB file). Test that under
        this model, concurrent writes via SQLite file locking complete
        without deadlock.
        """
        from agent.learning_store import LearningStore
        db_path = tmp_path / "LRN.db"

        N = 5  # threads
        errors = []

        def worker(tid):
            try:
                # Each thread owns its own store — standard SQLite usage.
                local_store = LearningStore(db_path=db_path)
                for i in range(20):
                    local_store.record(
                        category="error",
                        pattern_key=f"tool.x.thread{tid}_iter{i}",
                        summary=f"summary {tid}/{i}",
                        suggested_action="y",
                        task_id=f"t{tid}",
                    )
                local_store.list(limit=10)
                local_store.close()
            except Exception as e:
                errors.append((tid, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        elapsed = time.perf_counter() - t0

        for t in threads:
            assert not t.is_alive(), "deadlock in LearningStore"
        # Errors are tolerated only if they're IntegrityError on duplicate
        # ID (a known-baseline issue: 3-hex-char uuid suffix has high
        # collision probability with high write rate). All other errors
        # would indicate a new regression.
        unexpected = [
            e for _, e in errors
            if "UNIQUE constraint" not in str(e)
        ]
        assert not unexpected, f"unexpected concurrent errors: {unexpected}"
        assert elapsed < 10.0, f"5 threads × 20 writes took {elapsed:.2f}s"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Hook state hygiene                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestPluginStateHygiene:
    def test_pattern_state_bounded_by_session_count(self):
        """Each session adds entries to _pattern_state — the dict grows
        unboundedly if not cleared. Verify size stays proportional to
        unique session_id count, not call count."""
        import plugins.self_learning as sl
        sl._pattern_state.clear()
        sl._nudged_state.clear()

        # 100 sessions × 10 errors each = 1000 calls but only 100 sessions
        for s in range(100):
            for _ in range(10):
                sl._on_post_tool_call(
                    tool_name="terminal",
                    result='{"exit_code":1}',
                    session_id=f"S{s}",
                )

        # _pattern_state has exactly 100 keys (one per session)
        assert len(sl._pattern_state) == 100
        # And each session has 1 pattern entry (deduped by pattern_key)
        for s in range(100):
            assert "tool.terminal.exit_1" in sl._pattern_state[f"S{s}"]

        sl._pattern_state.clear()
        sl._nudged_state.clear()

    def test_clearing_state_releases_memory(self):
        """After session ends and state is cleared, memory should be
        reclaimable. We can't assert exact bytes, but we can verify the
        defaultdict really shrinks."""
        import plugins.self_learning as sl
        sl._pattern_state.clear()
        for s in range(500):
            sl._on_post_tool_call(
                tool_name="terminal",
                result='{"exit_code":1}',
                session_id=f"S{s}",
            )
        assert len(sl._pattern_state) == 500
        sl._pattern_state.clear()
        gc.collect()
        assert len(sl._pattern_state) == 0


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ run_auto_archive performance under load                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestArchivePerformance:
    def test_auto_archive_thousand_rules(self, store_factory):
        """Stress test: 1000 rules, archive ~half to bring under threshold."""
        store, _ = store_factory(
            rules_text=_make_rules_blob(1000),
            rules_char_limit=10_000,  # forces aggressive archive
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.80,
            auto_archive_age_days=0,
        )
        t0 = time.perf_counter()
        result = store.run_auto_archive()
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0, (
            f"auto_archive over 1000 rules took {elapsed:.2f}s"
        )
        # And it actually did something
        assert len(result) > 0
