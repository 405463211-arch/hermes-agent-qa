"""Unit tests for :mod:`agent.hook_hinter`.

The hinter is on a hot path (called from every successful tool call) and
its job is to nudge the model toward proposing shell hooks. Two things
absolutely cannot regress:

1. **Thresholds + de-dup.** Counting wrong = either spam (every call) or
   silence (never fires), both kill the feature.
2. **Scaffold lines.** When a fingerprint maps to a bundled starter
   template, the hint MUST say ``--from-template <name>``, not the
   generic ``--event/--matcher``. The whole point is to give the model
   a copy-pasteable command.

Tests are pure-Python, no I/O, no LLM — the hint-text format and the
fingerprint→template mapping are both deterministic.
"""

from __future__ import annotations

import threading

import pytest

from agent.hook_hinter import HookHinter, apply_pending_hints_to_tool_results


# ── threshold / dedup / cap ───────────────────────────────────────────────


def test_below_threshold_yields_no_hint():
    h = HookHinter(threshold=3, enabled=True)
    h.record("write_file", {"path": "a.py"})
    h.record("write_file", {"path": "b.py"})
    assert h.drain_pending() == []


def test_crossing_threshold_emits_once():
    h = HookHinter(threshold=3, enabled=True)
    for _ in range(5):
        h.record("write_file", {"path": "a.py"})
    pending = h.drain_pending()
    assert len(pending) == 1
    assert "write_file" in pending[0].text


def test_already_emitted_fingerprint_does_not_re_emit():
    h = HookHinter(threshold=3, enabled=True)
    for _ in range(3):
        h.record("write_file", {"path": "a.py"})
    _ = h.drain_pending()  # consume
    for _ in range(10):
        h.record("write_file", {"path": "b.py"})  # same .py fingerprint
    assert h.drain_pending() == []


def test_max_hints_per_session_cap():
    h = HookHinter(threshold=3, max_hints_per_session=2, enabled=True)
    # Cross threshold on three DIFFERENT fingerprints.
    for _ in range(3):
        h.record("write_file", {"path": "a.py"})
    for _ in range(3):
        h.record("write_file", {"path": "a.yaml"})
    for _ in range(3):
        h.record("terminal", {"command": "git push origin main --force"})
    pending = h.drain_pending()
    assert len(pending) == 2, "cap of 2 must be enforced"


def test_disabled_hinter_records_nothing():
    h = HookHinter(threshold=2, enabled=False)
    for _ in range(10):
        h.record("write_file", {"path": "a.py"})
    assert h.drain_pending() == []


# ── observation / navigation tools are filtered ───────────────────────────


@pytest.mark.parametrize("tool", ["read_file", "search_files"])
def test_observation_tools_never_trigger_hint(tool):
    h = HookHinter(threshold=2, enabled=True)
    for _ in range(20):
        h.record(tool, {"path": "x.py"})
    assert h.drain_pending() == [], (
        f"{tool!r} is observation-only; spam there is the agent doing its "
        "job, not a hook signal."
    )


# ── scaffold-line composition ─────────────────────────────────────────────


@pytest.mark.parametrize("path,expected_template", [
    ("a.py",   "auto-format"),
    ("a.yaml", "auto-format"),
    ("a.yml",  "auto-format"),
])
def test_write_file_hint_includes_from_template(path, expected_template):
    h = HookHinter(threshold=3, enabled=True)
    for _ in range(3):
        h.record("write_file", {"path": path})
    pending = h.drain_pending()
    assert len(pending) == 1
    assert f"--from-template {expected_template}" in pending[0].text


def test_write_file_unknown_suffix_falls_back_to_generic():
    h = HookHinter(threshold=3, enabled=True)
    for _ in range(3):
        h.record("write_file", {"path": "a.ts"})
    pending = h.drain_pending()
    assert "--from-template" not in pending[0].text
    assert "--event post_tool_call" in pending[0].text
    assert "--matcher" in pending[0].text


def test_terminal_black_hint_recommends_auto_format():
    h = HookHinter(threshold=3, enabled=True)
    for _ in range(3):
        h.record("terminal", {"command": "black src/foo.py"})
    pending = h.drain_pending()
    assert "--from-template auto-format" in pending[0].text


def test_terminal_force_push_hint_recommends_block_template():
    h = HookHinter(threshold=3, enabled=True)
    for _ in range(3):
        h.record("terminal", {"command": "git push origin main --force"})
    pending = h.drain_pending()
    assert "--from-template block-force-push-main" in pending[0].text


# ── drain semantics ───────────────────────────────────────────────────────


def test_drain_clears_queue():
    h = HookHinter(threshold=2, enabled=True)
    for _ in range(2):
        h.record("write_file", {"path": "a.py"})
    first = h.drain_pending()
    second = h.drain_pending()
    assert len(first) == 1
    assert second == [], "second drain must yield nothing"


# ── injection helper ──────────────────────────────────────────────────────


def test_apply_pending_appends_to_last_tool_message():
    h = HookHinter(threshold=2, enabled=True)
    for _ in range(2):
        h.record("write_file", {"path": "a.py"})

    messages = [
        {"role": "assistant", "content": "..."},
        {"role": "tool", "content": "result-1"},
        {"role": "tool", "content": "result-2"},
    ]
    injected = apply_pending_hints_to_tool_results(h, messages, num_tool_msgs=2)
    assert injected == 1
    assert "result-2" in messages[2]["content"]
    assert "[hermes hook hint]" in messages[2]["content"]
    # First tool message untouched
    assert messages[1]["content"] == "result-1"


def test_apply_pending_no_tool_message_drops_hint():
    """If there's no tool result in the trailing slice to attach to, the
    helper must short-circuit (return 0) and not touch the queue."""
    h = HookHinter(threshold=2, enabled=True)
    for _ in range(2):
        h.record("write_file", {"path": "a.py"})

    messages = [{"role": "assistant", "content": "..."}]
    injected = apply_pending_hints_to_tool_results(h, messages, num_tool_msgs=0)
    assert injected == 0


# ── thread safety smoke ──────────────────────────────────────────────────


def test_concurrent_record_no_crash_no_double_emit():
    """Many threads recording the same fingerprint must emit exactly one
    hint between them — never two, never zero."""
    h = HookHinter(threshold=5, max_hints_per_session=10, enabled=True)

    def worker():
        for _ in range(20):
            h.record("write_file", {"path": "shared.py"})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pending = h.drain_pending()
    assert len(pending) == 1
