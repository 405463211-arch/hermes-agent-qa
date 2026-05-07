"""Heuristic tool-result error detection for the self_learning plugin.

Pure functions — no I/O, no globals.  The plugin's hook layer feeds tool
results in here and gets back ``None`` (not a recognisable error) or a
classification dict it can use to bucket the failure.

Classification dicts use these keys:

  * ``kind``     — "exit_nonzero" | "tool_failure" | "permission_denied" |
                   "not_found" | "timeout"
  * ``summary``  — one-line human-readable description
  * ``signal``   — a short stable token used as part of the pattern_key

Pattern keys are constructed as ``tool.<tool_name>.<signal>`` so the same
pattern of failure across calls of the same tool dedupes naturally.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# Threshold for nudging — kept low so the loop stays visible during real
# debugging sessions without spamming on one-off blips.
DEFAULT_NUDGE_THRESHOLD = 2

# Quick-and-cheap regex hints — applied only when we already know the call
# failed (json success=false or non-zero exit), so false positives are bounded.
_PERMISSION_HINTS = re.compile(
    r"permission denied|operation not permitted|EACCES|sudo required",
    re.IGNORECASE,
)
_NOT_FOUND_HINTS = re.compile(
    r"no such file or directory|not found|ENOENT|cannot find",
    re.IGNORECASE,
)
_TIMEOUT_HINTS = re.compile(
    r"timed out|deadline exceeded|operation timed out|ETIMEDOUT",
    re.IGNORECASE,
)


def _try_parse_json(result: str) -> Optional[Dict[str, Any]]:
    if not result or not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _classify_text(text: str) -> Optional[str]:
    if _PERMISSION_HINTS.search(text):
        return "permission_denied"
    if _NOT_FOUND_HINTS.search(text):
        return "not_found"
    if _TIMEOUT_HINTS.search(text):
        return "timeout"
    return None


def classify_tool_error(
    tool_name: str,
    args: Dict[str, Any],
    result: str,
) -> Optional[Dict[str, str]]:
    """Inspect a tool result; return classification dict on failure, else None.

    The function is intentionally narrow — when in doubt we return ``None``
    so the plugin doesn't nudge on healthy work.  It looks for two clear
    failure signals:

      1. ``terminal`` exit-code non-zero (parsed from JSON ``exit_code``)
      2. Any tool returning JSON with ``success=false`` (the convention
         enforced by ``tools/registry.py``)

    Beyond that, the failure text is matched against permission/notfound/
    timeout regexes to pick a stable signal token.
    """
    tool_name = (tool_name or "").strip().lower()
    if not tool_name or not result:
        return None

    parsed = _try_parse_json(result)
    if parsed is None:
        return None

    is_terminal_failure = (
        tool_name == "terminal"
        and isinstance(parsed.get("exit_code"), int)
        and parsed["exit_code"] != 0
    )
    is_explicit_failure = parsed.get("success") is False

    if not (is_terminal_failure or is_explicit_failure):
        return None

    text_blob = " ".join(
        str(parsed.get(k, "")) for k in ("error", "stderr", "stdout", "message")
    )

    signal = _classify_text(text_blob)
    if signal is None:
        if is_terminal_failure:
            kind = "exit_nonzero"
            signal = f"exit_{parsed['exit_code']}"
        else:
            kind = "tool_failure"
            signal = "generic_failure"
    else:
        kind = signal

    summary = _summary_line(tool_name, parsed, signal)
    return {"kind": kind, "summary": summary, "signal": signal}


def _summary_line(tool_name: str, parsed: Dict[str, Any], signal: str) -> str:
    pieces: list[str] = [f"{tool_name} {signal}"]
    err = parsed.get("error") or parsed.get("stderr") or parsed.get("message")
    if err:
        snippet = str(err).strip().splitlines()[0][:120]
        if snippet:
            pieces.append(snippet)
    return " — ".join(pieces)


def pattern_key_for(tool_name: str, classification: Dict[str, str]) -> str:
    """Build the stable pattern key used by the self_learning plugin / store."""
    signal = (classification.get("signal") or "unknown").strip().lower()
    return f"tool.{(tool_name or 'unknown').lower()}.{signal}"
