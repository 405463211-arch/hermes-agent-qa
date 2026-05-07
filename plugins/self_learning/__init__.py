"""self_learning plugin — soft nudge for the learning-loop tool.

Two hooks:

1. ``post_tool_call`` — observes tool errors.  When the same tool fails in
   a recognisable pattern within a single session (e.g. terminal exits with
   non-zero, write_file returns ``success=false``), we tally it.

2. ``pre_llm_call`` — when the per-session tally for a given pattern
   crosses a small threshold (default: 2), we inject a one-line context
   suggestion via the standard ``pre_llm_call`` channel reminding the
   agent to consider ``learning_record``.

Crucially this plugin **never** auto-records or auto-promotes anything — the
agent stays the only authority for what gets persisted to the learning store.
The plugin's job is just to keep the loop visible at the right moment.

The detection is intentionally narrow: only ``error`` and ``terminal`` style
failures, no string heuristics on tool stdout (false-positive prone).  See
``error_detector.py`` for the rule set.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .error_detector import (
    classify_tool_error,
    DEFAULT_NUDGE_THRESHOLD,
    pattern_key_for,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-session pattern tally + nudge state
# ---------------------------------------------------------------------------

# Keyed by session_id (or "" when unknown).  Each value is a dict mapping
# pattern_key → list of {tool, summary} dicts captured this session.  We
# don't try to share this across sessions; the LearningStore is for that.
_pattern_state: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
    lambda: defaultdict(list)
)
# Pattern keys we've already nudged about in this session — avoid spam.
_nudged_state: Dict[str, set] = defaultdict(set)
_lock = threading.Lock()


def _on_post_tool_call(
    *,
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: str = "",
    task_id: str = "",
    session_id: str = "",
    **_kw: Any,
) -> None:
    """Record an error if the tool result indicates failure."""
    classification = classify_tool_error(tool_name, args or {}, result)
    if classification is None:
        return

    pattern = pattern_key_for(tool_name, classification)
    summary = classification.get("summary", "")

    with _lock:
        bucket = _pattern_state[session_id][pattern]
        bucket.append({"tool": tool_name, "summary": summary})


def _on_pre_llm_call(
    *,
    session_id: str = "",
    **_kw: Any,
) -> Optional[Dict[str, Any]]:
    """Inject a soft context message when a pattern crossed the threshold."""
    if not session_id:
        return None
    with _lock:
        pending = _pattern_state.get(session_id, {})
        already_nudged = _nudged_state[session_id]
        new_pattern = None
        new_count = 0
        for pattern, occurrences in pending.items():
            if pattern in already_nudged:
                continue
            if len(occurrences) >= DEFAULT_NUDGE_THRESHOLD:
                new_pattern = pattern
                new_count = len(occurrences)
                break
        if new_pattern is None:
            return None
        already_nudged.add(new_pattern)
        sample = pending[new_pattern][-1]

    # Soft nudge — never insists, never records on the agent's behalf.
    msg = (
        f"[self-learning] The same kind of failure ('{new_pattern}', tool: "
        f"{sample.get('tool', '?')}) recurred {new_count} times this session. "
        "If this is a real recurring pattern, consider calling "
        "learning_record(category='error', pattern_key='" + new_pattern +
        "', summary='...', suggested_action='...') so the loop can "
        "auto-promote it once it proves durable."
    )
    return {"context": msg}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Hermes plugin entry — wires the two hooks."""
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
