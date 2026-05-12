"""In-session hook-candidate detector.

Tracks the tool calls the agent makes during a single conversation, and
when the same fingerprint is observed ``threshold`` times, queues a
one-shot system reminder for injection into the next tool result. The
agent then sees the reminder and can offer the user a concrete
``hermes hooks new`` command, turning a repeated deterministic action
into a configured shell hook.

Design constraints
------------------
* **Single delivery per fingerprint per session.** The reminder is meant
  to nudge once; spamming kills both UX and prompt caching.
* **No mutation of past messages.** The hint is appended to the *current*
  tool result content so caching invariants stay intact.
* **Cheap.** Fingerprinting reuses the deterministic logic that powers
  ``hermes hooks suggest`` — no LLM call on the hot path.
* **Defaults to ON.** Users can opt out via
  ``display.hook_suggestions: off`` in ``~/.hermes/config.yaml``.

The module is intentionally small: it owns counting + message-building.
Injection into the messages list is performed by ``AIAgent`` (parallel
to ``_apply_pending_steer_to_tool_results``) so we stay out of the
agent loop's flow-control.
"""

from __future__ import annotations

import shlex
import threading
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

DEFAULT_THRESHOLD = 3
DEFAULT_MAX_HINTS_PER_SESSION = 3


@dataclass
class _Hint:
    """A queued reminder waiting to be appended to the next tool result."""
    fingerprint_key: str
    text: str


class HookHinter:
    """Per-session repetition detector.

    Thread-safe: ``record()`` is called from the tool-execution thread(s)
    and ``drain_pending()`` from the agent loop thread. All state lives
    behind a single ``threading.Lock``.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        max_hints_per_session: int = DEFAULT_MAX_HINTS_PER_SESSION,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.threshold = max(2, int(threshold))
        self.max_hints_per_session = max(1, int(max_hints_per_session))
        self._counts: Counter[str] = Counter()
        self._emitted: Set[str] = set()
        self._queue: Deque[_Hint] = deque()
        self._hints_total: int = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, tool_name: str, args: Dict[str, Any]) -> None:
        """Record a successful tool call and queue a hint if a fingerprint
        just crossed the threshold for the first time this session.

        Failures (exceptions, blocked calls, dispatched-but-erroring tools)
        should NOT be recorded — we only want to flag patterns where the
        deterministic side-effect is consistently happening.
        """
        if not self.enabled:
            return
        if not isinstance(tool_name, str) or not tool_name:
            return
        if not isinstance(args, dict):
            args = {}

        try:
            from hermes_cli.hooks_suggest import (
                CAT_NAVIGATION,
                CAT_OBSERVATION,
                fingerprint_tool_call,
            )
        except Exception:
            # Suggest module missing or broken — fail silently. The hint
            # system is best-effort and must never crash the agent loop.
            return

        fp = fingerprint_tool_call(tool_name, args)

        # Skip categories that are "just how the agent works" — high
        # frequency there is not a hook signal, and the suggest module
        # already filters them. Same policy here keeps in-session and
        # offline analysis consistent.
        if fp.category in (CAT_OBSERVATION, CAT_NAVIGATION):
            return

        with self._lock:
            if self._hints_total >= self.max_hints_per_session:
                # Cap reached — keep counting (cheap) but stop emitting.
                self._counts[fp.key] += 1
                return

            self._counts[fp.key] += 1
            count = self._counts[fp.key]
            if count < self.threshold:
                return
            if fp.key in self._emitted:
                return

            self._emitted.add(fp.key)
            self._hints_total += 1
            self._queue.append(_Hint(
                fingerprint_key=fp.key,
                text=_build_hint_text(fp, count),
            ))

    # ------------------------------------------------------------------
    # Draining (called from agent loop)
    # ------------------------------------------------------------------

    def drain_pending(self) -> List[_Hint]:
        """Pop every queued hint. Caller is responsible for injecting them
        into the next tool result. Hints are dropped from the queue once
        returned — by design no retry on injection failure (the next
        observation that crosses the threshold will queue a fresh one)."""
        if not self.enabled:
            return []
        with self._lock:
            if not self._queue:
                return []
            out = list(self._queue)
            self._queue.clear()
            return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_hint_text(fp, count: int) -> str:
    """Compose the system-style note that gets appended to the tool result.

    Kept short: the model needs only enough signal to decide whether to
    propose a hook to the user. Long reminders eat prompt-cache budget for
    little benefit.

    When the fingerprint maps cleanly to one of the bundled starter
    templates (``scripts/agent-hooks-examples/``), the scaffold line points
    at ``--from-template <name>``. Concrete > abstract: a copy-pasteable
    command beats "go think about what matcher you want".
    """
    base = (
        f"\n\n[hermes hook hint] You have now invoked `{fp.human}` "
        f"{count} times this session. If this is a repeating deterministic "
        f"action, consider proposing a shell hook to the user:"
    )

    template = None
    try:
        from hermes_cli.hooks_suggest import best_starter_template
        template = best_starter_template(fp)
    except Exception:
        # best_starter_template is best-effort; never break the hint over it.
        template = None

    if template:
        scaffold = f"hermes hooks new --from-template {template}"
    elif fp.suggested_matcher:
        scaffold = (
            f"hermes hooks new --event {fp.suggested_event} "
            f"--matcher {shlex.quote(fp.suggested_matcher)}"
        )
    else:
        scaffold = f"hermes hooks new --event {fp.suggested_event}"

    suffix = (
        f"\n  • scaffold:  {scaffold}\n"
        f"  • discover patterns: hermes hooks suggest\n"
        f"This reminder is one-shot; surface it to the user once, then "
        f"continue the task."
    )
    return base + suffix


# ---------------------------------------------------------------------------
# Public message-level helpers (consumed by AIAgent)
# ---------------------------------------------------------------------------

def apply_pending_hints_to_tool_results(
    hinter: Optional[HookHinter],
    messages: List[Dict[str, Any]],
    num_tool_msgs: int,
) -> int:
    """Append every pending hint to the LAST tool result in this batch.

    Returns the number of hints injected. ``num_tool_msgs`` bounds the
    tail slice we'll touch — defends against future code that appends
    non-tool entries at the boundary.

    Hints are merged into a single trailing block per call so the model
    sees one coherent reminder rather than three near-identical ones.
    """
    if hinter is None or num_tool_msgs <= 0 or not messages:
        return 0
    pending = hinter.drain_pending()
    if not pending:
        return 0

    target_idx: Optional[int] = None
    for j in range(len(messages) - 1, max(len(messages) - num_tool_msgs - 1, -1), -1):
        msg = messages[j]
        if isinstance(msg, dict) and msg.get("role") == "tool":
            target_idx = j
            break
    if target_idx is None:
        # No tool result to attach to — drop the hints. A subsequent
        # threshold-crossing will queue a fresh one if the pattern continues.
        return 0

    merged = "\n".join(h.text for h in pending)
    existing = messages[target_idx].get("content", "")
    if isinstance(existing, str):
        messages[target_idx]["content"] = existing + merged
    else:
        try:
            blocks = list(existing) if existing else []
            blocks.append({"type": "text", "text": merged.lstrip()})
            messages[target_idx]["content"] = blocks
        except Exception:
            messages[target_idx]["content"] = f"{existing}{merged}"

    return len(pending)
