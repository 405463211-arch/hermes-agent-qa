#!/usr/bin/env python3
"""Project Knowledge tools — search / view / save the per-project reference tree.

Three tools:

  - ``project_knowledge_search(query, max_results=10)``
      Plain ripgrep over the project knowledge directory.  Returns matching
      lines with file + line number context.  Cheap, deterministic, no
      embedder required.  Use this for "where is X mentioned" queries.

  - ``project_knowledge_view(relpath, offset=1, limit=200)``
      Read a specific file (or chunk of it) inside the knowledge tree.
      Mirrors the read_file API so the model already knows the pattern;
      enforces directory containment so the tool can't be tricked into
      reading arbitrary files via ``../`` escapes.

  - ``project_knowledge_save(relpath, content, mode='write')``
      Persist a new entry to the knowledge tree (e.g. distilled facts the
      agent learned this session).  Supports ``write`` (overwrite) and
      ``append`` modes.  Refuses to write outside the project directory.

All three are scoped to the **active project** (auto-detected from the
git root basename of cwd).  Cross-project queries aren't allowed —
each project has an isolated knowledge tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.project_knowledge import detect_project_name, get_project_dir
from tools.registry import registry, tool_error


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _resolve_inside(pk_dir: Path, relpath: str) -> Optional[Path]:
    """Resolve *relpath* under *pk_dir*, refusing escapes via ``../`` etc.

    Returns the resolved absolute path on success, ``None`` when the
    requested path would land outside ``pk_dir``.
    """
    if not relpath:
        return None
    try:
        candidate = (pk_dir / relpath).resolve()
        pk_resolved = pk_dir.resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(pk_resolved)
    except ValueError:
        return None
    return candidate


def _ok(payload: Dict[str, Any]) -> str:
    payload.setdefault("success", True)
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return tool_error(message, success=False)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def _ripgrep_available() -> bool:
    return shutil.which("rg") is not None


def _grep_with_rg(query: str, pk_dir: Path, max_results: int) -> List[Dict[str, Any]]:
    """Run ripgrep and return JSON-friendly hits (line numbers + previews)."""
    cmd = [
        "rg",
        "--no-heading",
        "--line-number",
        "--max-count", "5",   # cap matches per file
        "--max-columns", "300",
        "--smart-case",
        "--",
        query,
        str(pk_dir),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return [{"error": f"ripgrep failed: {e}"}]
    hits: List[Dict[str, Any]] = []
    if not result.stdout:
        return hits
    for line in result.stdout.splitlines():
        # Format: <abs_path>:<lineno>:<text>
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path_str, lineno_str, text = parts
        try:
            relpath = str(Path(path_str).relative_to(pk_dir))
        except ValueError:
            relpath = path_str
        try:
            lineno = int(lineno_str)
        except ValueError:
            lineno = 0
        hits.append({
            "path": relpath,
            "line": lineno,
            "preview": text.strip()[:200],
        })
        if len(hits) >= max_results:
            break
    return hits


def _grep_python_fallback(
    query: str, pk_dir: Path, max_results: int
) -> List[Dict[str, Any]]:
    """Pure-Python fallback when ripgrep isn't installed."""
    hits: List[Dict[str, Any]] = []
    q_lower = query.lower()
    for path in sorted(pk_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if any(p in {"__pycache__", "node_modules"} for p in path.parts):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for n, line in enumerate(fh, 1):
                    if q_lower in line.lower():
                        hits.append({
                            "path": str(path.relative_to(pk_dir)),
                            "line": n,
                            "preview": line.strip()[:200],
                        })
                        if len(hits) >= max_results:
                            return hits
        except OSError:
            continue
    return hits


def project_knowledge_search(
    query: str,
    max_results: int = 10,
    project: Optional[str] = None,
) -> str:
    """Search the active project knowledge tree."""
    if not query or not query.strip():
        return _err("query is required and must be non-empty")
    max_results = max(1, min(int(max_results), 50))

    project_name = project or detect_project_name()
    pk_dir = get_project_dir(project_name)
    if not pk_dir.is_dir():
        return _ok({
            "project": project_name,
            "pk_dir": str(pk_dir),
            "exists": False,
            "hits": [],
            "message": (
                f"No project-knowledge directory at {pk_dir}. "
                f"Create one with `mkdir -p {pk_dir}` and add reference "
                f"files (markdown, YAML, JSON, text)."
            ),
        })

    if _ripgrep_available():
        hits = _grep_with_rg(query, pk_dir, max_results)
    else:
        hits = _grep_python_fallback(query, pk_dir, max_results)

    return _ok({
        "project": project_name,
        "pk_dir": str(pk_dir),
        "exists": True,
        "query": query,
        "hits": hits,
        "hit_count": len(hits),
    })


# --------------------------------------------------------------------------
# view
# --------------------------------------------------------------------------

def project_knowledge_view(
    relpath: str,
    offset: int = 1,
    limit: int = 200,
    project: Optional[str] = None,
) -> str:
    """Read a specific file under the project knowledge tree."""
    if not relpath:
        return _err("relpath is required")

    project_name = project or detect_project_name()
    pk_dir = get_project_dir(project_name)
    if not pk_dir.is_dir():
        return _err(
            f"No project-knowledge directory at {pk_dir}. "
            f"Use project_knowledge_save to create the first entry."
        )

    target = _resolve_inside(pk_dir, relpath)
    if target is None:
        return _err(f"Path '{relpath}' is not inside {pk_dir} (refusing to read).")
    if not target.is_file():
        return _err(f"Not a file (or does not exist): {relpath}")

    try:
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except OSError as e:
        return _err(f"Read failed: {e}")

    total_lines = len(all_lines)
    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 2000))
    start = offset - 1
    end = min(start + limit, total_lines)
    chunk = "".join(all_lines[start:end])

    return _ok({
        "project": project_name,
        "path": relpath,
        "offset": offset,
        "lines_returned": end - start,
        "total_lines": total_lines,
        "content": chunk,
        "more_available": end < total_lines,
    })


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------

_SAFE_CHARS_HINT = (
    "Path components must not start with '.', and traversal segments "
    "(.., absolute paths, leading '/') are rejected."
)


def project_knowledge_save(
    relpath: str,
    content: str,
    mode: str = "write",
    project: Optional[str] = None,
) -> str:
    """Persist *content* to the project knowledge tree.

    ``mode='write'`` overwrites the file; ``mode='append'`` appends.  The
    parent directory is created if missing.  Refuses paths outside the
    project knowledge dir.
    """
    if not relpath:
        return _err("relpath is required")
    if mode not in ("write", "append"):
        return _err("mode must be 'write' or 'append'")
    if content is None:
        return _err("content is required")
    if relpath.startswith("/") or ".." in relpath.split("/"):
        return _err(f"Invalid relpath '{relpath}'. {_SAFE_CHARS_HINT}")

    project_name = project or detect_project_name()
    pk_dir = get_project_dir(project_name)
    pk_dir.mkdir(parents=True, exist_ok=True)

    target = _resolve_inside(pk_dir, relpath)
    if target is None:
        return _err(f"Path '{relpath}' resolves outside {pk_dir} (refusing to write).")

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if mode == "write":
            target.write_text(content, encoding="utf-8")
            written = len(content)
            action = "written"
        else:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(content)
            written = len(content)
            action = "appended"
    except OSError as e:
        return _err(f"Write failed: {e}")

    return _ok({
        "project": project_name,
        "path": relpath,
        "abs_path": str(target),
        "mode": mode,
        "bytes_written": written,
        "message": (
            f"{action.capitalize()} {written} chars to {relpath}. The new "
            f"content will appear in the next session's project_knowledge "
            f"index automatically."
        ),
    })


# --------------------------------------------------------------------------
# Schemas + registry
# --------------------------------------------------------------------------

PK_SEARCH_SCHEMA = {
    "name": "project_knowledge_search",
    "description": (
        "Search the active project's knowledge tree (a directory under "
        "$HERMES_HOME/project-knowledge/<project>/) for reference data — "
        "distilled source code, extracted strings, page maps, schemas, etc. "
        "Use this BEFORE reading large files. Returns matching lines with "
        "file paths and line numbers; follow up with project_knowledge_view "
        "to read the full file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term (literal or regex; ripgrep smart-case).",
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


PK_VIEW_SCHEMA = {
    "name": "project_knowledge_view",
    "description": (
        "Read a specific file inside the active project's knowledge tree. "
        "Use the relative path returned by project_knowledge_search. "
        "Supports offset/limit for paging through large dumps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "relpath": {
                "type": "string",
                "description": "Path relative to the project knowledge dir.",
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
        "required": ["relpath"],
    },
}


PK_SAVE_SCHEMA = {
    "name": "project_knowledge_save",
    "description": (
        "Persist distilled knowledge to the active project's knowledge tree. "
        "Use this when you've learned a stable fact about the project that "
        "would be useful as reference data later (a page map, an extracted "
        "i18n list, an API schema, a workflow diagram). Avoid saving "
        "task progress — that belongs in session history. Avoid saving rules "
        "or preferences — those belong in the persistent memory tool. "
        "Project knowledge is for REFERENCE DATA: things you'd grep for "
        "next time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "relpath": {
                "type": "string",
                "description": (
                    "Path inside the project knowledge dir. Use a stable "
                    "directory layout (e.g. distilled/i18n/strings.yaml)."
                ),
            },
            "content": {
                "type": "string",
                "description": "Full file content (markdown / YAML / JSON / text).",
            },
            "mode": {
                "type": "string",
                "enum": ["write", "append"],
                "description": "'write' overwrites; 'append' appends. Default 'write'.",
                "default": "write",
            },
        },
        "required": ["relpath", "content"],
    },
}


def _check_pk_requirements() -> bool:
    # No external requirements — the tool degrades to "no PK dir yet" rather
    # than failing.  Always available.
    return True


# --------------------------------------------------------------------------
# project_knowledge_promote — distill PK doc into a RULES.md entry
# --------------------------------------------------------------------------


def project_knowledge_promote(
    *,
    rule_text: str,
    source_relpath: str = "",
    pinned: bool = False,
    store: Any = None,
) -> str:
    """Promote a fact discovered in the project-knowledge tree into RULES.md.

    The agent calls this when a piece of project knowledge proves to be a
    persistent constraint worth elevating from "data the model can grep" to
    "rule injected into every system prompt".  The promotion reuses the
    rules-lifecycle layer so the new entry gets a ``[NEW]`` tag for 7 days
    (so the model treats it as trial), and an audit trail pointing back at
    the source file lives in the rule's metadata via the ``source`` field.

    ``store`` is the live ``MemoryStore`` (passed by the agent's dispatch
    code).  When unavailable, the call is rejected — promotion writes to
    RULES.md and we don't want to construct an ad-hoc store mid-tool.
    """
    rule_text = (rule_text or "").strip()
    if not rule_text:
        return tool_error("rule_text is required and must be non-empty.")
    if store is None:
        return tool_error(
            "MemoryStore unavailable in this context — cannot promote.",
        )

    src = (source_relpath or "").strip().replace("\n", " ")[:120]
    source = f"PK:{src}" if src else "PK"
    try:
        result = store.add_rule_with_lifecycle(
            text=rule_text,
            pinned=bool(pinned),
            source=source,
            recurrence=0,
            pattern_key=f"project_knowledge.{src}" if src else "project_knowledge",
        )
    except Exception as exc:  # pragma: no cover — defensive
        return tool_error(f"Promotion failed: {exc}")

    if not result.get("success"):
        return json.dumps(result)
    return json.dumps({
        "success": True,
        "promoted_to": "rules",
        "source": source,
        "pinned": bool(pinned),
        "rule_text": rule_text,
        "message": (
            f"Promoted project-knowledge fact to RULES.md (source={source}). "
            "It will appear with a [NEW] marker for the next 7 days."
        ),
    })


PK_PROMOTE_SCHEMA = {
    "name": "project_knowledge_promote",
    "description": (
        "Promote a fact you discovered while reading project-knowledge into "
        "RULES.md so it gets injected at the top of every system prompt for "
        "this project.  Use sparingly — only for constraints / contracts the "
        "agent must follow on every turn.  For one-off facts the model can "
        "look up on demand, leave them in the project knowledge tree.\n\n"
        "WHEN TO USE:\n"
        "  • A repo-wide convention you keep tripping over (build cmd, "
        "lint config, deploy script).\n"
        "  • A safety constraint specific to this project (e.g. 'never run "
        "scripts/destroy.sh outside staging').\n"
        "  • A persistent gotcha that recurs across tasks.\n\n"
        "DON'T USE for: file-specific notes, reference docs, or anything you "
        "can rediscover via project_knowledge_search in <30 seconds."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rule_text": {
                "type": "string",
                "description": "The rule body that will be injected into RULES.md.",
            },
            "source_relpath": {
                "type": "string",
                "description": (
                    "Path inside the project-knowledge dir that this fact came "
                    "from (recorded in the rule metadata for traceability)."
                ),
            },
            "pinned": {
                "type": "boolean",
                "description": "Pin the rule (always at top, never auto-archived).",
                "default": False,
            },
        },
        "required": ["rule_text"],
    },
}


registry.register(
    name="project_knowledge_search",
    toolset="project_knowledge",
    schema=PK_SEARCH_SCHEMA,
    handler=lambda args, **kw: project_knowledge_search(
        query=args.get("query", ""),
        max_results=args.get("max_results", 10),
    ),
    check_fn=_check_pk_requirements,
    emoji="📚",
)


registry.register(
    name="project_knowledge_view",
    toolset="project_knowledge",
    schema=PK_VIEW_SCHEMA,
    handler=lambda args, **kw: project_knowledge_view(
        relpath=args.get("relpath", ""),
        offset=args.get("offset", 1),
        limit=args.get("limit", 200),
    ),
    check_fn=_check_pk_requirements,
    emoji="📖",
)


registry.register(
    name="project_knowledge_save",
    toolset="project_knowledge",
    schema=PK_SAVE_SCHEMA,
    handler=lambda args, **kw: project_knowledge_save(
        relpath=args.get("relpath", ""),
        content=args.get("content", ""),
        mode=args.get("mode", "write"),
    ),
    check_fn=_check_pk_requirements,
    emoji="📝",
)


registry.register(
    name="project_knowledge_promote",
    toolset="project_knowledge",
    schema=PK_PROMOTE_SCHEMA,
    handler=lambda args, **kw: project_knowledge_promote(
        rule_text=args.get("rule_text", ""),
        source_relpath=args.get("source_relpath", ""),
        pinned=bool(args.get("pinned", False)),
        store=kw.get("store"),
    ),
    check_fn=_check_pk_requirements,
    emoji="⬆️",
)
