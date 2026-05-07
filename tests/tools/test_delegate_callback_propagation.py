"""Regression test: subagent worker threads must inherit the parent
thread's CLI callbacks (sudo + approval).

The bug this guards against
---------------------------

Before the fix, the parent agent thread registered an approval callback
via `tools.terminal_tool.set_approval_callback`, which stores it in a
`threading.local`.  Subagents are then run on a `ThreadPoolExecutor`
worker thread (`tools.delegate_tool._run_single_child` →
`_timeout_executor.submit(child.run_conversation, ...)`).  That worker
thread had no callback registered, so any dangerous-command approval the
subagent triggered fell through to the legacy fallback in
`tools.approval.prompt_dangerous_approval`, which spawned a
`threading.Thread(target=lambda: input(...), daemon=True)`.  That daemon
thread blocked on `<stdin>` indefinitely, holding its BufferedReader
lock — fighting `prompt_toolkit` for keystrokes during the run, and at
interpreter shutdown crashing the user's terminal with::

    Fatal Python error: _enter_buffered_busy: could not acquire lock
    for <_io.BufferedReader name='<stdin>'> at interpreter shutdown,
    possibly due to daemon threads

The fix snapshots the parent thread's callbacks once and restores them
on the worker thread before any tool call runs.  This test pins that
behaviour: when the parent thread has a callback set, the worker thread
that runs the subagent's body must see the same callback.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from tools.terminal_tool import (
    apply_thread_callbacks,
    clear_thread_callbacks,
    get_thread_callbacks,
    set_approval_callback,
    set_sudo_password_callback,
)


def test_get_thread_callbacks_returns_currently_registered_callbacks():
    """The snapshot helper must surface whatever this thread registered."""
    clear_thread_callbacks()
    try:
        approval_cb = lambda *a, **kw: "deny"  # noqa: E731
        sudo_cb = lambda *a, **kw: ""  # noqa: E731
        set_approval_callback(approval_cb)
        set_sudo_password_callback(sudo_cb)

        snap = get_thread_callbacks()
        assert snap["approval"] is approval_cb
        assert snap["sudo_password"] is sudo_cb
    finally:
        clear_thread_callbacks()


def test_apply_thread_callbacks_propagates_into_worker_thread():
    """Snapshot from parent thread + apply in worker = same callback object."""
    clear_thread_callbacks()
    try:
        sentinel = object()

        def parent_approval_cb(command, description, *, allow_permanent=True):
            return sentinel

        set_approval_callback(parent_approval_cb)
        snapshot = get_thread_callbacks()
        assert snapshot["approval"] is parent_approval_cb

        observed = {}

        def worker():
            # Worker thread starts with NO callback (threading.local
            # never propagates across threads on its own).
            observed["before_apply"] = get_thread_callbacks()["approval"]
            apply_thread_callbacks(snapshot)
            observed["after_apply"] = get_thread_callbacks()["approval"]
            clear_thread_callbacks()
            observed["after_clear"] = get_thread_callbacks()["approval"]

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "worker hung"

        assert observed["before_apply"] is None, (
            "threading.local unexpectedly propagated across threads — "
            "test environment is incorrect"
        )
        assert observed["after_apply"] is parent_approval_cb
        assert observed["after_clear"] is None
    finally:
        clear_thread_callbacks()


def test_apply_thread_callbacks_is_safe_with_none_or_empty():
    """Defensive: None / {} snapshots must not crash and must not clobber."""
    clear_thread_callbacks()
    try:
        existing_cb = lambda *a, **kw: "deny"  # noqa: E731
        set_approval_callback(existing_cb)

        apply_thread_callbacks(None)
        apply_thread_callbacks({})
        apply_thread_callbacks({"approval": None, "sudo_password": None})

        # Existing callback must still be intact.
        assert get_thread_callbacks()["approval"] is existing_cb
    finally:
        clear_thread_callbacks()


def test_thread_pool_executor_propagation_pattern():
    """End-to-end: the same pattern delegate_tool uses must work.

    Mimics the snapshot-in-parent / apply-in-worker dance that
    `_run_single_child` and the ThreadPoolExecutor submit performs.
    """
    clear_thread_callbacks()
    try:
        approvals_seen = []

        def approval_cb(command, description, *, allow_permanent=True):
            approvals_seen.append((threading.current_thread().name, command))
            return "once"

        set_approval_callback(approval_cb)
        snapshot = get_thread_callbacks()

        def worker_body():
            apply_thread_callbacks(snapshot)
            cb = get_thread_callbacks()["approval"]
            assert cb is not None, "callback was not propagated into worker"
            return cb("rm -rf /tmp/foo", "recursive delete")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker_body) for _ in range(4)]
            results = [f.result(timeout=5) for f in futures]

        assert results == ["once", "once", "once", "once"]
        assert len(approvals_seen) == 4
        # All four ran on worker threads — never the main thread.
        worker_thread_names = {name for name, _ in approvals_seen}
        assert threading.current_thread().name not in worker_thread_names
    finally:
        clear_thread_callbacks()


def test_no_daemon_thread_leaked_when_callback_registered():
    """Smoke test: the call path used by subagents must not leak threads.

    This is the proximate guard against the `_enter_buffered_busy`
    Fatal Error.  When a callback IS registered, prompt_dangerous_approval
    must invoke it directly and NOT spawn a stdin-reading daemon thread.
    """
    from tools.approval import prompt_dangerous_approval

    clear_thread_callbacks()
    try:
        thread_count_before = threading.active_count()

        def cb(command, description, *, allow_permanent=True):
            return "once"

        result = prompt_dangerous_approval(
            "rm -rf /tmp/foo",
            "recursive delete",
            timeout_seconds=5,
            approval_callback=cb,
        )
        assert result == "once"

        # The callback path must not start any background thread.
        # (Allow a tiny race window for unrelated test infra threads.)
        thread_count_after = threading.active_count()
        assert thread_count_after <= thread_count_before, (
            f"prompt_dangerous_approval leaked threads: "
            f"{thread_count_before} -> {thread_count_after}"
        )
    finally:
        clear_thread_callbacks()
