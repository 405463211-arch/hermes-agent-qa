#!/usr/bin/env python3
"""Obsidian tools — search/view/save against the user's vault.

These tools are the agent-facing surface of the Obsidian bridge.  All the
real logic lives in ``agent/obsidian.py``; this file is a thin schema +
JSON-encoding wrapper.

Three tools:

  - ``obsidian_search(query, max_results=10)``
      Substring search over the configured vault.  Scope-restricted by
      default (``vault/hermes/`` only) — set ``obsidian.search_scope: all``
      in config.yaml to widen.

  - ``obsidian_view(path, offset=1, limit=200)``
      Read a file from the vault.  Refuses paths outside the vault and
      paths outside the active scope.

  - ``obsidian_save(path, content, mode='write')``
      Persist a note inside ``vault/hermes/notes/``.  Cannot write to the
      user's own folders — the user's notes are sacred.

Cost note: 0 baseline tokens beyond the schema (~200 tokens, cached).  The
agent only pays when it actually calls these tools.  No prefetch, no
system-prompt content injection — that's intentional, see
``agent/obsidian.py`` for the rationale.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from agent import obsidian as ob
from tools.registry import registry, tool_error


def _ok(payload: Dict[str, Any]) -> str:
    payload.setdefault("success", True)
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return tool_error(message, success=False)


def check_obsidian_requirements() -> bool:
    """Available iff the user has configured a vault path that exists."""
    return ob.is_enabled()


# ---------------------------------------------------------------------------
# obsidian_search
# ---------------------------------------------------------------------------

def obsidian_search(query: str, max_results: int = 10) -> str:
    if not query or not query.strip():
        return _err("query is required and must be non-empty")
    if not ob.is_enabled():
        return _err(
            "Obsidian bridge is not configured. Run `hermes obsidian setup` "
            "to point hermes at your vault."
        )

    hits = ob.search(query, max_results=max_results)
    return _ok({
        "vault_path": str(ob.get_vault_path() or ""),
        "scope": ob.get_search_scope(),
        "query": query,
        "hit_count": len(hits),
        "hits": [
            {"path": h.relpath, "line": h.line, "preview": h.preview}
            for h in hits
        ],
    })


OBSIDIAN_SEARCH_SCHEMA = {
    "name": "obsidian_search",
    "description": (
        "Search the user's Obsidian vault for matching text. Use this when "
        "the user asks about something they have notes on (\"my notes on X\", "
        "\"我之前写过\", \"my Obsidian\", \"我笔记里\"). Returns matching lines "
        "with file paths and line numbers — follow up with obsidian_view to "
        "read the full file.\n\n"
        "Scope is controlled by config (default: only vault/hermes/ — the "
        "hermes-managed subdir). Search may return zero hits if the user's "
        "actual notes live outside the configured scope; tell the user to "
        "set `obsidian.search_scope: all` in config.yaml if that happens."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term (literal substring, smart-case).",
            },
            "max_results": {
                "type": "integer",
                "description": "Max hits to return (default 10, cap 50).",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# obsidian_view
# ---------------------------------------------------------------------------

def obsidian_view(path: str, offset: int = 1, limit: int = 200) -> str:
    if not ob.is_enabled():
        return _err(
            "Obsidian bridge is not configured. Run `hermes obsidian setup`."
        )
    result = ob.view(path, offset=offset, limit=limit)
    if not result.get("success"):
        return _err(result.get("error", "view failed"))
    return _ok(result)


OBSIDIAN_VIEW_SCHEMA = {
    "name": "obsidian_view",
    "description": (
        "Read a file from the user's Obsidian vault. Use the relative path "
        "returned by obsidian_search (e.g. \"hermes/notes/2026-05-06.md\"). "
        "Supports offset/limit for paging through long notes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path (e.g. 'hermes/rules.md').",
            },
            "offset": {
                "type": "integer",
                "description": "1-based line number to start from (default 1).",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return (default 200, cap 2000).",
                "default": 200,
            },
        },
        "required": ["path"],
    },
}


# ---------------------------------------------------------------------------
# obsidian_save
# ---------------------------------------------------------------------------

def obsidian_save(path: str, content: str, mode: str = "write") -> str:
    if not ob.is_enabled():
        return _err(
            "Obsidian bridge is not configured. Run `hermes obsidian setup`."
        )
    result = ob.save(path, content, mode=mode)
    if not result.get("success"):
        return _err(result.get("error", "save failed"))
    return _ok(result)


OBSIDIAN_SAVE_SCHEMA = {
    "name": "obsidian_save",
    "description": (
        "Save a note into the user's Obsidian vault under vault/hermes/notes/. "
        "Use this for distilled session output the user might want to keep — "
        "a debug post-mortem, a reference snippet, a meeting summary, a draft "
        "the user can later promote into a real note.\n\n"
        "Cannot write outside vault/hermes/ — the user's own notes are not "
        "writable by the agent. For RULES/MEMORY/USER persistence, use the "
        "memory tool instead; this tool is for free-form notes, not "
        "directives or user profile facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to vault/hermes/notes/ (e.g. "
                    "'2026-05-06-debug-session.md'). Subdirectories allowed."
                ),
            },
            "content": {
                "type": "string",
                "description": "Markdown content to save.",
            },
            "mode": {
                "type": "string",
                "enum": ["write", "append"],
                "description": "'write' overwrites; 'append' appends. Default 'write'.",
                "default": "write",
            },
        },
        "required": ["path", "content"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="obsidian_search",
    toolset="obsidian",
    schema=OBSIDIAN_SEARCH_SCHEMA,
    handler=lambda args, **kw: obsidian_search(
        query=args.get("query", ""),
        max_results=args.get("max_results", 10),
    ),
    check_fn=check_obsidian_requirements,
    emoji="📓",
)

registry.register(
    name="obsidian_view",
    toolset="obsidian",
    schema=OBSIDIAN_VIEW_SCHEMA,
    handler=lambda args, **kw: obsidian_view(
        path=args.get("path", ""),
        offset=args.get("offset", 1),
        limit=args.get("limit", 200),
    ),
    check_fn=check_obsidian_requirements,
    emoji="📖",
)

registry.register(
    name="obsidian_save",
    toolset="obsidian",
    schema=OBSIDIAN_SAVE_SCHEMA,
    handler=lambda args, **kw: obsidian_save(
        path=args.get("path", ""),
        content=args.get("content", ""),
        mode=args.get("mode", "write"),
    ),
    check_fn=check_obsidian_requirements,
    emoji="✍️",
)
