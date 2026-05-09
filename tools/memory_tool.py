#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. THREE stores
(layered by stability and authority):

  - RULES.md: agent rules / red lines / mandatory protocols.
    Highest priority, smallest churn. Things the user said "always do this" /
    "never do that". Survives compression. Loaded at the very top of the
    system prompt next to identity. Use sparingly — every entry costs every
    future turn.
  - MEMORY.md: agent's personal notes and observations (environment facts,
    project conventions, tool quirks, things learned). Medium churn. When
    full, oldest entries are auto-archived to LCM (if available) and
    recoverable via lcm_search.
  - USER.md: what the agent knows about the user (preferences, communication
    style, expectations, workflow habits).

All three are injected into the system prompt as a frozen snapshot at session
start. Mid-session writes update files on disk immediately (durable) but do
NOT change the system prompt -- this preserves the prefix cache for the
entire session. The snapshot refreshes on the next session start (or after
context compression triggers a system-prompt rebuild).

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove
- target parameter selects which store: rules / memory / user
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
- Optional LCM bridge: when MEMORY.md fills up, oldest entries auto-archive to
  the LCM long-context store (if installed), keeping the active bucket lean
  while preserving the knowledge for retrieval.
"""

import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Any, Dict, List, Optional

from utils import atomic_replace

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Where memory files live — resolved dynamically so profile overrides
# (HERMES_HOME env var changes) are always respected.  The old module-level
# constant was cached at import time and could go stale if a profile switch
# happened after the first import.
def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

ENTRY_DELIMITER = "\n§\n"


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
# ---------------------------------------------------------------------------

_MEMORY_THREAT_PATTERNS = [
    # Prompt injection
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    # Exfiltration via curl/wget with secrets
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    # Persistence via shell rc
    (r'authorized_keys', "ssh_backdoor"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env"),
]

# Subset of invisible chars for injection detection
_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    # Check invisible unicode
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X} (possible injection)."

    # Check threat patterns
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'. Memory entries are injected into the system prompt and must not contain injection or exfiltration payloads."

    return None


VALID_TARGETS = ("rules", "memory", "user")

# Minimum count of "substantive" characters (letters, digits, CJK ideographs)
# required for a memory entry. Pure-punctuation or single-char entries like
# ``.`` and ``.``` have shown up in production memory files (typically when
# an LLM emits a fallback empty-token response that the tool used to accept).
# These pollute the system prompt and serve no purpose, so we reject them.
# Five chars passes "Lefty"/"我习惯" while rejecting "."/"-"/"。。。".
_MIN_SUBSTANTIVE_CHARS = 3


def _count_substantive_chars(text: str) -> int:
    """Count alphanumeric + CJK characters in ``text``.

    Used by ``MemoryStore.add`` to reject entries that consist entirely of
    whitespace, punctuation, or symbols. CJK range covers the basic
    Unified Ideographs block — sufficient for Chinese / Japanese kanji
    inputs and avoids pulling in ``unicodedata`` for one check.
    """
    return sum(
        1
        for ch in text
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    )


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Three layered targets, in order of authority:
      - rules:  RULES.md  — red lines / mandatory protocols (highest priority)
      - memory: MEMORY.md — agent's working notes and learned facts
      - user:   USER.md   — user profile, preferences, communication style

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt
        injection. Never mutated mid-session. Keeps prefix cache stable.
      - rules_entries / memory_entries / user_entries: live state, mutated by
        tool calls, persisted to disk. Tool responses always reflect live state.

    Optional LCM bridge: pass an ``lcm_engine`` to enable auto-archiving of the
    oldest MEMORY.md entries when it fills up.  USER.md and RULES.md never
    auto-archive — they're considered authoritative by the user and must be
    pruned explicitly.
    """

    def __init__(
        self,
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
        rules_char_limit: int = 4000,
        lcm_engine: Any = None,
        # ── Auto-archive (rules-lifecycle) ──────────────────────────────
        # Disabled by default at the constructor level so unit tests stay
        # deterministic; the production code path passes the config values
        # through from run_agent.py:_create_memory_store.
        auto_archive_rules: bool = False,
        auto_archive_capacity_threshold: float = 0.80,
        auto_archive_age_days: int = 90,
        auto_archive_recurrence_window: int = 30,
        trial_new_marker_days: int = 7,
    ):
        self.rules_entries: List[str] = []
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.rules_char_limit = rules_char_limit
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Optional LCM long-context engine for memory overflow archiving.
        # Set to None to disable archiving; add() will then return the
        # original "exceeds limit" error when memory is full.
        self._lcm_engine = lcm_engine
        # Tracks how many entries were archived in the current session — used
        # by the tool dispatcher to surface a one-line note to the model.
        self._archived_this_session: int = 0
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {
            "rules": "", "memory": "", "user": "",
        }
        # Rules-lifecycle settings.  See agent/rules_lifecycle.py for the
        # exact semantics of each knob.  ``_pending_archive_notice`` accumulates
        # auto-archive results during load_from_disk for the runner / CLI to
        # surface to the user once per session.
        self.auto_archive_rules_enabled = bool(auto_archive_rules)
        self.auto_archive_capacity_threshold = float(auto_archive_capacity_threshold)
        self.auto_archive_age_days = int(auto_archive_age_days)
        self.auto_archive_recurrence_window = int(auto_archive_recurrence_window)
        self.trial_new_marker_days = int(trial_new_marker_days)
        self._pending_archive_notice: List[Dict[str, Any]] = []

    # -- LCM integration --------------------------------------------------

    def attach_lcm(self, lcm_engine: Any) -> None:
        """Bind an LCM engine for memory-overflow archiving.

        Safe to call after construction (e.g. when the LCM plugin loads
        later in the agent boot sequence).  Pass ``None`` to detach.
        """
        self._lcm_engine = lcm_engine

    def load_from_disk(self):
        """Load entries from RULES.md / MEMORY.md / USER.md, capture snapshots."""
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.rules_entries = self._read_file(mem_dir / "RULES.md")
        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.rules_entries = list(dict.fromkeys(self.rules_entries))
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "rules": self._render_block("rules", self.rules_entries),
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }
        # New session — reset the archive counter
        self._archived_this_session = 0

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")

        fd = open(lock_path, "r+" if msvcrt else "a+")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        if target == "rules":
            return mem_dir / "RULES.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        """
        fresh = self._read_file(self._path_for(target))
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        if target == "rules":
            return self.rules_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        elif target == "rules":
            self.rules_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        if target == "rules":
            return self.rules_char_limit
        return self.memory_char_limit

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit.

        For ``target='memory'`` only: if an LCM engine is attached and the new
        entry would exceed the limit, oldest entries are auto-archived to LCM
        (recoverable via ``lcm_search``) until the new entry fits. RULES.md
        and USER.md never auto-archive — user-curated content is too valuable
        to silently move out of the prompt.
        """
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Reject pure-punctuation / single-char entries before they pollute
        # the system prompt. See ``_count_substantive_chars`` above for the
        # rationale and the production "." entry that motivated this check.
        if _count_substantive_chars(content) < _MIN_SUBSTANTIVE_CHARS:
            return {
                "success": False,
                "error": (
                    f"Content lacks substantive characters "
                    f"(need at least {_MIN_SUBSTANTIVE_CHARS} letters/digits/"
                    f"CJK chars; got {_count_substantive_chars(content)}). "
                    f"Memory entries must be meaningful prose, not "
                    f"placeholders like '.' or '---'."
                ),
            }

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            # Re-read from disk under lock to pick up writes from other sessions
            self._reload_target(target)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            archived_now: List[str] = []
            if new_total > limit:
                # Memory-only auto-archive path
                if target == "memory" and self._lcm_engine is not None:
                    archived_now = self._archive_oldest_to_lcm_locked(
                        entries, content_chars=len(content)
                    )
                    self._set_entries(target, entries)
                    new_entries = entries + [content]
                    new_total = len(ENTRY_DELIMITER.join(new_entries))

                if new_total > limit:
                    current = self._char_count(target)
                    return {
                        "success": False,
                        "error": (
                            f"{target.upper()} at {current:,}/{limit:,} chars. "
                            f"Adding this entry ({len(content)} chars) would exceed the limit. "
                            f"Replace or remove existing entries first."
                        ),
                        "current_entries": entries,
                        "usage": f"{current:,}/{limit:,}",
                    }

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        message = "Entry added."
        if archived_now:
            self._archived_this_session += len(archived_now)
            message = (
                f"Entry added. Auto-archived {len(archived_now)} oldest "
                f"MEMORY entry/entries to LCM (use lcm_search to recall)."
            )
        response = self._success_response(target, message)
        if archived_now:
            response["archived_to_lcm"] = archived_now
        return response

    # -- LCM overflow ---------------------------------------------------------

    def _archive_oldest_to_lcm_locked(
        self, entries: List[str], content_chars: int
    ) -> List[str]:
        """Archive oldest MEMORY entries to LCM until ``content_chars`` fits.

        Caller must hold the MEMORY.md file lock and pass the live entries
        list — we mutate it in-place (popping from the front).  Returns a
        list of preview strings for the entries that were archived, so the
        tool response can surface them to the model.

        Failures (LCM engine not initialised, no active session, embedder
        unavailable) are swallowed and we just stop archiving — the caller
        falls back to the regular "memory full" error in that case.
        """
        archived_previews: List[str] = []
        if self._lcm_engine is None or not entries:
            return archived_previews

        # Need: room for current entries + content + delimiters
        # Pop from index 0 (oldest) while we'd still be over budget.
        try:
            store = self._lcm_engine._ensure_store()  # type: ignore[attr-defined]
            embedder = self._lcm_engine._ensure_embedder()  # type: ignore[attr-defined]
            session_id = (
                getattr(self._lcm_engine, "_session_id", "")
                or "memory-archive"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("LCM unavailable for memory archive: %s", e)
            return archived_previews

        limit = self.memory_char_limit
        # Estimate: every additional entry adds delimiter + chars
        # Stop once entries + content_chars + (n-1)*delim_len <= limit
        delim = len(ENTRY_DELIMITER)
        archive_session = f"memory:{session_id}"
        while entries:
            projected = (
                sum(len(e) for e in entries)
                + delim * max(0, len(entries) - 1)
                + delim
                + content_chars
            )
            if projected <= limit:
                break
            evicted = entries.pop(0)
            try:
                emb_array = embedder.embed([evicted])  # shape (1, dim)
                store.add(
                    session_id=archive_session,
                    chunks=[{
                        "role": "memory_archive",
                        "content": evicted,
                        "chunk_type": "memory_archive",
                    }],
                    embeddings=emb_array,
                    embedder_name=embedder.name,
                )
                preview = (evicted[:80] + "...") if len(evicted) > 80 else evicted
                archived_previews.append(preview)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "LCM archive failed for evicted entry (re-inserting): %s", e
                )
                # Re-insert at front — we couldn't safely archive it.
                entries.insert(0, evicted)
                break

        return archived_previews

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Apply the same substantive-content gate as ``add`` so a callsite
        # can't sneak ``.``-style junk past via replace.
        if _count_substantive_chars(new_content) < _MIN_SUBSTANTIVE_CHARS:
            return {
                "success": False,
                "error": (
                    f"new_content lacks substantive characters "
                    f"(need at least {_MIN_SUBSTANTIVE_CHARS} letters/digits/"
                    f"CJK chars; got {_count_substantive_chars(new_content)})."
                ),
            }

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content or remove other entries first."
                    ),
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    # ── Memory lifecycle (lightweight) ──────────────────────────────────────
    #
    # MEMORY.md doesn't go through the full pinned/[NEW]/auto-archive loop —
    # it's a free-form scratchpad.  But entries CAN carry optional Pattern-Key
    # / created metadata so the ``/memory review`` command can find dormant
    # entries.  Legacy entries without metadata default to ``stable+manual``
    # with no created timestamp (so they're never auto-flagged as stale).

    def add_memory_with_lifecycle(
        self,
        text: str,
        *,
        pattern_key: str = "",
        source: str = "manual",
    ) -> Dict[str, Any]:
        """Append a MEMORY.md entry tagged with optional lifecycle metadata.

        ``pattern_key`` is purely informational at this layer — there's no
        dedupe or auto-promotion (that's what ``learning_record`` is for).
        It exists so downstream review commands (``/memory review``) can
        cluster related entries.

        ``source`` defaults to ``manual`` for human/agent-written notes.
        """
        from datetime import date as _date
        from agent.rules_lifecycle import RuleEntry, serialize_rule_entry

        text = (text or "").strip()
        if not text:
            return {"success": False, "error": "Memory text cannot be empty."}

        entry = RuleEntry(
            text=text,
            created=_date.today(),
            source=source or "manual",
            pattern_key=pattern_key or "",
        )
        # Reuse the rules serializer — it produces the same hermes-meta format.
        return self.add("memory", serialize_rule_entry(entry))

    def find_stale_memory_entries(self, *, age_days: int = 60) -> List[Dict[str, Any]]:
        """Return memory entries older than ``age_days`` that have lifecycle metadata.

        Legacy entries (no metadata) are skipped — we have no way to know
        their age.  Returns parsed dicts with ``text``, ``created``,
        ``pattern_key`` so the ``/memory review`` UI can present each.
        """
        from datetime import date as _date
        from agent.rules_lifecycle import parse_rule_entry

        today = _date.today()
        out: List[Dict[str, Any]] = []
        for raw in self.memory_entries:
            entry = parse_rule_entry(raw)
            if entry.created is None:
                continue
            age = (today - entry.created).days
            if age < age_days:
                continue
            out.append({
                "text": entry.text,
                "raw": raw,
                "created": entry.created.isoformat(),
                "pattern_key": entry.pattern_key,
                "age_days": age,
            })
        return out

    # ── Rules lifecycle: add with metadata + archive bookkeeping ────────────

    def add_rule_with_lifecycle(
        self,
        text: str,
        *,
        pinned: bool = False,
        source: str = "manual",
        recurrence: int = 0,
        pattern_key: str = "",
    ) -> Dict[str, Any]:
        """Add a rule entry with structured lifecycle metadata.

        Wraps ``text`` with an HTML-comment metadata block describing how the
        rule was born so the lifecycle layer can age and archive it later.

        ``source`` should be ``manual`` for user-authored rules or
        ``LRN-YYYYMMDD-XXX`` for rules promoted from a learning entry.  When
        ``source`` is an LRN id, ``promoted_at`` is set to today (so the
        ``[NEW]`` marker fires) and ``recurrence``/``pattern_key`` are
        recorded for future archive judgments.
        """
        from datetime import date as _date
        from agent.rules_lifecycle import (
            LEARNING_SOURCE_PREFIXES,
            RuleEntry,
            serialize_rule_entry,
        )

        text = (text or "").strip()
        if not text:
            return {"success": False, "error": "Rule text cannot be empty."}

        today = _date.today()
        is_promoted = any(
            (source or "").startswith(p) for p in LEARNING_SOURCE_PREFIXES
        )
        entry = RuleEntry(
            text=text,
            pinned=bool(pinned),
            created=today,
            source=source or "manual",
            promoted_at=today if is_promoted else None,
            recurrence=int(recurrence) if is_promoted else 0,
            pattern_key=(pattern_key or "") if is_promoted else "",
        )
        serialized = serialize_rule_entry(entry)
        # Delegate to the existing add() so we get the lock + size enforcement
        # for free; the only difference is we pass the serialized form.
        return self.add("rules", serialized)

    def run_auto_archive(self) -> List[Dict[str, Any]]:
        """Apply auto-archive policy to RULES.md right now.

        Loads the current rules under lock, asks the lifecycle module which
        ones to evict, writes the archived rules to RULES.archive.md (with
        archive metadata), pushes them into the LCM long-context store if
        available, and rewrites RULES.md without the evicted rules.

        Returns a list of ``{"text", "reason", "source"}`` dicts describing
        what was archived (empty list when nothing happened or the feature
        is disabled).  Also stores the result in
        ``self._pending_archive_notice`` so the runner / CLI can surface a
        notification on the next user-visible turn.
        """
        if not self.auto_archive_rules_enabled:
            return []

        from datetime import date as _date
        from agent.rules_lifecycle import (
            ARCHIVE_REASON_AGE,
            ARCHIVE_REASON_CAPACITY,
            auto_archive_rules,
            parse_rule_entry,
            serialize_rule_entry,
        )

        rules_path = self._path_for("rules")
        archive_path = rules_path.with_name("RULES.archive.md")
        results: List[Dict[str, Any]] = []

        with self._file_lock(rules_path):
            self._reload_target("rules")
            entries = list(self.rules_entries)
            if not entries:
                return []

            parsed = [parse_rule_entry(e) for e in entries]
            decision = auto_archive_rules(
                parsed,
                char_limit=self.rules_char_limit,
                today=_date.today(),
                capacity_threshold=self.auto_archive_capacity_threshold,
                age_days=self.auto_archive_age_days,
                recurrence_window_days=self.auto_archive_recurrence_window,
                new_marker_days=self.trial_new_marker_days,
                delimiter=ENTRY_DELIMITER,
            )
            if not decision.archived:
                return []

            # Stamp archived entries with archive metadata so unarchive can
            # restore them, and append to RULES.archive.md.
            today = _date.today()
            archive_blocks: List[str] = []
            for victim, reason in zip(decision.archived, decision.reasons):
                victim.extra["archived_at"] = today.isoformat()
                victim.extra["archived_reason"] = reason
                archive_blocks.append(serialize_rule_entry(victim))
                results.append(
                    {
                        "text": victim.text,
                        "reason": reason,
                        "source": victim.source,
                    }
                )

            # Append to archive (don't truncate — archive accumulates history).
            if archive_blocks:
                self._append_archive(archive_path, archive_blocks)

            # Rewrite RULES.md with survivors (round-trip through serializer
            # so we don't lose metadata that was added during this session).
            survivors = [serialize_rule_entry(e) for e in decision.keep if e.text.strip()]
            self._set_entries("rules", survivors)
            self.save_to_disk("rules")

            # Best-effort: push into LCM so the rule is recoverable via
            # lcm_search even after archiving.  Failures are silent — the
            # archive file itself is the durable record.  Outer try/except
            # is defense-in-depth: _index_archive_to_lcm already swallows
            # known errors, but a misbehaving LCM provider could raise
            # outside its own wrappers (e.g. attribute access on a None
            # _lcm_engine that was replaced mid-call), and we don't want
            # archive bookkeeping to fail because of an indexing hiccup.
            try:
                self._index_archive_to_lcm(decision.archived, reasons=decision.reasons)
            except Exception as _lcm_exc:
                logger.debug(
                    "LCM indexing of archived rules failed (non-fatal): %s",
                    _lcm_exc,
                )

        # Stash the notice for the next user-visible turn.  Cleared by the
        # consumer (cli.py / run_agent) once shown.
        self._pending_archive_notice = list(results)
        return results

    def consume_archive_notice(self) -> List[Dict[str, Any]]:
        """Return the pending archive notice (if any) and clear it.

        Used by the CLI/gateway runner to ensure the user sees the
        notification exactly once after an auto-archive event.
        """
        notice = list(self._pending_archive_notice)
        self._pending_archive_notice = []
        return notice

    def unarchive_rule(self, identifier: str) -> Dict[str, Any]:
        """Move a rule from RULES.archive.md back into RULES.md.

        ``identifier`` is matched against (a) the rule's ``source`` field
        (e.g. ``LRN-20260428-003``) and (b) a substring of the rule text.
        The first match wins.  The restored rule loses its archive metadata
        but keeps its original ``created`` / ``source`` / ``pattern_key``.
        """
        from agent.rules_lifecycle import parse_rule_entry, serialize_rule_entry

        identifier = (identifier or "").strip()
        if not identifier:
            return {"success": False, "error": "identifier required"}

        rules_path = self._path_for("rules")
        archive_path = rules_path.with_name("RULES.archive.md")
        if not archive_path.exists():
            return {"success": False, "error": "No archived rules."}

        with self._file_lock(rules_path):
            archived_entries = self._read_file(archive_path)
            parsed_archived = [parse_rule_entry(e) for e in archived_entries]
            target_idx = -1
            for i, entry in enumerate(parsed_archived):
                if entry.source == identifier or identifier.lower() in entry.text.lower():
                    target_idx = i
                    break
            if target_idx < 0:
                return {"success": False, "error": f"No archived rule matches '{identifier}'."}

            target = parsed_archived.pop(target_idx)
            # Strip archive metadata before restoring.
            target.extra.pop("archived_at", None)
            target.extra.pop("archived_reason", None)

            # Re-add to RULES.md (front of list — recently restored bubbles up).
            self._reload_target("rules")
            current = list(self.rules_entries)
            current.insert(0, serialize_rule_entry(target))
            self._set_entries("rules", current)
            self.save_to_disk("rules")

            # Rewrite archive without the restored entry.
            self._write_file(
                archive_path,
                [serialize_rule_entry(e) for e in parsed_archived if e.text.strip()],
            )

        return {
            "success": True,
            "restored": target.text,
            "source": target.source,
        }

    def list_archived_rules(self) -> List[Dict[str, Any]]:
        """Return parsed archive entries for /rules archive list."""
        from agent.rules_lifecycle import parse_rule_entry

        archive_path = self._path_for("rules").with_name("RULES.archive.md")
        if not archive_path.exists():
            return []
        out: List[Dict[str, Any]] = []
        for raw in self._read_file(archive_path):
            entry = parse_rule_entry(raw)
            if not entry.text.strip():
                continue
            out.append(
                {
                    "text": entry.text,
                    "source": entry.source,
                    "created": entry.created.isoformat() if entry.created else "",
                    "archived_at": entry.extra.get("archived_at", ""),
                    "reason": entry.extra.get("archived_reason", ""),
                }
            )
        return out

    @staticmethod
    def _append_archive(archive_path: Path, new_blocks: List[str]) -> None:
        """Append archived rule blocks to RULES.archive.md (creating if needed)."""
        existing: List[str] = []
        if archive_path.exists():
            existing = MemoryStore._read_file(archive_path)
        merged = existing + [b for b in new_blocks if b.strip()]
        MemoryStore._write_file(archive_path, merged)

    def _index_archive_to_lcm(self, archived: List[Any], *, reasons: List[str]) -> None:
        """Best-effort: push archived rules into LCM for retrieval."""
        if self._lcm_engine is None or not archived:
            return
        try:
            store = self._lcm_engine._ensure_store()  # type: ignore[attr-defined]
            embedder = self._lcm_engine._ensure_embedder()  # type: ignore[attr-defined]
            session_id = (
                getattr(self._lcm_engine, "_session_id", "")
                or "rules-archive"
            )
        except Exception:
            return

        for entry, reason in zip(archived, reasons):
            try:
                text = (
                    f"[archived rule, reason={reason}, source={entry.source}]\n"
                    f"{entry.text}"
                )
                vector = embedder.embed([text])[0]
                store.add(
                    session_id=session_id,
                    text=text,
                    vector=vector,
                    metadata={
                        "kind": "rules_archive",
                        "reason": reason,
                        "source": entry.source,
                    },
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("LCM index of archived rule failed: %s", exc)

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.

        This returns the state captured at load_from_disk() time, NOT the live
        state. Mid-session writes do not affect this. This keeps the system
        prompt stable across all turns, preserving the prefix cache.

        Returns None if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator.

        For ``target='rules'`` the block goes through the lifecycle layer:
        entries are parsed, split into pinned/regular tiers and rendered as
        two sections.  Entries flagged by ``should_show_new_marker`` get a
        trailing ``[NEW — verify before applying]`` tag so the model knows
        they were auto-promoted recently.

        For ``target='memory'`` and ``target='user'`` we transparently strip
        any HTML metadata comments (added by ``add_memory_with_lifecycle``)
        before rendering so the model only sees the prose body.  This keeps
        the prompt cache stable across migrations of legacy entries.
        """
        if not entries:
            return ""

        if target == "rules":
            return self._render_rules_block(entries)

        limit = self._char_limit(target)
        rendered_entries = [self._strip_meta_for_display(e) for e in entries]
        content = ENTRY_DELIMITER.join(rendered_entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _strip_meta_for_display(entry: str) -> str:
        """Remove the trailing ``<!-- hermes-meta: ... -->`` comment, if any."""
        from agent.rules_lifecycle import META_LINE_RE
        match = META_LINE_RE.search(entry)
        if match is None:
            return entry
        return entry[: match.start()].rstrip()

    # -- Rules tier rendering -------------------------------------------------

    def _render_rules_block(self, entries: List[str]) -> str:
        """Render RULES.md as two tiers: pinned (top) + regular (with [NEW] tags).

        We strip the HTML metadata comments from what the LLM sees — the
        prose body is enough for the model.  Metadata stays on disk for the
        lifecycle layer to consume on the next read/auto-archive cycle.
        """
        from datetime import date as _date
        from agent.rules_lifecycle import (
            parse_rule_entry,
            should_show_new_marker,
            split_by_tier,
        )

        parsed = [parse_rule_entry(e) for e in entries]
        tiers = split_by_tier(parsed)

        limit = self.rules_char_limit
        # Char count uses the full on-disk form (matches what fills the bucket).
        current = sum(len(e.raw or e.text) for e in parsed)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        separator = "═" * 46
        sections: List[str] = []
        today = _date.today()

        def _render_tier(header: str, tier_entries: List[Any], mark_new: bool) -> str:
            lines: List[str] = []
            for entry in tier_entries:
                text = entry.text.strip()
                if mark_new and should_show_new_marker(
                    entry,
                    today=today,
                    window_days=self.trial_new_marker_days,
                ):
                    text = f"{text}  [NEW — verify before applying]"
                lines.append(text)
            body = ENTRY_DELIMITER.join(lines)
            return f"{separator}\n{header}\n{separator}\n{body}"

        if tiers["pinned"]:
            sections.append(
                _render_tier(
                    f"⭐ PINNED RULES (highest priority — MUST NOT violate) "
                    f"[{pct}% — {current:,}/{limit:,} chars]",
                    tiers["pinned"],
                    mark_new=False,  # pinned never get [NEW]
                )
            )
        if tiers["regular"]:
            header = (
                "AGENT RULES (mandatory protocols — MUST NOT violate)"
                if not sections
                else "AGENT RULES (regular)"
            )
            if not sections:
                header = f"{header} [{pct}% — {current:,}/{limit:,} chars]"
            sections.append(_render_tier(header, tiers["regular"], mark_new=True))

        return "\n\n".join(sections)

    def format_rules_by_tier(self) -> Dict[str, str]:
        """Return the pinned / regular sections rendered separately.

        Used by run_agent's prompt builder when it wants to interleave other
        blocks between the pinned tier and the regular tier.  The default
        ``format_for_system_prompt('rules')`` returns the same content as one
        joined string for callers that don't care about tiering.
        """
        from datetime import date as _date
        from agent.rules_lifecycle import (
            parse_rule_entry,
            should_show_new_marker,
            split_by_tier,
        )

        # Use the snapshot (frozen at load_from_disk) so the prompt stays
        # cache-stable mid-session.  Re-parse the snapshot from raw entries
        # at injection time.
        # Source of truth here: live entries (matches existing snapshot logic
        # in load_from_disk).  Callers outside the prompt-build path use
        # format_for_system_prompt('rules') which honours the snapshot.
        parsed = [parse_rule_entry(e) for e in self.rules_entries]
        tiers = split_by_tier(parsed)

        from datetime import date as _date2  # avoid name shadow
        today = _date2.today()
        separator = "═" * 46
        out: Dict[str, str] = {"pinned": "", "regular": ""}

        def _block(header: str, tier_entries: List[Any], mark_new: bool) -> str:
            if not tier_entries:
                return ""
            lines = []
            for entry in tier_entries:
                text = entry.text.strip()
                if mark_new and should_show_new_marker(
                    entry,
                    today=today,
                    window_days=self.trial_new_marker_days,
                ):
                    text = f"{text}  [NEW — verify before applying]"
                lines.append(text)
            body = ENTRY_DELIMITER.join(lines)
            return f"{separator}\n{header}\n{separator}\n{body}"

        out["pinned"] = _block(
            "⭐ PINNED RULES (highest priority — MUST NOT violate)",
            tiers["pinned"],
            mark_new=False,
        )
        out["regular"] = _block(
            "AGENT RULES (mandatory protocols — MUST NOT violate)",
            tiers["regular"],
            mark_new=True,
        )
        return out

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # Write to temp file in same directory (same filesystem for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp_path, path)
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def memory_tool(
    action: str,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Returns JSON string with results.
    """
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    if target not in VALID_TARGETS:
        return tool_error(
            f"Invalid target '{target}'. Use 'rules', 'memory', or 'user'.",
            success=False,
        )

    if action == "add":
        if not content:
            return tool_error("Content is required for 'add' action.", success=False)
        result = store.add(target, content)

    elif action == "replace":
        if not old_text:
            return tool_error("old_text is required for 'replace' action.", success=False)
        if not content:
            return tool_error("content is required for 'replace' action.", success=False)
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return tool_error("old_text is required for 'remove' action.", success=False)
        result = store.remove(target, old_text)

    else:
        return tool_error(f"Unknown action '{action}'. Use: add, replace, remove", success=False)

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Every entry is injected into the system prompt of future turns, so keep "
        "entries compact and focused on facts that will still matter later.\n\n"
        "THREE LAYERED TARGETS — choose the right one:\n"
        "- 'rules': mandatory protocols / red lines / hard守则. Use when the user "
        "  says 'always do X', 'never Y', 'this is a rule', 'must', '必须', "
        "  '红线', or corrects a behavior pattern (not a one-off mistake). "
        "  Highest priority, smallest budget — only the most universal rules "
        "  belong here. Stays at the top of every system prompt.\n"
        "- 'user': who the user is — name, role, preferences, communication "
        "  style, pet peeves, personal details. Use when learning facts about "
        "  the user themselves (not what they want you to do).\n"
        "- 'memory': your working notes — environment facts, project "
        "  conventions, tool quirks, lessons learned. Default bucket for "
        "  observations that don't fit rules or user. When this fills up, "
        "  oldest entries auto-archive to the long-context store (and stay "
        "  recoverable when the long-context store is active).\n\n"
        "ROUTING SHORTCUTS:\n"
        "- 'always / never / must / 必须 / 红线' → rules\n"
        "- 'I am / I prefer / 我是 / 我喜欢' → user\n"
        "- 'this project uses / this tool needs' → memory\n\n"
        "WHEN TO SAVE (proactively, don't wait to be asked):\n"
        "- User corrects you or says 'remember this' / 'don't do that again'\n"
        "- User shares a preference, habit, or personal detail\n"
        "- You discover an environment / project / tool fact reusable later\n"
        "- You learn a workflow rule the user wants enforced\n\n"
        "DETECTION TRIGGERS (capture immediately when seen):\n"
        "- 'No, that's wrong' / 'Actually...' / '不对' / '其实应该...' → likely memory or rules\n"
        "- 'I am / I prefer / 我是 / 我喜欢 / 我习惯' → user\n"
        "- 'Always / Never / Must / 必须 / 红线' → rules\n"
        "- 'This project uses X' / 'this tool needs Y' → memory\n\n"
        "PROMOTION SIGNAL (memory → rules):\n"
        "If you're adding a similar memory entry for the 2nd or 3rd time about "
        "the same behavior, that's a recurring pattern — promote it as a rule "
        "and delete the duplicate memory entries. Repeated observations about "
        "behavior are underlying rules in disguise. (For transient errors and "
        "in-flight learnings, use the learning_record tool if available — it "
        "tracks recurrence and auto-promotes for you.)\n\n"
        "Do NOT save: task progress, session outcomes, completed-work logs, "
        "temporary TODO state, raw data dumps, trivial/easily-rediscovered info. "
        "For procedural knowledge that is reusable across sessions (a how-to, a "
        "checklist, a recipe), prefer the skill tool — skills hold richer detail "
        "and are loaded on demand instead of every turn.\n\n"
        "ACTIONS: add (new entry), replace (update existing — old_text identifies "
        "it), remove (delete — old_text identifies it)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform.",
            },
            "target": {
                "type": "string",
                "enum": list(VALID_TARGETS),
                "description": (
                    "Which store: 'rules' for mandatory protocols / red lines, "
                    "'user' for user profile facts, 'memory' for general notes. "
                    "See routing shortcuts in the description."
                ),
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'.",
            },
            "old_text": {
                "type": "string",
                "description": "Short unique substring identifying the entry to replace or remove.",
            },
        },
        "required": ["action", "target"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)




