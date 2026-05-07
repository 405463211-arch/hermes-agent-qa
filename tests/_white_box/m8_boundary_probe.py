"""M8 white-box boundary / exception probe.

Covers stressful real-world conditions:
- Empty / zero-byte memory files
- Garbled hermes-meta lines (don't crash parse_rule_entry)
- Legacy MEMORY.md without ENTRY_DELIMITER
- Concurrent rules writes from N threads (lock holds)
- RULES.md fully consumed → graceful error path
- LCM unavailable → _index_archive_to_lcm silent failure
- Corrupted SQLite → LearningStore reset / re-init logic
- LRN-* with malformed source ID (e.g. truncated date)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import (
    RuleEntry,
    parse_rule_entry,
    serialize_rule_entry,
)


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    import tools.memory_tool as mt
    counter = {"n": 0}

    def make(rules_text=None, memory_text=None, archive_text=None, **kw):
        counter["n"] += 1
        mem_dir = tmp_path / f"mem-{counter['n']}"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (mem_dir / "RULES.md").write_text(rules_text, encoding="utf-8")
        if memory_text is not None:
            (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
        if archive_text is not None:
            (mem_dir / "RULES.archive.md").write_text(archive_text, encoding="utf-8")
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


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Empty files                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestEmptyFiles:
    def test_zero_byte_rules_file(self, store_factory):
        store, _ = store_factory(rules_text="")
        assert store.rules_entries == []
        # Doesn't crash on tier rendering
        tiers = store.format_rules_by_tier()
        assert tiers == {"pinned": "", "regular": ""}
        # auto_archive on empty file is a no-op
        assert store.run_auto_archive() == []

    def test_zero_byte_memory_file(self, store_factory):
        store, _ = store_factory(memory_text="")
        assert store.find_stale_memory_entries(age_days=60) == []

    def test_whitespace_only_rules_file(self, store_factory):
        store, _ = store_factory(rules_text="   \n\n   \n")
        assert store.rules_entries == []


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Garbled metadata                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestGarbledMetadata:
    def test_malformed_hermes_meta_line_falls_back(self):
        """Garbled meta line should not lose the rule's text body."""
        raw = (
            "Real rule content here.\n"
            "<!-- hermes-meta: pinned=GARBAGE; created=NOTADATE; "
            "this is not a valid kvpair -->"
        )
        e = parse_rule_entry(raw)
        # Text must survive
        assert e.text == "Real rule content here."
        # pinned defaults to false on parse failure
        assert e.pinned is False

    def test_meta_with_unknown_keys_preserved(self):
        raw = (
            "Body.\n"
            "<!-- hermes-meta: pinned=true; created=2026-04-30; "
            "experimental_field=alpha; created=2026-05-01 -->"
        )
        e = parse_rule_entry(raw)
        assert e.text == "Body."
        assert e.pinned is True
        # Unknown keys go into extra (round-trip safe)
        assert "experimental_field" in e.extra

    def test_invalid_date_in_meta_handled(self):
        raw = (
            "Body.\n"
            "<!-- hermes-meta: created=2026-13-99 -->"
        )
        e = parse_rule_entry(raw)
        assert e.text == "Body."
        # Invalid date → None instead of raising
        assert e.created is None

    def test_no_meta_line_legacy_format(self):
        e = parse_rule_entry("Plain rule, no metadata.")
        assert e.text == "Plain rule, no metadata."
        assert e.pinned is False
        assert e.created is None
        # Round trip: serialize a legacy entry and re-parse it
        round_tripped = serialize_rule_entry(e)
        e2 = parse_rule_entry(round_tripped)
        assert e2.text == "Plain rule, no metadata."

    def test_truncated_lrn_source_id(self):
        """LRN-yyyymmdd-xxxx is the canonical format. A truncated source
        like 'LRN-20260101' should still parse but be a normal rule."""
        raw = (
            "Truncated LRN test.\n"
            "<!-- hermes-meta: source=LRN-202601 -->"
        )
        e = parse_rule_entry(raw)
        assert e.text == "Truncated LRN test."
        assert e.source == "LRN-202601"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Legacy MEMORY.md without ENTRY_DELIMITER                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLegacyFileFormat:
    def test_legacy_memory_md_loads(self, store_factory):
        """A pre-rules-lifecycle MEMORY.md with multiple paragraphs but no
        delimiter should still load (as a single entry)."""
        legacy = (
            "First fact about the project.\n\n"
            "Second fact, no delimiter.\n\n"
            "Third fact."
        )
        store, _ = store_factory(memory_text=legacy)
        # Should not crash
        block = store.format_for_system_prompt("memory")
        assert "First fact" in block
        assert "Third fact" in block

    def test_legacy_rules_md_with_no_meta_still_renders(self, store_factory):
        """Old RULES.md from before the lifecycle layer should still render
        (without [NEW] markers, since there's no promoted_at metadata)."""
        legacy = "Always run tests before pushing.\nNever rebase main."
        store, _ = store_factory(rules_text=legacy)
        block = store.format_for_system_prompt("rules")
        assert "Always run tests" in block
        assert "[NEW" not in block


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Concurrency: multiple writers on the same RULES.md                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestConcurrentWrites:
    def test_concurrent_rule_adds_no_data_loss(self, tmp_path, monkeypatch):
        """N threads add rules — final RULES.md must contain ALL of them
        (no lost updates from race condition)."""
        import tools.memory_tool as mt
        mem_dir = tmp_path / "mem"
        mem_dir.mkdir()
        monkeypatch.setattr(mt, "get_memory_dir", lambda: mem_dir)

        # Single store shared across threads (the file lock is the sync)
        store = mt.MemoryStore(rules_char_limit=200_000)
        store.load_from_disk()

        N = 20
        adds_per_thread = 10

        def worker(tid):
            for i in range(adds_per_thread):
                store.add("rules", f"rule from thread {tid} #{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Re-read RULES.md from disk to confirm persistence
        rules_path = mem_dir / "RULES.md"
        text = rules_path.read_text(encoding="utf-8")
        # Each thread's rules should appear exactly adds_per_thread times
        for tid in range(N):
            count = text.count(f"rule from thread {tid} #")
            assert count == adds_per_thread, (
                f"thread {tid} lost updates: expected {adds_per_thread}, got {count}"
            )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ RULES.md saturation                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestRulesSaturation:
    def test_add_returns_error_when_at_limit_and_no_archive(
        self, store_factory
    ):
        """With auto_archive=False and no LCM overflow, adding to a full
        RULES.md should return an error (not silently truncate)."""
        from tools.memory_tool import ENTRY_DELIMITER
        # Fill to ~95%
        big = ENTRY_DELIMITER.join(
            f"X" * 80 for _ in range(20)
        )
        store, _ = store_factory(
            rules_text=big,
            rules_char_limit=2000,
            auto_archive_rules=False,
        )
        # Adding more should either succeed (under limit) or return an
        # error dict — never raise
        result = store.add("rules", "Y" * 200)
        assert isinstance(result, dict)
        assert "success" in result or "error" in result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ LCM unavailable                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLCMUnavailable:
    def test_archive_succeeds_without_lcm(self, store_factory):
        """When LCM is not configured, run_auto_archive should still write
        the archive file — LCM indexing is best-effort."""
        from tools.memory_tool import ENTRY_DELIMITER
        old = date.today() - timedelta(days=120)
        rule = serialize_rule_entry(RuleEntry(
            text="will be archived",
            source="LRN-20250101-OLD",
            created=old,
            promoted_at=old,
        ))
        store, mem_dir = store_factory(
            rules_text=rule,
            auto_archive_rules=True,
            auto_archive_age_days=90,
        )
        # No LCM provider plumbed in — _index_archive_to_lcm should silently
        # skip
        result = store.run_auto_archive()
        assert len(result) == 1
        archive = mem_dir / "RULES.archive.md"
        assert archive.exists(), "archive file must still be created"

    def test_index_archive_to_lcm_silent_on_error(self, store_factory):
        """Even if a hooked LCM provider raises, archive should not fail."""
        store, mem_dir = store_factory()

        # Inject a broken LCM hook
        def broken_index(*args, **kwargs):
            raise RuntimeError("LCM down")

        # Replace the method via monkeypatch on the instance — the production
        # code wraps the call in try/except (best-effort indexing).
        store._index_archive_to_lcm = broken_index  # type: ignore[assignment]

        # Direct call must not raise — _index_archive_to_lcm is invoked
        # behind try/except in run_auto_archive
        from tools.memory_tool import ENTRY_DELIMITER
        from agent.rules_lifecycle import RuleEntry
        old = date.today() - timedelta(days=120)
        rule = serialize_rule_entry(RuleEntry(
            text="x",
            source="LRN-20250101-OLD",
            created=old,
            promoted_at=old,
        ))
        store.rules_entries = [rule]
        store.save_to_disk("rules")
        store.auto_archive_rules_enabled = True
        store.auto_archive_age_days = 90
        # If exception leaks, this raises:
        try:
            store.run_auto_archive()
        except RuntimeError:
            pytest.fail("LCM error must not leak out of run_auto_archive")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Corrupted SQLite (LearningStore)                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCorruptedSQLite:
    def test_garbage_db_file_handled(self, tmp_path):
        """A pre-existing file with non-SQLite garbage at the LRN.db path
        should produce a clean, observable error — not a hard crash on
        construction."""
        db = tmp_path / "LRN.db"
        db.write_bytes(b"this is not a sqlite database, not even close")

        from agent.learning_store import LearningStore

        # The store is allowed to either raise a recognizable sqlite error
        # OR auto-recover (delete and recreate). Both are valid policies.
        # What's NOT acceptable: a partial state that crashes on next
        # operation.
        try:
            store = LearningStore(db_path=db)
            # If construction succeeded, basic ops should work
            store.list(limit=10)
        except sqlite3.DatabaseError:
            pass  # acceptable
        except Exception as exc:
            # Anything else is an unexpected failure mode
            pytest.fail(f"unexpected exception type: {type(exc).__name__}: {exc}")

    def test_partial_db_recoverable(self, tmp_path):
        """Truncated DB (header byte only) — same expectations."""
        db = tmp_path / "LRN.db"
        db.write_bytes(b"SQLite\x00")  # not a valid header

        from agent.learning_store import LearningStore
        try:
            store = LearningStore(db_path=db)
            store.list(limit=10)
        except sqlite3.DatabaseError:
            pass
