"""Heuristic tool-result error detection for the self_learning plugin.

Pure functions — no I/O, no globals.  The plugin's hook layer feeds tool
results in here and gets back ``None`` (not a recognisable error) or a
classification dict it can use to bucket the failure.

Classification dicts use these keys:

  * ``kind``     — "exit_nonzero" | "tool_failure" | "permission_denied" |
                   "not_found" | "timeout"
  * ``summary``  — one-line human-readable description
  * ``signal``   — a short stable token used as part of the pattern_key
  * ``category`` — semantic bucket the tool belongs to (``file_io`` /
                   ``shell`` / ``net`` / ``other``).  Lets us bucket
                   *cross-tool* recurrences — e.g. "read_file not_found"
                   and "terminal cat not_found" both bucket as
                   ``cat.file_io.not_found`` so the second occurrence
                   crosses the nudge threshold instead of staying
                   stranded as two separate per-tool count=1 buckets.

Pattern keys come in two layers:

  * ``tool.<tool_name>.<signal>`` (legacy, per-tool) — used for the
    user-visible ``summary`` so people can see which exact tool failed.
  * ``cat.<category>.<signal>``   (new, cross-tool) — used as the
    *bucketing* key for nudge thresholds, so semantically equivalent
    failures across different tools are counted together.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# Threshold for nudging — kept low so the loop stays visible during real
# debugging sessions without spamming on one-off blips.
DEFAULT_NUDGE_THRESHOLD = 2

# Semantic category map — tools that operate on the same domain (file I/O,
# shell, network) share a category so cross-tool failures of the same
# nature accumulate together. Without this, an LLM hitting "not_found" once
# via ``read_file`` and once via ``terminal cat`` would never trigger a
# nudge (each pattern at count=1) even though it's semantically the same
# pattern. Add new tools here as they become first-class.
_TOOL_CATEGORY: Dict[str, str] = {
    # File I/O — read/write/check on the local filesystem
    "read_file":    "file_io",
    "write_file":   "file_io",
    "edit_file":    "file_io",
    "search_files": "file_io",
    "list_files":   "file_io",
    # Shell — terminal acts as a generic interpreter; we treat its
    # *file-touching* failures (cat/ls/test -f) as file_io semantically
    # (handled below by error-message inspection), and its *non-file*
    # failures as shell.
    "terminal":     "shell",
    "execute_code": "shell",
    # Network / HTTP fetchers
    "web_search":   "net",
    "web_extract":  "net",
    "browser_navigate": "net",
    # Vault tools — semantically file_io, so a not_found on obsidian_view
    # buckets together with read_file not_found.
    "obsidian_view":   "file_io",
    "obsidian_search": "file_io",
    "obsidian_save":   "file_io",
}

# Signals that imply file-I/O semantics regardless of which tool surfaced
# them. When ``terminal`` is the carrier (``cat /missing/file`` etc.) we
# upgrade its category from generic ``shell`` to ``file_io`` so the
# cross-tool bucket lines up.
_FILE_IO_PROMOTING_SIGNALS = {"not_found", "permission_denied"}


def _category_for(tool_name: str, signal: str) -> str:
    """Pick the semantic category for the (tool, signal) pair.

    See ``_TOOL_CATEGORY`` for the static map.  ``terminal`` is special-
    cased: a not_found / permission_denied surfaced through ``terminal``
    is virtually always a file-I/O error (cat/ls/test/rm), so we bucket
    it under ``file_io`` regardless of the static map.  Everything else
    falls back to ``other``.
    """
    name = (tool_name or "").strip().lower()
    base = _TOOL_CATEGORY.get(name, "other")
    if name == "terminal" and (signal or "") in _FILE_IO_PROMOTING_SIGNALS:
        return "file_io"
    return base

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
    category = _category_for(tool_name, signal)
    return {
        "kind": kind,
        "summary": summary,
        "signal": signal,
        "category": category,
    }


def _summary_line(tool_name: str, parsed: Dict[str, Any], signal: str) -> str:
    pieces: list[str] = [f"{tool_name} {signal}"]
    err = parsed.get("error") or parsed.get("stderr") or parsed.get("message")
    if err:
        snippet = str(err).strip().splitlines()[0][:120]
        if snippet:
            pieces.append(snippet)
    return " — ".join(pieces)


def pattern_key_for(tool_name: str, classification: Dict[str, str]) -> str:
    """Build the *bucketing* pattern key used by the self_learning plugin.

    Returns a cross-tool category key (``cat.<category>.<signal>``) when
    the classification has a ``category`` field — semantically equivalent
    failures across different tools therefore share one bucket and reach
    the nudge threshold together.

    Falls back to the legacy per-tool key (``tool.<tool>.<signal>``) when
    ``category`` is missing (e.g. external callers passing hand-built
    classifications), preserving back-compat for direct API users.

    Use ``per_tool_pattern_key_for(tool, cls)`` if you specifically need
    the per-tool key (e.g. for the human-facing ``summary``).
    """
    signal = (classification.get("signal") or "unknown").strip().lower()
    category = (classification.get("category") or "").strip().lower()
    if category:
        return f"cat.{category}.{signal}"
    return f"tool.{(tool_name or 'unknown').lower()}.{signal}"


def per_tool_pattern_key_for(tool_name: str, classification: Dict[str, str]) -> str:
    """Legacy per-tool pattern key (``tool.<tool>.<signal>``).

    Useful when callers want the *original* per-tool granularity — kept as
    a separate function so we don't conflate "what the user sees in the
    summary" with "which bucket the count goes in".
    """
    signal = (classification.get("signal") or "unknown").strip().lower()
    return f"tool.{(tool_name or 'unknown').lower()}.{signal}"
