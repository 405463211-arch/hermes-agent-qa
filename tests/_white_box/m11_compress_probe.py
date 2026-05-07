"""M11 white-box compress-window behavior probe.

Context compression is the ONLY legal mid-session window where the system
prompt may legally rebuild (per AGENTS.md). This module locks the contract:

  Before compress: snapshot frozen at session start
  Inside compress: _invalidate_system_prompt() called, memory reloaded
  After compress: new snapshot is fresh (mid-session writes now visible)
                  AND tier order survives (pinned → regular → memory → user)
                  AND self-consistent within the new session
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import RuleEntry, serialize_rule_entry


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    import tools.memory_tool as mt
    counter = {"n": 0}

    def make(rules_text=None, memory_text=None, user_text=None, **kw):
        counter["n"] += 1
        mem_dir = tmp_path / f"mem-{counter['n']}"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (mem_dir / "RULES.md").write_text(rules_text, encoding="utf-8")
        if memory_text is not None:
            (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
        if user_text is not None:
            (mem_dir / "USER.md").write_text(user_text, encoding="utf-8")
        monkeypatch.setattr(mt, "get_memory_dir", lambda d=mem_dir: d)
        params = dict(rules_char_limit=10_000, memory_char_limit=10_000, user_char_limit=10_000)
        params.update(kw)
        store = mt.MemoryStore(**params)
        store.load_from_disk()
        return store, mem_dir
    return make


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Snapshot freeze + reload behavior                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestSnapshotLifecycle:
    def test_snapshot_frozen_after_load(self, store_factory):
        """Initial load captures a snapshot; mutating live entries does NOT
        affect the snapshot returned by format_for_system_prompt."""
        rule = serialize_rule_entry(RuleEntry(text="initial"))
        store, _ = store_factory(rules_text=rule)
        first = store.format_for_system_prompt("rules")
        assert "initial" in first

        # Add to live entries (simulating mid-session memory tool calls)
        store.rules_entries.append(
            serialize_rule_entry(RuleEntry(text="mid-session add"))
        )
        second = store.format_for_system_prompt("rules")
        assert second == first  # snapshot still frozen
        assert "mid-session add" not in second

    def test_load_from_disk_refreshes_snapshot(self, store_factory):
        """This is the compress-window contract: after load_from_disk, the
        snapshot reflects current disk state — the ONE allowed
        invalidation path."""
        rule = serialize_rule_entry(RuleEntry(text="initial"))
        store, mem_dir = store_factory(rules_text=rule)
        before = store.format_for_system_prompt("rules")
        assert "initial" in before

        # Simulate disk-side mutation (e.g. another writer or this session
        # writing through the lock-protected save path)
        from tools.memory_tool import ENTRY_DELIMITER
        new_blob = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(text="initial")),
            serialize_rule_entry(RuleEntry(text="added on disk")),
        ])
        (mem_dir / "RULES.md").write_text(new_blob, encoding="utf-8")

        # Re-load (compress's _invalidate_system_prompt path)
        store.load_from_disk()
        after = store.format_for_system_prompt("rules")
        assert "added on disk" in after
        assert after != before  # snapshot did refresh

    def test_load_from_disk_resets_session_counter(self, store_factory):
        """A fresh compress-window resets the archive counter — the
        new session shouldn't inherit "5 entries auto-archived" from
        the previous one."""
        store, _ = store_factory()
        store._archived_this_session = 7
        store.load_from_disk()
        assert store._archived_this_session == 0


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Tier order invariant survives compress                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestTierOrderAfterCompress:
    def test_tiers_correctly_split_after_reload(self, store_factory):
        """Pinned and regular rules must remain in their respective tiers
        after a load_from_disk (compress) cycle."""
        from tools.memory_tool import ENTRY_DELIMITER
        items = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(text="P1", pinned=True)),
            serialize_rule_entry(RuleEntry(text="R1", pinned=False)),
            serialize_rule_entry(RuleEntry(text="P2", pinned=True)),
        ])
        store, _ = store_factory(rules_text=items)

        # Simulate compress
        store.load_from_disk()
        tiers = store.format_rules_by_tier()
        assert "P1" in tiers["pinned"] and "P2" in tiers["pinned"]
        assert "R1" in tiers["regular"]
        assert "P1" not in tiers["regular"]
        assert "R1" not in tiers["pinned"]

    def test_new_marker_drops_when_promoted_at_is_old(self, store_factory):
        """[NEW] window is computed against today's date — rules with
        promoted_at older than the trial window must NOT show the marker
        even on a fresh load_from_disk."""
        old = date.today() - timedelta(days=30)
        rule_old = serialize_rule_entry(RuleEntry(
            text="aged",
            source="LRN-20260301-OLD",
            promoted_at=old,
        ))
        store, _ = store_factory(rules_text=rule_old, trial_new_marker_days=7)
        tiers = store.format_rules_by_tier()
        assert "[NEW" not in tiers["regular"]

    def test_new_marker_present_within_trial_window(self, store_factory):
        """Symmetric check: with promoted_at = today, marker must be present."""
        rule = serialize_rule_entry(RuleEntry(
            text="freshly promoted",
            source="LRN-20260501-NEW",
            promoted_at=date.today(),
        ))
        store, _ = store_factory(rules_text=rule, trial_new_marker_days=7)
        tiers = store.format_rules_by_tier()
        assert "[NEW" in tiers["regular"]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Compress idempotency                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCompressIdempotency:
    def test_repeated_load_from_disk_same_result(self, store_factory):
        """Calling load_from_disk twice in a row must be idempotent —
        no extra dedupe, no growth, no shrinkage."""
        from tools.memory_tool import ENTRY_DELIMITER
        items = ENTRY_DELIMITER.join([
            serialize_rule_entry(RuleEntry(text=f"r{i}")) for i in range(10)
        ])
        store, _ = store_factory(rules_text=items)

        first = store.format_for_system_prompt("rules")
        n_entries_first = len(store.rules_entries)

        store.load_from_disk()
        store.load_from_disk()
        store.load_from_disk()

        second = store.format_for_system_prompt("rules")
        n_entries_second = len(store.rules_entries)

        assert first == second
        assert n_entries_first == n_entries_second == 10

    def test_load_dedupes_duplicate_disk_entries(self, store_factory):
        """If the on-disk file somehow contains duplicate entries (e.g.
        due to an unflushed concurrent write merge), load_from_disk
        should dedupe — ``list(dict.fromkeys(...))``."""
        from tools.memory_tool import ENTRY_DELIMITER
        same = serialize_rule_entry(RuleEntry(text="duplicate"))
        items = ENTRY_DELIMITER.join([same, same, same])
        store, _ = store_factory(rules_text=items)
        assert len(store.rules_entries) == 1


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Mid-session writes become visible only after compress                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestWriteVisibility:
    def test_add_visible_in_snapshot_only_after_reload(self, store_factory):
        """Production flow: tool calls add a rule mid-session. The rule
        is persisted to disk, but format_for_system_prompt still returns
        the OLD snapshot. Only after compress (load_from_disk) is the
        new entry visible. This is the prefix-cache contract."""
        store, _ = store_factory()

        # Mid-session add
        result = store.add("rules", "added during session")
        assert result.get("success")
        # Disk now has it; live entries do too
        assert any("added during session" in r for r in store.rules_entries)

        # But the SNAPSHOT (used for system prompt) does not yet.
        # format_for_system_prompt returns None when the snapshot is empty
        # (no entries at load time) — that's the prefix-cache contract.
        block_before = store.format_for_system_prompt("rules")
        assert block_before is None or "added during session" not in block_before

        # After compress / reload, snapshot includes it
        store.load_from_disk()
        block_after = store.format_for_system_prompt("rules")
        assert "added during session" in block_after


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Static check: compress flow in run_agent.py                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCompressFlowStaticAnalysis:
    """run_agent.py's compression flow must call:
       1. _invalidate_system_prompt() (which reloads memory)
       2. _build_system_prompt() (which rebuilds from fresh snapshot)
    in that order. Static check locks the contract."""

    def _src(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        return (repo_root / "run_agent.py").read_text(encoding="utf-8")

    def test_invalidate_then_rebuild_in_compress_path(self):
        src = self._src()
        # Find the compress block
        idx_compress = src.find("compressed = self.context_compressor.compress")
        assert idx_compress > 0
        # Within ~2k chars after compress, both calls must appear
        # in the right order
        window = src[idx_compress : idx_compress + 2000]
        i_inval = window.find("_invalidate_system_prompt")
        i_rebuild = window.find("_build_system_prompt")
        assert i_inval > 0, "compress flow missing _invalidate_system_prompt"
        assert i_rebuild > i_inval, (
            "compress flow must call _invalidate BEFORE _build_system_prompt"
        )

    def test_invalidate_resets_cache_and_reloads_memory(self):
        src = self._src()
        # _invalidate_system_prompt body must:
        #   - set _cached_system_prompt = None
        #   - call memory_store.load_from_disk()
        idx = src.find("def _invalidate_system_prompt")
        assert idx > 0
        body = src[idx : idx + 600]
        assert "_cached_system_prompt = None" in body
        assert "load_from_disk()" in body, (
            "_invalidate_system_prompt must reload memory — that's the "
            "ONLY mid-session write-visibility path"
        )
