#!/usr/bin/env python3
"""
Learning Tool — agent-facing self-learning loop
================================================

Three tools backed by ``agent.learning_store.LearningStore``:

  * ``learning_record``  — capture an in-flight learning, error, or feature
    request with a stable ``pattern_key``.  When the same pattern recurs
    enough times across enough tasks within a 30-day window, we auto-promote
    the entry into RULES.md (via ``MemoryStore.add_rule_with_lifecycle``).
  * ``learning_list``    — query past entries (by status / category / area).
  * ``learning_resolve`` — mark an entry as fixed.  Resolved entries no
    longer accumulate recurrence counts; the next sighting starts a new row.

The auto-promotion link is the value-add over plain ``memory`` writes:
the model can record everything noisy in here without polluting RULES.md
or MEMORY.md, and only patterns that prove durable get a permanent home.

Promotion threshold defaults: 3 recurrences, 2 distinct tasks, 30-day window.
See ``agent.learning_store.PromotionRule`` for the knobs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton store accessor (lazy — works whether or not memory module is loaded)
# ---------------------------------------------------------------------------

_GLOBAL_STORE = None  # populated on first use; reset by test fixtures


def _get_store():
    """Return the process-wide ``LearningStore`` (lazy init).

    Tests can override this by monkeypatching the module-level
    ``_GLOBAL_STORE`` to a freshly-built store with a temp db path.
    """
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        from agent.learning_store import LearningStore
        _GLOBAL_STORE = LearningStore()
    return _GLOBAL_STORE


def _reset_store_for_tests() -> None:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is not None:
        try:
            _GLOBAL_STORE.close()
        except Exception:
            pass
    _GLOBAL_STORE = None


# ---------------------------------------------------------------------------
# Auto-promotion link
# ---------------------------------------------------------------------------

def _try_auto_promote(entry: Dict[str, Any], memory_store) -> Optional[Dict[str, Any]]:
    """Promote an eligible learning entry into RULES.md.

    Returns the promotion result dict on success, ``None`` when the entry
    is ineligible or no memory store is available.

    Promotion logic:
      * The rule body comes from ``suggested_action`` if present, else
        ``summary``.  Empty bodies abort the promotion (the entry stays
        ``pending`` so a future ``learning_record`` call can re-attempt
        with a better suggested_action).
      * On a successful add we mark the learning ``promoted`` with
        target=``rules``.
    """
    if memory_store is None:
        return None
    if not entry.get("eligible_for_promotion"):
        return None

    rule_text = (entry.get("suggested_action") or "").strip()
    if not rule_text:
        rule_text = (entry.get("summary") or "").strip()
    if not rule_text:
        return None

    try:
        result = memory_store.add_rule_with_lifecycle(
            text=rule_text,
            pinned=False,
            source=entry["id"],
            recurrence=int(entry.get("recurrence_count") or 0),
            pattern_key=entry.get("pattern_key", ""),
        )
    except Exception as exc:
        logger.warning("auto-promotion to RULES failed for %s: %s", entry.get("id"), exc)
        return None

    if not result.get("success"):
        # RULES.md was full or rejected — leave learning pending for retry.
        logger.info(
            "auto-promotion deferred for %s (rules add returned: %s)",
            entry.get("id"),
            result.get("error", "?"),
        )
        return None

    # Persist promotion status on the learning row so we don't keep retrying.
    try:
        store = _get_store()
        store.mark_promoted(entry["id"], target="rules")
    except Exception:
        pass

    return {
        "success": True,
        "promoted_to": "rules",
        "rule_text": rule_text,
        "source": entry["id"],
    }


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def learning_record_handler(args: Dict[str, Any], **kw) -> str:
    category = (args.get("category") or "").strip().lower()
    pattern_key = (args.get("pattern_key") or "").strip()
    summary = (args.get("summary") or "").strip()

    if not category:
        return json.dumps({"success": False, "error": "category is required"})
    if not pattern_key:
        return json.dumps({"success": False, "error": "pattern_key is required"})
    if not summary:
        return json.dumps({"success": False, "error": "summary is required"})

    store = _get_store()
    try:
        entry = store.record(
            category=category,
            pattern_key=pattern_key,
            summary=summary,
            details=args.get("details", "") or "",
            suggested_action=args.get("suggested_action", "") or "",
            subcategory=args.get("subcategory", "") or "",
            priority=args.get("priority", "medium") or "medium",
            area=args.get("area", "") or "",
            task_id=str(kw.get("task_id") or args.get("task_id") or ""),
            related_files=list(args.get("related_files") or []),
        )
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})

    response: Dict[str, Any] = {
        "success": True,
        "id": entry["id"],
        "recurrence_count": entry["recurrence_count"],
        "distinct_tasks": entry["distinct_tasks"],
        "status": entry["status"],
        "eligible_for_promotion": bool(entry["eligible_for_promotion"]),
    }

    # Try to auto-promote eligible entries.  The MemoryStore is passed via
    # the kwargs that ``handle_function_call`` threads through (``store``
    # kwarg), matching the convention used by the existing ``memory`` tool.
    memory_store = kw.get("store")
    promo = _try_auto_promote(entry, memory_store)
    if promo:
        response["auto_promoted"] = True
        response["promoted_to"] = promo["promoted_to"]
        response["rule_text"] = promo["rule_text"]
        response["message"] = (
            f"Pattern recurred {entry['recurrence_count']}x across "
            f"{entry['distinct_tasks']} tasks → auto-promoted to RULES "
            f"(status=trial, [NEW] for the next 7 days)."
        )

    return json.dumps(response)


def learning_list_handler(args: Dict[str, Any], **kw) -> str:
    store = _get_store()
    rows = store.list(
        status=args.get("status"),
        category=args.get("category"),
        area=args.get("area"),
        limit=int(args.get("limit") or 20),
    )
    # Project to a compact form so the model's context isn't blown out.
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "category": r["category"],
            "pattern_key": r["pattern_key"],
            "summary": r["summary"],
            "status": r["status"],
            "priority": r["priority"],
            "recurrence_count": r["recurrence_count"],
            "distinct_tasks": r["distinct_tasks"],
            "promoted_to": r.get("promoted_to"),
        })
    return json.dumps({"success": True, "count": len(out), "entries": out})


def learning_resolve_handler(args: Dict[str, Any], **kw) -> str:
    learning_id = (args.get("learning_id") or "").strip()
    if not learning_id:
        return json.dumps({"success": False, "error": "learning_id is required"})
    store = _get_store()
    return json.dumps(store.mark_resolved(learning_id, notes=args.get("notes", "") or ""))


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

LEARNING_RECORD_SCHEMA = {
    "name": "learning_record",
    "description": (
        "Record a learning, error, or feature request for the self-improvement "
        "loop.  Use this for transient observations that may or may not become "
        "permanent rules — it tracks recurrence and only promotes patterns "
        "that prove durable, so you don't pollute RULES.md or MEMORY.md with "
        "one-off entries.\n\n"
        "WHEN TO USE (capture immediately when you see these):\n"
        "  • User correction — 'No, that's wrong' / 'Actually...' / '不对'\n"
        "  • Tool call failed unexpectedly and required debugging\n"
        "  • User requested a capability that doesn't exist\n"
        "  • You discover prior knowledge was outdated\n"
        "  • You find a recurring pattern in the work\n\n"
        "AUTO-PROMOTION: if the same ``pattern_key`` is recorded ≥3 times "
        "across ≥2 distinct tasks within 30 days, the entry is auto-promoted "
        "to RULES.md (status=trial, [NEW] tag for 7 days).  Choose "
        "``pattern_key`` carefully: it must be stable across recurrences "
        "(e.g. ``tool.terminal.permission_denied``, not a free-form summary).\n\n"
        "DO NOT use for: long-term facts (use the persistent memory tool), "
        "explicit rules from the user (use the persistent memory tool with "
        "target='rules' directly), or task progress logs (use the todo tool). "
        " When in doubt, prefer ``learning_record`` over a direct memory "
        "write for anything error- or correction-shaped — the dedupe "
        "machinery is what you want."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["learning", "error", "feature_request"],
                "description": (
                    "'learning' for corrections / knowledge gaps / best practices; "
                    "'error' for tool/command failures; "
                    "'feature_request' for missing capabilities the user asked for."
                ),
            },
            "pattern_key": {
                "type": "string",
                "description": (
                    "Stable dedupe key.  Prefer dotted form: "
                    "'<area>.<thing>.<problem>'.  Example: "
                    "'agent.scope.unconfirmed_bulk_edit', "
                    "'tool.terminal.permission_denied'."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One-line description of what was learned/failed/requested.",
            },
            "details": {
                "type": "string",
                "description": "Optional full context (what happened, what was wrong, etc.)",
            },
            "suggested_action": {
                "type": "string",
                "description": (
                    "What to do differently.  When auto-promoted, this becomes "
                    "the rule text.  Without it, the rule falls back to ``summary``."
                ),
            },
            "subcategory": {
                "type": "string",
                "enum": ["correction", "knowledge_gap", "best_practice", "insight", ""],
            },
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
            },
            "area": {
                "type": "string",
                "description": "Free-form tag (e.g. 'frontend', 'tests', 'config').",
            },
            "related_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths relevant to this learning.",
            },
        },
        "required": ["category", "pattern_key", "summary"],
    },
}


LEARNING_LIST_SCHEMA = {
    "name": "learning_list",
    "description": (
        "List recorded learnings, errors, and feature requests.  Use this "
        "before starting a complex task to recall past mistakes / patterns "
        "in the same area, or to find candidates worth promoting / resolving."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "resolved", "promoted", "promoted_to_skill", "all"],
            },
            "category": {
                "type": "string",
                "enum": ["learning", "error", "feature_request", "all"],
            },
            "area": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
        },
    },
}


LEARNING_RESOLVE_SCHEMA = {
    "name": "learning_resolve",
    "description": (
        "Mark a learning / error / feature_request as resolved.  Use after "
        "fixing the underlying issue.  Resolved entries no longer accumulate "
        "recurrence counts; the next sighting of the same pattern_key starts "
        "a fresh row."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "learning_id": {"type": "string", "description": "e.g. 'LRN-20260430-A3F'"},
            "notes": {"type": "string"},
        },
        "required": ["learning_id"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="learning_record",
    toolset="learning",
    schema=LEARNING_RECORD_SCHEMA,
    handler=learning_record_handler,
    emoji="📝",
)

registry.register(
    name="learning_list",
    toolset="learning",
    schema=LEARNING_LIST_SCHEMA,
    handler=learning_list_handler,
    emoji="📋",
)

registry.register(
    name="learning_resolve",
    toolset="learning",
    schema=LEARNING_RESOLVE_SCHEMA,
    handler=learning_resolve_handler,
    emoji="✅",
)
