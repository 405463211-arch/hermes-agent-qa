"""Project Knowledge — first-class reference-data store per project.

Hermes already has three layers for runtime knowledge:

  - SOUL.md / RULES.md  — agent-level identity + mandatory rules
  - MEMORY.md / USER.md — bounded curated memory
  - skills/             — reusable procedural how-tos

What was missing: a place to put **project-specific reference data** that's
too large for memory and too data-heavy for a skill — distilled source code,
extracted i18n strings, page maps, API docs, schema dumps, etc.  Users had
been hand-creating ``~/.hermes/project-knowledge/<project>/`` directories
and pointing the agent at them with a SOUL.md note, but the framework
itself had no awareness of this directory.

This module makes that pattern an official feature:

  - Auto-detect the active project from cwd (git root basename).
  - Discover ``$HERMES_HOME/project-knowledge/<project>/`` if it exists.
  - Build a compact index that gets injected into the system prompt so the
    model knows the directory's high-level structure without reading
    everything.
  - Expose ``project_knowledge_search`` / ``project_knowledge_view`` /
    ``project_knowledge_save`` tools so the agent can query and grow the
    knowledge base on demand.

Design principles:

  - **Cache-friendly** — the index is built once per session, frozen into
    the system prompt, identical across turns.  Refreshes only on a new
    session start (same pattern as skills + memory).
  - **Lazy** — we never read full file contents into the prompt.  The
    index is just structure (paths + one-line summaries).  The agent
    pulls full content via the tool on demand.
  - **Bounded** — every prompt-side artefact has a hard char cap so a
    huge knowledge base can't bloat every API call.  Excess gets
    truncated with a "see project_knowledge_search to discover more"
    hint.
  - **Profile-aware** — uses ``get_hermes_home()`` so each profile has
    its own knowledge tree.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Hard cap on the system-prompt index size.  ~3000 chars keeps the cost
# below ~1100 tokens while still allowing a 30-50 entry overview.
DEFAULT_INDEX_MAX_CHARS = 3000

# How many files to surface in the index before truncating.  Tuned so a
# typical distilled repo (a few dozen page maps + a handful of overview
# docs) all fit comfortably; larger trees get truncated with a hint.
DEFAULT_INDEX_MAX_FILES = 60

# File extensions worth indexing (everything else is treated as raw data
# the agent should never auto-load — they still show up in search though).
INDEXABLE_EXTS = (".md", ".txt", ".rst", ".yaml", ".yml", ".json")

# Name of the directory under HERMES_HOME that holds all per-project
# knowledge trees.  Each project gets its own subdirectory.
PK_ROOT_DIRNAME = "project-knowledge"


@dataclass
class ProjectKnowledgeIndex:
    """In-memory representation of a project's knowledge tree."""

    project_name: str
    pk_dir: Path
    files: List["PKFileEntry"]
    truncated_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.files


@dataclass
class PKFileEntry:
    """One indexed file."""

    relpath: str            # path relative to pk_dir
    summary: str            # one-line summary (frontmatter description, # heading, or first non-blank line)
    size_bytes: int


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------

def detect_project_name(cwd: Optional[str] = None) -> str:
    """Return the active project name based on the working directory.

    Resolution order:
      1. The basename of the git repository root, if cwd is inside one.
      2. The basename of cwd itself, otherwise.
      3. ``"default"`` as the last-resort fallback (so the lookup is
         always deterministic and never raises).
    """
    base = cwd or os.getcwd()
    try:
        result = subprocess.run(
            ["git", "-C", base, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            root = (result.stdout or "").strip()
            if root:
                return Path(root).name
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # git not installed or hung — fall through to cwd-based detection.
        pass
    name = Path(base).name
    return name or "default"


def get_pk_root() -> Path:
    """Return the ``project-knowledge`` directory under the active HERMES_HOME."""
    return get_hermes_home() / PK_ROOT_DIRNAME


def get_project_dir(project_name: Optional[str] = None) -> Path:
    """Return the directory for *project_name* (auto-detect when None)."""
    name = project_name or detect_project_name()
    return get_pk_root() / name


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def _extract_summary(path: Path) -> str:
    """Pull a short human-readable summary from a markdown/text file.

    Tries (in order): YAML frontmatter ``description:``, the first ``#``
    heading, the first non-blank non-frontmatter line.  Falls back to
    an empty string when the file is binary or unreadable.
    """
    try:
        # Read just the head — full file would be expensive on a huge dump.
        text = path.read_text(encoding="utf-8", errors="ignore")[:2000]
    except (OSError, UnicodeDecodeError):
        return ""
    if not text.strip():
        return ""

    # YAML frontmatter description: ---\n...\ndescription: ...\n---\n
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            for line in fm.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("description:"):
                    desc = stripped.split(":", 1)[1].strip().strip("'\"")
                    if desc:
                        return desc[:140]
            text = text[end + 4:]

    # First markdown heading
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:140]
        return stripped[:140]
    return ""


def _iter_indexable_files(pk_dir: Path) -> Iterable[Path]:
    """Yield candidate files for the index, sorted by path for stability."""
    if not pk_dir.is_dir():
        return
    for path in sorted(pk_dir.rglob("*")):
        if not path.is_file():
            continue
        # Skip dotfiles and obvious build/cache dirs anywhere in the path
        parts = path.relative_to(pk_dir).parts
        if any(p.startswith(".") or p in {"__pycache__", "node_modules"} for p in parts):
            continue
        if path.suffix.lower() not in INDEXABLE_EXTS:
            continue
        yield path


def build_index(
    project_name: Optional[str] = None,
    *,
    max_files: int = DEFAULT_INDEX_MAX_FILES,
) -> ProjectKnowledgeIndex:
    """Scan the project knowledge directory and build a fresh index.

    Returns an index with ``is_empty=True`` when no PK directory exists
    for the active project — the caller should treat that as "no index
    block to inject" and skip silently.
    """
    project = project_name or detect_project_name()
    pk_dir = get_project_dir(project)

    if not pk_dir.is_dir():
        return ProjectKnowledgeIndex(project_name=project, pk_dir=pk_dir, files=[])

    files: List[PKFileEntry] = []
    truncated = 0
    for path in _iter_indexable_files(pk_dir):
        if len(files) >= max_files:
            truncated += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files.append(PKFileEntry(
            relpath=str(path.relative_to(pk_dir)),
            summary=_extract_summary(path),
            size_bytes=size,
        ))

    return ProjectKnowledgeIndex(
        project_name=project,
        pk_dir=pk_dir,
        files=files,
        truncated_count=truncated,
    )


# ---------------------------------------------------------------------------
# System-prompt block
# ---------------------------------------------------------------------------

def render_index_block(
    index: ProjectKnowledgeIndex,
    *,
    max_chars: int = DEFAULT_INDEX_MAX_CHARS,
) -> str:
    """Render the index as a system-prompt block, truncating to ``max_chars``."""
    if index.is_empty:
        return ""

    header = (
        f"## Project Knowledge: {index.project_name}\n"
        f"Reference data for this project lives in {index.pk_dir}. "
        f"Use `project_knowledge_search` to find specific facts and "
        f"`project_knowledge_view` to read full files. Do NOT auto-read "
        f"every file — these are large reference dumps, treat them like a "
        f"library you query on demand.\n"
        f"<knowledge_index>\n"
    )

    lines = []
    used = len(header)
    files_shown = 0
    for entry in index.files:
        line = (
            f"  - {entry.relpath}"
            + (f": {entry.summary}" if entry.summary else "")
        )
        # +1 for newline
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        files_shown += 1

    body = "\n".join(lines)
    not_shown = max(
        0, len(index.files) - files_shown
    ) + index.truncated_count

    footer_extra = ""
    if not_shown > 0:
        footer_extra = (
            f"\n  (... {not_shown} more file(s) — use "
            f"project_knowledge_search to discover them)"
        )

    return f"{header}{body}{footer_extra}\n</knowledge_index>"
