"""M12 white-box cross-process concurrency probe.

Real scenario: CLI session and gateway running for the same profile, both
holding their own MemoryStore. They write to the same RULES.md / MEMORY.md.
This probe locks the contract:

  - fcntl-based file lock serializes cross-process writes
  - No data loss under N parallel processes each adding M rules
  - No file corruption (every persisted entry parses correctly)
  - Atomic rename guarantees readers never see a half-written file

We use multiprocessing.Process (NOT threading) to truly exercise the
inter-process file lock — threading would only test the in-process
threading.Lock layer.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from agent.rules_lifecycle import RuleEntry, parse_rule_entry, serialize_rule_entry


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Worker functions (must be top-level for multiprocessing.spawn)           ║
# ╚══════════════════════════════════════════════════════════════════════════╝


def _worker_add_rules(mem_dir_str: str, worker_id: int, n_rules: int,
                      result_q: mp.Queue):
    """Each worker: instantiate its own MemoryStore, add N rules, return
    success/failure. This simulates a separate hermes process touching
    the same memory directory."""
    import os
    os.environ["HERMES_HOME"] = str(Path(mem_dir_str).parent)

    # Re-import in the worker process so module-level state is fresh
    import tools.memory_tool as mt
    mem_dir = Path(mem_dir_str)
    # Patch the get_memory_dir function in this process
    mt.get_memory_dir = lambda: mem_dir

    try:
        store = mt.MemoryStore(rules_char_limit=200_000)
        store.load_from_disk()
        added = []
        for i in range(n_rules):
            content = f"rule from worker {worker_id} idx {i}"
            result = store.add("rules", content)
            if result.get("success"):
                added.append(content)
            else:
                result_q.put(("fail", worker_id, i, result.get("error")))
                return
        result_q.put(("ok", worker_id, len(added), None))
    except Exception as e:
        result_q.put(("exc", worker_id, str(e), None))


def _worker_read_rules(mem_dir_str: str, worker_id: int, n_reads: int,
                       result_q: mp.Queue):
    """Reader worker: continuously load_from_disk and verify the file
    parses correctly. If any read sees corrupted state, report it."""
    import os
    os.environ["HERMES_HOME"] = str(Path(mem_dir_str).parent)
    import tools.memory_tool as mt
    mem_dir = Path(mem_dir_str)
    mt.get_memory_dir = lambda: mem_dir

    try:
        store = mt.MemoryStore(rules_char_limit=200_000)
        corruptions = 0
        for _ in range(n_reads):
            store.load_from_disk()
            for raw in store.rules_entries:
                try:
                    parse_rule_entry(raw)
                except Exception:
                    corruptions += 1
            time.sleep(0.001)
        result_q.put(("ok", worker_id, corruptions, None))
    except Exception as e:
        result_q.put(("exc", worker_id, str(e), None))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Pure cross-process write contention                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCrossProcessWrites:
    def test_n_processes_no_data_loss(self, tmp_path):
        """N=4 processes each add M=5 rules. Final RULES.md must contain
        all 4×5 = 20 entries, none lost to race condition."""
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

        ctx = mp.get_context("spawn")
        result_q = ctx.Queue()

        N = 4
        M = 5
        procs = [
            ctx.Process(
                target=_worker_add_rules,
                args=(str(mem_dir), wid, M, result_q),
            )
            for wid in range(N)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        results = []
        while not result_q.empty():
            results.append(result_q.get_nowait())

        # All workers must complete with status "ok"
        statuses = [r[0] for r in results]
        assert statuses.count("ok") == N, (
            f"some workers failed: {results}"
        )

        # Final RULES.md must contain ALL N×M entries
        rules_path = mem_dir / "RULES.md"
        assert rules_path.exists()
        text = rules_path.read_text(encoding="utf-8")
        for wid in range(N):
            for i in range(M):
                marker = f"rule from worker {wid} idx {i}"
                assert marker in text, (
                    f"lost: '{marker}' missing from final RULES.md"
                )

    def test_concurrent_readers_see_no_corruption(self, tmp_path):
        """While a writer is hammering RULES.md, multiple readers
        continuously parse it. Every entry every reader sees must be
        well-formed (no half-written rows)."""
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

        # Pre-seed with a few entries so the readers always have data
        seed = "\n§\n".join(
            serialize_rule_entry(RuleEntry(text=f"seed {i}"))
            for i in range(3)
        )
        (mem_dir / "RULES.md").write_text(seed, encoding="utf-8")

        ctx = mp.get_context("spawn")
        result_q = ctx.Queue()

        # 1 writer (10 adds) + 3 readers (50 reads each)
        writer = ctx.Process(
            target=_worker_add_rules,
            args=(str(mem_dir), 999, 10, result_q),
        )
        readers = [
            ctx.Process(
                target=_worker_read_rules,
                args=(str(mem_dir), wid, 50, result_q),
            )
            for wid in range(3)
        ]

        writer.start()
        for r in readers:
            r.start()
        writer.join(timeout=30)
        for r in readers:
            r.join(timeout=30)

        results = []
        while not result_q.empty():
            results.append(result_q.get_nowait())

        # Find reader results — each reports its corruption count
        reader_results = [r for r in results if r[1] in (0, 1, 2)]
        for status, wid, corruptions, _ in reader_results:
            assert status == "ok", f"reader {wid} crashed: {corruptions}"
            assert corruptions == 0, (
                f"reader {wid} saw {corruptions} malformed entries — "
                f"atomic rename / fcntl lock isn't holding"
            )

    def test_lock_file_created(self, tmp_path):
        """The fcntl lock uses a sidecar .lock file so the data file
        itself can still be atomically replaced via os.replace()."""
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        proc = ctx.Process(
            target=_worker_add_rules,
            args=(str(mem_dir), 0, 1, q),
        )
        proc.start()
        proc.join(timeout=15)

        # After at least one write, the .lock sidecar must exist
        lock = mem_dir / "RULES.md.lock"
        assert lock.exists(), (
            "RULES.md.lock not created — fcntl path may be broken or "
            "fallback (no-op) path was hit"
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Atomic rename — readers never see partial writes                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestAtomicRename:
    def test_write_uses_atomic_rename(self):
        """Static check: _write_file must use os.replace (or equivalent
        atomic rename). String-replacement via open(path, 'w') is NOT
        atomic — readers can see the empty file mid-write."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        src = (repo_root / "tools" / "memory_tool.py").read_text()
        # Find _write_file
        idx = src.find("def _write_file")
        assert idx > 0
        body = src[idx : idx + 2000]
        assert "os.replace" in body or "tmp" in body, (
            "_write_file should use atomic rename (os.replace) for "
            "cross-process read safety"
        )
