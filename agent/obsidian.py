"""Obsidian Vault Bridge — read/write Obsidian-managed Markdown notes.

Hermes already has five layers of memory (RULES/MEMORY/USER, project-knowledge,
learning_store, LCM, skills).  What was missing: a bridge to the user's own
human-curated knowledge base — typically an Obsidian vault — so the agent can
look up notes the user has been writing for years and so the user can read
hermes-curated state in their familiar editor.

This module is **the data plane** for the bridge:

  - Resolve the configured vault path (with profile-awareness).
  - Enforce a write-allow-list that defaults to ``vault/hermes/`` so we never
    silently overwrite the user's own notes.
  - Enforce a read scope so ``obsidian_search`` doesn't accidentally surface
    a daily note that contains "老板真烦" to the LLM.
  - Provide pure helpers for search/view/save used by both the tools layer
    and the CLI layer.

Design principles
-----------------

1. **Cache-friendly** — nothing here gets injected into the system prompt
   per turn.  The agent uses tools on demand; the only system-prompt cost
   is a tiny instruction block (built in ``agent/prompt_builder.py``).
2. **Scoped by default** — ``search_scope='hermes_subdir'`` (default) means
   the agent can only see ``vault/hermes/`` content.  ``'ingest'`` widens
   to ``vault/hermes/ingest/`` (a curated whitelist).  ``'all'`` opens up
   the whole vault — opt-in for users who already trust the agent.
3. **Writes confined to ``hermes/``** — agent writes (via ``obsidian_save``)
   land under ``vault/hermes/notes/`` by default, never inside the user's
   own folders.
4. **Profile-aware** — the vault path itself is global (you usually have one
   vault for your life), but the staging area inside each profile has its
   own subdirectory.  This lets ``hermes -p coder`` and ``hermes -p personal``
   coexist in the same vault.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Where in the vault hermes-managed files live.  Anything outside this
# subdirectory is treated as the user's own notes — never written by hermes,
# only read when scope explicitly permits.
HERMES_SUBDIR = "hermes"

# Sub-folders within ``vault/hermes/`` that hermes uses.
EXPORT_RULES_FILENAME = "rules.md"
EXPORT_MEMORY_FILENAME = "memory.md"
EXPORT_USER_FILENAME = "user.md"
LEARNINGS_DIRNAME = "learnings"
NOTES_DIRNAME = "notes"
INGEST_DIRNAME = "ingest"
STAGING_RULES_FILENAME = "rules-staging.md"

# File extensions hermes will read/index.  Binaries are silently skipped.
READABLE_EXTS = (".md", ".txt", ".markdown", ".rst", ".org")

# Scopes for ``obsidian_search``.
SCOPE_HERMES_SUBDIR = "hermes_subdir"
SCOPE_INGEST = "ingest"
SCOPE_ALL = "all"
VALID_SCOPES = (SCOPE_HERMES_SUBDIR, SCOPE_INGEST, SCOPE_ALL)

# Hard caps to keep tool responses bounded.
MAX_SEARCH_RESULTS = 50
MAX_VIEW_CHARS = 80_000  # ~20k tokens — agent should page if larger
MAX_SAVE_CHARS = 200_000  # 200k chars per file is plenty for any note

# Marker used in exported markdown so the user knows hermes manages it
# (and so import paths can detect "this is a mirror, don't double-export").
HERMES_MANAGED_MARKER = "<!-- hermes-managed: do not edit; changes are overwritten -->"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _read_obsidian_config() -> dict:
    """Read obsidian config from ``~/.hermes/config.yaml`` (best-effort).

    Falls back to an env-var read so tests / scripts without a config file
    can still drive the bridge.  Returns ``{}`` when nothing is set.

    Resolution order:
      1. ``HERMES_OBSIDIAN_VAULT`` env var (override, useful for tests)
      2. ``obsidian:`` block in config.yaml
      3. empty dict
    """
    env_vault = os.environ.get("HERMES_OBSIDIAN_VAULT", "").strip()
    if env_vault:
        return {
            "enabled": True,
            "vault_path": env_vault,
            "search_scope": os.environ.get(
                "HERMES_OBSIDIAN_SCOPE", SCOPE_HERMES_SUBDIR
            ),
        }

    try:
        # Lazy import — keep this module importable in tests that haven't
        # built up the full CLI config machinery yet.
        from hermes_cli.config import load_config
    except Exception:
        return {}

    try:
        cfg = load_config() or {}
    except Exception:
        return {}

    obs = cfg.get("obsidian") or {}
    if not isinstance(obs, dict):
        return {}
    return obs


def is_enabled() -> bool:
    """True iff the user has configured a vault and turned the bridge on."""
    cfg = _read_obsidian_config()
    if not cfg.get("enabled", False):
        return False
    vault = cfg.get("vault_path") or ""
    return bool(vault and Path(os.path.expanduser(str(vault))).is_dir())


def get_vault_path() -> Optional[Path]:
    """Resolve the configured vault path.  Returns None when unset/missing.

    The path is ``expanduser``-ed so ``~/Obsidian/Vault`` works.  We do
    *not* resolve symlinks — the user's vault may legitimately contain
    them and resolving would break path containment checks below.
    """
    cfg = _read_obsidian_config()
    raw = cfg.get("vault_path") or ""
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_dir():
        return None
    return path


def get_search_scope() -> str:
    """Return the configured scope ('hermes_subdir' | 'ingest' | 'all')."""
    cfg = _read_obsidian_config()
    scope = str(cfg.get("search_scope", SCOPE_HERMES_SUBDIR)).strip().lower()
    return scope if scope in VALID_SCOPES else SCOPE_HERMES_SUBDIR


def get_hermes_dir() -> Optional[Path]:
    """Return ``<vault>/hermes/`` (creating it if missing).  None when no vault."""
    vault = get_vault_path()
    if vault is None:
        return None
    target = vault / HERMES_SUBDIR
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create %s: %s", target, exc)
        return None
    return target


def get_profile_subdir() -> Optional[Path]:
    """Return per-profile staging area inside the vault.

    Multiple profiles (``hermes -p coder`` etc.) coexist in the same vault
    by getting their own ``hermes/profiles/<name>/`` subdirectory.  The
    default profile uses ``hermes/`` directly so the common case stays
    flat.
    """
    base = get_hermes_dir()
    if base is None:
        return None
    profile = _active_profile_name()
    if profile in ("", "default", None):
        return base
    sub = base / "profiles" / profile
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def _active_profile_name() -> str:
    """Best-effort profile name resolution; falls back to 'default'."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


# ---------------------------------------------------------------------------
# Path containment helpers
# ---------------------------------------------------------------------------

def _is_inside(path: Path, root: Path) -> bool:
    """Whether *path* (resolved) is contained within *root* (resolved).

    Used as the gatekeeper for any tool/CLI write to make sure ``../`` and
    absolute paths can't escape the vault.  ``os.path.realpath`` to defeat
    symlink-based escapes.
    """
    try:
        path_real = Path(os.path.realpath(path))
        root_real = Path(os.path.realpath(root))
    except OSError:
        return False
    try:
        path_real.relative_to(root_real)
        return True
    except ValueError:
        return False


def _resolve_inside_vault(relpath: str) -> Optional[Path]:
    """Resolve *relpath* under the vault, refusing escapes.

    Returns the absolute Path on success, None on any safety violation
    (no vault configured, escape attempt, empty path, etc.).
    """
    vault = get_vault_path()
    if vault is None:
        return None
    if not relpath:
        return None
    if relpath.startswith("/"):
        return None
    if ".." in Path(relpath).parts:
        return None
    candidate = vault / relpath
    if not _is_inside(candidate, vault):
        return None
    return candidate


def _resolve_inside_hermes_subdir(relpath: str) -> Optional[Path]:
    """Same as ``_resolve_inside_vault`` but constrained to ``vault/hermes/``."""
    hermes_dir = get_hermes_dir()
    if hermes_dir is None:
        return None
    if not relpath or relpath.startswith("/") or ".." in Path(relpath).parts:
        return None
    candidate = hermes_dir / relpath
    if not _is_inside(candidate, hermes_dir):
        return None
    return candidate


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@dataclass
class SearchHit:
    relpath: str  # path relative to vault root
    line: int
    preview: str


def _search_roots(scope: str) -> List[Path]:
    """Return the directories to search for the given scope."""
    vault = get_vault_path()
    if vault is None:
        return []
    if scope == SCOPE_ALL:
        return [vault]
    hermes_dir = get_hermes_dir()
    if hermes_dir is None:
        return []
    if scope == SCOPE_HERMES_SUBDIR:
        return [hermes_dir]
    if scope == SCOPE_INGEST:
        ingest = hermes_dir / INGEST_DIRNAME
        return [ingest] if ingest.is_dir() else []
    return [hermes_dir]


def _ripgrep_available() -> bool:
    """True if the ``rg`` binary is on $PATH."""
    import shutil
    return shutil.which("rg") is not None


def _grep_with_rg(
    query: str, roots: List[Path], vault: Path, max_results: int
) -> List[SearchHit]:
    cmd = [
        "rg",
        "--no-heading",
        "--line-number",
        "--max-count", "5",
        "--max-columns", "300",
        "--smart-case",
        "--type", "md",
        "--type-add", "md:*.markdown",
        "--type-add", "md:*.txt",
        "--type-add", "md:*.rst",
        "--type-add", "md:*.org",
        "--",
        query,
    ] + [str(r) for r in roots]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("ripgrep failed: %s", exc)
        return []

    hits: List[SearchHit] = []
    if not result.stdout:
        return hits
    for line in result.stdout.splitlines():
        # Format: <abs_path>:<lineno>:<text>
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path_str, lineno_str, text = parts
        try:
            relpath = str(Path(path_str).relative_to(vault))
        except ValueError:
            relpath = path_str
        try:
            lineno = int(lineno_str)
        except ValueError:
            lineno = 0
        hits.append(SearchHit(
            relpath=relpath,
            line=lineno,
            preview=text.strip()[:200],
        ))
        if len(hits) >= max_results:
            break
    return hits


def _grep_python_fallback(
    query: str, roots: List[Path], vault: Path, max_results: int
) -> List[SearchHit]:
    """Pure-Python case-insensitive substring search."""
    hits: List[SearchHit] = []
    q_lower = query.lower()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            # Skip dotfiles, build artefacts, hidden Obsidian internals
            parts = path.relative_to(root).parts
            if any(
                p.startswith(".") or p in {"__pycache__", "node_modules", ".obsidian"}
                for p in parts
            ):
                continue
            if path.suffix.lower() not in READABLE_EXTS:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for n, line in enumerate(fh, 1):
                        if q_lower in line.lower():
                            try:
                                relpath = str(path.relative_to(vault))
                            except ValueError:
                                relpath = str(path)
                            hits.append(SearchHit(
                                relpath=relpath,
                                line=n,
                                preview=line.strip()[:200],
                            ))
                            if len(hits) >= max_results:
                                return hits
            except OSError:
                continue
    return hits


def search(
    query: str, *, max_results: int = 10, scope: Optional[str] = None
) -> List[SearchHit]:
    """Search the vault for *query*.  Scope-restricted by default."""
    if not query or not query.strip():
        return []
    max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    vault = get_vault_path()
    if vault is None:
        return []
    use_scope = (scope or get_search_scope()).strip().lower()
    if use_scope not in VALID_SCOPES:
        use_scope = SCOPE_HERMES_SUBDIR
    roots = _search_roots(use_scope)
    if not roots:
        return []
    if _ripgrep_available():
        return _grep_with_rg(query, roots, vault, max_results)
    return _grep_python_fallback(query, roots, vault, max_results)


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

def view(
    relpath: str, *, offset: int = 1, limit: int = 200, scope: Optional[str] = None
) -> dict:
    """Read a file from the vault.  Returns a dict with content + metadata.

    Refuses paths that fall outside the configured scope.  ``offset`` is
    1-based to match the rest of hermes' file-reading APIs.
    """
    if not relpath:
        return {"success": False, "error": "relpath is required"}

    target = _resolve_inside_vault(relpath)
    if target is None:
        return {
            "success": False,
            "error": f"Path '{relpath}' is not inside the vault (refusing to read).",
        }

    use_scope = (scope or get_search_scope()).strip().lower()
    if use_scope not in VALID_SCOPES:
        use_scope = SCOPE_HERMES_SUBDIR
    if use_scope != SCOPE_ALL:
        roots = _search_roots(use_scope)
        if not any(_is_inside(target, root) for root in roots):
            return {
                "success": False,
                "error": (
                    f"Path '{relpath}' is outside the active search scope "
                    f"('{use_scope}'). Configure obsidian.search_scope: all "
                    f"in config.yaml to broaden access."
                ),
            }

    if not target.is_file():
        return {"success": False, "error": f"Not a file (or does not exist): {relpath}"}

    try:
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except OSError as exc:
        return {"success": False, "error": f"Read failed: {exc}"}

    total_lines = len(all_lines)
    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 2000))
    start = offset - 1
    end = min(start + limit, total_lines)
    chunk = "".join(all_lines[start:end])
    if len(chunk) > MAX_VIEW_CHARS:
        chunk = chunk[:MAX_VIEW_CHARS] + "\n[...truncated]"

    return {
        "success": True,
        "path": relpath,
        "offset": offset,
        "lines_returned": end - start,
        "total_lines": total_lines,
        "content": chunk,
        "more_available": end < total_lines,
    }


# ---------------------------------------------------------------------------
# Save (agent writes)
# ---------------------------------------------------------------------------

def save(
    relpath: str, content: str, *, mode: str = "write", subdir: str = NOTES_DIRNAME
) -> dict:
    """Write *content* to ``vault/hermes/<subdir>/<relpath>``.

    The agent never writes outside ``vault/hermes/`` — the user's own notes
    are sacred.  ``subdir`` defaults to ``notes/`` (the agent's free-form
    write area); pass ``subdir=""`` to write directly under ``hermes/``
    (used by export operations like rules mirror).

    ``mode``: ``write`` overwrites, ``append`` appends.
    """
    if not relpath:
        return {"success": False, "error": "relpath is required"}
    if mode not in ("write", "append"):
        return {"success": False, "error": "mode must be 'write' or 'append'"}
    if content is None:
        return {"success": False, "error": "content is required"}
    if len(content) > MAX_SAVE_CHARS:
        return {
            "success": False,
            "error": f"content exceeds {MAX_SAVE_CHARS:,} char cap",
        }

    base_relpath = f"{subdir}/{relpath}" if subdir else relpath
    target = _resolve_inside_hermes_subdir(base_relpath)
    if target is None:
        return {
            "success": False,
            "error": (
                f"Path '{relpath}' resolves outside vault/hermes/ "
                f"(refusing to write — the agent only writes inside hermes/)."
            ),
        }

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
    except OSError as exc:
        return {"success": False, "error": f"Write failed: {exc}"}

    vault = get_vault_path() or target.parent
    try:
        rel_to_vault = str(target.relative_to(vault))
    except ValueError:
        rel_to_vault = str(target)

    return {
        "success": True,
        "path": rel_to_vault,
        "abs_path": str(target),
        "mode": mode,
        "bytes_written": written,
        "message": (
            f"{action.capitalize()} {written} chars to {rel_to_vault}. "
            f"Visible in Obsidian on next vault refresh."
        ),
    }


# ---------------------------------------------------------------------------
# Export (hermes → vault)  — used by `hermes obsidian export` and the
# session-end hook.  Not part of the agent's tool surface.
# ---------------------------------------------------------------------------

@dataclass
class ExportResult:
    files_written: List[str]
    skipped: List[str]
    error: Optional[str] = None


def export_memory_files(*, include_user: bool = True) -> ExportResult:
    """Mirror ``RULES.md`` / ``MEMORY.md`` / ``USER.md`` into the vault.

    Each mirror file gets the ``HERMES_MANAGED_MARKER`` so the user knows
    not to edit it (changes will be overwritten on the next export).  The
    real file names live under the per-profile staging dir so multiple
    profiles can share one vault without colliding.
    """
    base = get_profile_subdir()
    if base is None:
        return ExportResult(files_written=[], skipped=[], error="vault not configured")

    files_written: List[str] = []
    skipped: List[str] = []

    mem_dir = get_hermes_home() / "memories"
    pairs = [
        ("RULES.md", EXPORT_RULES_FILENAME),
        ("MEMORY.md", EXPORT_MEMORY_FILENAME),
    ]
    if include_user:
        pairs.append(("USER.md", EXPORT_USER_FILENAME))

    for src_name, dst_name in pairs:
        src = mem_dir / src_name
        dst = base / dst_name
        if not src.exists():
            skipped.append(src_name)
            continue
        try:
            body = src.read_text(encoding="utf-8")
        except OSError as exc:
            skipped.append(f"{src_name} (read error: {exc})")
            continue
        rendered = _render_export(src_name, body)
        try:
            dst.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            skipped.append(f"{dst_name} (write error: {exc})")
            continue
        files_written.append(str(dst))

    return ExportResult(files_written=files_written, skipped=skipped, error=None)


def _render_export(src_name: str, body: str) -> str:
    """Wrap a mirrored memory file in a hermes-managed banner."""
    title = {
        "RULES.md": "Hermes Rules",
        "MEMORY.md": "Hermes Memory",
        "USER.md": "Hermes — User Profile",
    }.get(src_name, src_name)
    from datetime import datetime, timezone
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        f"---\n"
        f"hermes-managed: true\n"
        f"hermes-source: {src_name}\n"
        f"hermes-exported-at: {when}\n"
        f"---\n"
        f"{HERMES_MANAGED_MARKER}\n\n"
        f"# {title}\n\n"
        f"{body.rstrip()}\n"
    )


def export_learnings(limit: int = 200) -> ExportResult:
    """Export learning_store entries (one Markdown file per LRN id).

    Useful for users who want to see what hermes has been auto-learning in
    their note app.  Each file is named ``LRN-YYYYMMDD-XXXXXX.md`` and
    carries the entry's stats in YAML frontmatter.
    """
    base = get_profile_subdir()
    if base is None:
        return ExportResult(files_written=[], skipped=[], error="vault not configured")

    try:
        from agent.learning_store import LearningStore
    except Exception as exc:
        return ExportResult(files_written=[], skipped=[], error=f"learning_store unavailable: {exc}")

    learnings_dir = base / LEARNINGS_DIRNAME
    learnings_dir.mkdir(parents=True, exist_ok=True)

    store = LearningStore()
    try:
        entries = store.list(status="all", limit=limit)
    except Exception as exc:
        return ExportResult(files_written=[], skipped=[], error=str(exc))
    finally:
        try:
            store.close()
        except Exception:
            pass

    files_written: List[str] = []
    skipped: List[str] = []
    for entry in entries:
        lrn_id = str(entry.get("id") or "").strip()
        if not lrn_id:
            skipped.append("<entry without id>")
            continue
        path = learnings_dir / f"{lrn_id}.md"
        body = _render_learning(entry)
        try:
            path.write_text(body, encoding="utf-8")
            files_written.append(str(path))
        except OSError as exc:
            skipped.append(f"{lrn_id} (write error: {exc})")
    return ExportResult(files_written=files_written, skipped=skipped, error=None)


def _render_learning(entry: dict) -> str:
    """Render one learning_store entry as a Markdown note."""
    from datetime import datetime, timezone

    def _iso(ts):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except Exception:
            return ""

    fm_lines = ["---", "hermes-managed: true", "hermes-kind: learning"]
    for key in (
        "id", "category", "subcategory", "pattern_key", "priority",
        "status", "area", "recurrence_count", "distinct_tasks",
        "promoted_to",
    ):
        val = entry.get(key)
        if val is None or val == "":
            continue
        fm_lines.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
    if entry.get("first_seen"):
        fm_lines.append(f"first_seen: {_iso(entry['first_seen'])}")
    if entry.get("last_seen"):
        fm_lines.append(f"last_seen: {_iso(entry['last_seen'])}")
    fm_lines.append("---")
    fm_lines.append(HERMES_MANAGED_MARKER)
    fm_lines.append("")
    fm_lines.append(f"# {entry.get('summary') or entry.get('id') or 'Learning'}")
    fm_lines.append("")
    if entry.get("details"):
        fm_lines.append("## Details")
        fm_lines.append("")
        fm_lines.append(str(entry["details"]).rstrip())
        fm_lines.append("")
    if entry.get("suggested_action"):
        fm_lines.append("## Suggested action")
        fm_lines.append("")
        fm_lines.append(str(entry["suggested_action"]).rstrip())
        fm_lines.append("")
    if entry.get("resolution_notes"):
        fm_lines.append("## Resolution notes")
        fm_lines.append("")
        fm_lines.append(str(entry["resolution_notes"]).rstrip())
        fm_lines.append("")
    return "\n".join(fm_lines) + "\n"


# ---------------------------------------------------------------------------
# Import (vault → hermes)
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    rules_added: List[str]
    rules_skipped: List[str]
    notes_imported: List[str]
    error: Optional[str] = None


_RULE_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+)$")


def import_rules_from_staging(*, store=None) -> ImportResult:
    """Pull pending rules from ``vault/hermes/rules-staging.md`` into RULES.md.

    Each non-empty bullet (``- ...`` or ``* ...``) and each non-bullet
    non-blank line becomes a rule entry, recorded with
    ``source='obsidian-import'`` so the rules-lifecycle layer can age it
    distinctly from manually-typed rules.

    After a successful import we **truncate** the staging file (rather than
    delete) so the user has a stable place to keep adding new pending
    rules between sessions.

    ``store`` is the live ``MemoryStore``; if None, we construct a fresh
    one bound to the current profile.
    """
    base = get_profile_subdir()
    if base is None:
        return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                            error="vault not configured")
    staging = base / STAGING_RULES_FILENAME
    if not staging.exists():
        return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                            error=None)

    try:
        text = staging.read_text(encoding="utf-8")
    except OSError as exc:
        return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                            error=f"read error: {exc}")

    # A small state machine — staging files are user-edited markdown, so
    # we have to be tolerant of YAML frontmatter, multi-line HTML comments,
    # headings, and stray separators.  Anything that survives the filters
    # is treated as a candidate rule.
    candidates: List[str] = []
    in_frontmatter = False
    in_html_comment = False
    seen_first_non_blank = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # YAML frontmatter:  must be the very first non-blank token, opened
        # and closed by a literal ``---`` on its own line.  Lines in
        # between are dropped wholesale.
        if line == "---":
            if not seen_first_non_blank:
                in_frontmatter = True
                seen_first_non_blank = True
                continue
            if in_frontmatter:
                in_frontmatter = False
                continue
            # Lone ``---`` outside frontmatter — treat as a separator
            seen_first_non_blank = True
            continue
        if in_frontmatter:
            seen_first_non_blank = True
            continue
        seen_first_non_blank = True

        # HTML comments — single-line and multi-line both handled.  We
        # detect ``<!--`` and ``-->`` substrings rather than only at line
        # boundaries, so partial-line comments work too.
        if in_html_comment:
            if "-->" in line:
                in_html_comment = False
            continue
        if "<!--" in line and "-->" not in line:
            in_html_comment = True
            continue
        if line.startswith("<!--") and line.endswith("-->"):
            continue

        # Markdown headings — any level.
        if line.startswith("#"):
            continue

        m = _RULE_BULLET_RE.match(raw)
        if m:
            candidates.append(m.group(1).strip())
        else:
            candidates.append(line)

    if not candidates:
        return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                            error=None)

    if store is None:
        try:
            from tools.memory_tool import MemoryStore
            store = MemoryStore()
            store.load_from_disk()
        except Exception as exc:
            return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                                error=f"memory store unavailable: {exc}")

    added: List[str] = []
    skipped: List[str] = []
    for entry in candidates:
        try:
            res = store.add_rule_with_lifecycle(
                text=entry,
                pinned=False,
                source="obsidian-import",
                pattern_key="obsidian-import",
            )
            if res.get("success"):
                added.append(entry)
            else:
                skipped.append(f"{entry[:60]}: {res.get('error', 'unknown error')}")
        except Exception as exc:
            skipped.append(f"{entry[:60]}: {exc}")

    # Truncate staging only if we successfully imported something — leave
    # rejected bullets in place so the user can fix them.
    if added and not skipped:
        try:
            staging.write_text(
                "<!-- Add new rules below as bullet points; "
                "they will be imported into RULES.md on next "
                "`hermes obsidian import-rules`. -->\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    return ImportResult(rules_added=added, rules_skipped=skipped,
                        notes_imported=[], error=None)


def list_ingest_files() -> List[Path]:
    """Files under ``vault/hermes/ingest/`` that hermes will treat as input."""
    base = get_hermes_dir()
    if base is None:
        return []
    ingest = base / INGEST_DIRNAME
    if not ingest.is_dir():
        return []
    out: List[Path] = []
    for path in sorted(ingest.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in READABLE_EXTS:
            continue
        out.append(path)
    return out


def import_notes_to_pk(project: str, *, source_dir: Optional[Path] = None) -> ImportResult:
    """Copy notes from a vault folder into the project-knowledge tree.

    Used by ``hermes pk import-from-vault <project> <vault-folder>``.
    Only files with extensions in ``READABLE_EXTS`` are copied; the
    relative directory structure is preserved.
    """
    from agent.project_knowledge import get_project_dir

    if not project:
        return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                            error="project name required")

    if source_dir is None:
        base = get_hermes_dir()
        if base is None:
            return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                                error="vault not configured")
        source_dir = base / INGEST_DIRNAME

    src = Path(source_dir)
    if not src.is_dir():
        return ImportResult(rules_added=[], rules_skipped=[], notes_imported=[],
                            error=f"source dir not found: {source_dir}")

    pk_dir = get_project_dir(project)
    pk_dir.mkdir(parents=True, exist_ok=True)

    imported: List[str] = []
    skipped: List[str] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in READABLE_EXTS:
            continue
        try:
            relpath = path.relative_to(src)
        except ValueError:
            skipped.append(str(path))
            continue
        target = pk_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(path.read_bytes())
            imported.append(str(relpath))
        except OSError as exc:
            skipped.append(f"{relpath} (error: {exc})")

    return ImportResult(rules_added=[], rules_skipped=skipped,
                        notes_imported=imported, error=None)


# ---------------------------------------------------------------------------
# Status (used by `hermes obsidian status` and `/obsidian` slash command)
# ---------------------------------------------------------------------------

def status() -> dict:
    """High-level vault status.  Cheap; safe to call on hot paths."""
    cfg = _read_obsidian_config()
    enabled = bool(cfg.get("enabled", False))
    vault = get_vault_path()
    info: dict = {
        "enabled": enabled,
        "vault_path": str(vault) if vault else (cfg.get("vault_path") or ""),
        "vault_exists": vault is not None,
        "search_scope": get_search_scope(),
    }
    if vault is None:
        return info

    hermes_dir = get_hermes_dir()
    info["hermes_dir"] = str(hermes_dir) if hermes_dir else ""

    if hermes_dir:
        ingest = hermes_dir / INGEST_DIRNAME
        info["ingest_files"] = sum(
            1 for _ in (ingest.rglob("*") if ingest.is_dir() else [])
            if _.is_file() and _.suffix.lower() in READABLE_EXTS
        )
        staging = (get_profile_subdir() or hermes_dir) / STAGING_RULES_FILENAME
        info["staging_pending_lines"] = 0
        if staging.exists():
            try:
                lines = staging.read_text(encoding="utf-8").splitlines()
                info["staging_pending_lines"] = sum(
                    1 for raw in lines
                    if raw.strip()
                    and not raw.strip().startswith("#")
                    and not raw.strip().startswith("---")
                    and not raw.strip().startswith("<!--")
                )
            except OSError:
                pass
    return info
