#!/usr/bin/env python3
"""
Rules Lifecycle Module
======================

Manages metadata, tier ordering, [NEW] marking, and auto-archive decisions
for entries in RULES.md (and the companion RULES.archive.md).

Each entry in RULES.md is a free-form line (or paragraph) followed by an
optional HTML comment carrying lifecycle metadata::

    Always use scripts/run_tests.sh.
    <!-- hermes-meta: pinned=false; created=2026-04-30; source=manual -->
    §
    Confirm scope before editing >5 files.
    <!-- hermes-meta: pinned=false; created=2026-04-28; source=LRN-20260428-003;
                      promoted_at=2026-04-28; recurrence=4 -->
    §
    Don't add narrative comments.
    <!-- hermes-meta: pinned=true; created=2026-03-15; source=manual -->

Design choices:

* HTML comment so the metadata is invisible to the LLM but trivial to parse.
* Backward compatible — entries without a meta comment are treated as
  ``stable + manual`` (the conservative default).
* Pure data layer: no I/O, no SQLite, no LCM coupling. The store layer
  (``tools/memory_tool.py``) is responsible for reading/writing files and
  triggering archive bookkeeping.

The two production-critical functions are::

    auto_archive_rules(...)        # double-triggered eviction
    should_show_new_marker(...)    # 7-day [NEW] tag for freshly promoted rules

Both are pure functions that take the parsed entries plus the current date
and return decisions; the caller wires them up to RULES.md / RULES.archive.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Metadata serialization (HTML-comment "key=value; key=value" form)
# ---------------------------------------------------------------------------

# Single-line HTML comment carrying our metadata. Tolerant of leading
# whitespace and trailing newline. Only a comment that actually starts with
# the ``hermes-meta:`` prefix is treated as our metadata. The underscore
# alias is kept for backward compatibility with existing imports.
META_LINE_RE = re.compile(
    r"<!--\s*hermes-meta\s*:\s*(?P<body>[^>]*?)\s*-->",
    re.IGNORECASE,
)
_META_LINE_RE = META_LINE_RE  # legacy alias

# Permitted boolean values (case-insensitive).
_BOOL_TRUE = {"true", "yes", "1", "y", "on"}
_BOOL_FALSE = {"false", "no", "0", "n", "off"}

# Default new-marker window in days (also exposed as a config key).
DEFAULT_NEW_MARKER_DAYS = 7

# Reasons returned by ``auto_archive_rules`` for tracking/notification.
ARCHIVE_REASON_CAPACITY = "capacity_threshold"
ARCHIVE_REASON_AGE = "age_no_recurrence"

# Source-id prefixes that indicate the rule was promoted from another
# subsystem (rather than typed by a human).  Any prefix listed here makes
# ``RuleEntry.is_from_learning()`` return True.  Centralised here so the
# rules-lifecycle layer, the memory_tool wrapper, and the project-knowledge
# promotion path all agree on what counts as "auto-promoted".
LEARNING_SOURCE_PREFIXES: tuple = ("LRN-", "ERR-", "FEAT-", "PK:", "PK-")


@dataclass
class RuleEntry:
    """A single rule entry from RULES.md plus parsed lifecycle metadata.

    The ``text`` is the rule's prose body (everything except the trailing
    ``<!-- hermes-meta: ... -->`` comment, if any).  ``raw`` is the entry
    exactly as it appeared on disk so we can round-trip without churn when
    nothing changed.
    """

    text: str
    pinned: bool = False
    created: Optional[date] = None
    source: str = "manual"          # "manual" or "LRN-YYYYMMDD-XXX"
    promoted_at: Optional[date] = None
    recurrence: int = 0             # only meaningful for source=LRN-*
    last_recurrence: Optional[date] = None  # last time the same pattern reappeared
    pattern_key: str = ""           # echo of the LRN's pattern_key for archive judgment
    last_edited: Optional[date] = None
    extra: Dict[str, str] = field(default_factory=dict)  # forward-compat catch-all
    raw: str = ""                   # original on-disk form (text + meta)

    # ------------------------------------------------------------------ helpers
    def display_id(self) -> str:
        """Short stable id for CLI display: source if LRN-*, else first 12 chars hash-free."""
        if self.source.startswith("LRN-"):
            return self.source
        # short slug from text — first 8 word chars, lowercase
        slug = re.sub(r"\W+", "-", self.text.strip().lower())[:32].strip("-")
        return slug or "rule"

    def is_from_learning(self) -> bool:
        """True if the source id was minted by a promotion subsystem.

        Treats LRN-/ERR-/FEAT- (learning-store), PK:/PK- (project-knowledge
        promotions), and any other prefix listed in
        ``LEARNING_SOURCE_PREFIXES`` as "auto-promoted".  Manual rules
        (source='manual' or the literal string 'PK' alone) return False.
        """
        return any(self.source.startswith(p) for p in LEARNING_SOURCE_PREFIXES)

    def has_lifecycle_meta(self) -> bool:
        """Whether this entry carries an explicit hermes-meta comment."""
        return bool(self.raw and _META_LINE_RE.search(self.raw))


# ---------------------------------------------------------------------------
# Parse / Serialize
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    # Accept YYYY-MM-DD or full ISO (timestamp tail tolerated).
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_bool(value: str, default: bool = False) -> bool:
    v = (value or "").strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    return default


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def _parse_meta_body(body: str) -> Dict[str, str]:
    """Parse a ``key=value; key=value`` body into a dict (lowercased keys)."""
    out: Dict[str, str] = {}
    for chunk in body.split(";"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k:
            out[k] = v
    return out


# Known metadata keys we know how to interpret. Anything else lands in
# ``extra`` so we can forward-compatibly round-trip future fields.
_KNOWN_META_KEYS = {
    "pinned",
    "created",
    "source",
    "promoted_at",
    "recurrence",
    "last_recurrence",
    "pattern_key",
    "last_edited",
}


def parse_rule_entry(raw: str) -> RuleEntry:
    """Parse a single RULES.md entry (body + optional metadata comment)."""
    raw = (raw or "").rstrip("\n")
    if not raw.strip():
        return RuleEntry(text="", raw=raw)

    meta_match = _META_LINE_RE.search(raw)
    if meta_match is None:
        return RuleEntry(text=raw.strip(), raw=raw)

    body = raw[: meta_match.start()].rstrip()
    meta = _parse_meta_body(meta_match.group("body"))

    extra = {k: v for k, v in meta.items() if k not in _KNOWN_META_KEYS}

    return RuleEntry(
        text=body.strip(),
        pinned=_parse_bool(meta.get("pinned", "false")),
        created=_parse_date(meta.get("created", "")),
        source=meta.get("source", "manual") or "manual",
        promoted_at=_parse_date(meta.get("promoted_at", "")),
        recurrence=_parse_int(meta.get("recurrence", "0")),
        last_recurrence=_parse_date(meta.get("last_recurrence", "")),
        pattern_key=meta.get("pattern_key", ""),
        last_edited=_parse_date(meta.get("last_edited", "")),
        extra=extra,
        raw=raw,
    )


def serialize_rule_entry(entry: RuleEntry) -> str:
    """Render a RuleEntry back to the on-disk form (body + meta comment).

    Always emits a metadata comment for entries we know about so future
    re-reads have everything. Entries with empty text return empty string.
    """
    if not entry.text.strip():
        return ""

    parts = [
        f"pinned={'true' if entry.pinned else 'false'}",
    ]
    if entry.created:
        parts.append(f"created={entry.created.isoformat()}")
    parts.append(f"source={entry.source or 'manual'}")
    if entry.promoted_at:
        parts.append(f"promoted_at={entry.promoted_at.isoformat()}")
    if entry.recurrence:
        parts.append(f"recurrence={entry.recurrence}")
    if entry.last_recurrence:
        parts.append(f"last_recurrence={entry.last_recurrence.isoformat()}")
    if entry.pattern_key:
        parts.append(f"pattern_key={entry.pattern_key}")
    if entry.last_edited:
        parts.append(f"last_edited={entry.last_edited.isoformat()}")
    # Preserve unknown metadata keys for forward compat.
    for k, v in entry.extra.items():
        if k not in _KNOWN_META_KEYS:
            parts.append(f"{k}={v}")

    meta = "; ".join(parts)
    return f"{entry.text.rstrip()}\n<!-- hermes-meta: {meta} -->"


# ---------------------------------------------------------------------------
# Tier classification + [NEW] marker
# ---------------------------------------------------------------------------

def split_by_tier(entries: List[RuleEntry]) -> Dict[str, List[RuleEntry]]:
    """Split parsed entries into ``pinned`` and ``regular`` lists.

    The ordering is preserved — within each tier entries appear in the same
    order as the input list (which mirrors RULES.md on-disk order).
    """
    pinned: List[RuleEntry] = []
    regular: List[RuleEntry] = []
    for e in entries:
        if not e.text.strip():
            continue
        if e.pinned:
            pinned.append(e)
        else:
            regular.append(e)
    return {"pinned": pinned, "regular": regular}


def should_show_new_marker(
    entry: RuleEntry,
    *,
    today: Optional[date] = None,
    window_days: int = DEFAULT_NEW_MARKER_DAYS,
) -> bool:
    """Whether to flag ``entry`` with a ``[NEW]`` tag in the rendered prompt.

    A rule earns the marker when:
      - it was auto-promoted from a learning (``source`` starts with ``LRN-``),
      - ``promoted_at`` is set,
      - ``today - promoted_at <= window_days``,
      - it's not pinned (pinned rules already stand out enough).

    ``today`` defaults to ``date.today()``. Callers are encouraged to pass an
    explicit value in tests to keep results deterministic.
    """
    if window_days <= 0:
        return False
    if entry.pinned:
        return False
    if not entry.is_from_learning():
        return False
    if entry.promoted_at is None:
        return False
    if today is None:
        today = date.today()
    age = (today - entry.promoted_at).days
    if age < 0:
        return False
    return age <= window_days


# ---------------------------------------------------------------------------
# Auto-archive decision (pure function — no I/O)
# ---------------------------------------------------------------------------

@dataclass
class ArchiveDecision:
    """Result of ``auto_archive_rules``.

    * ``keep`` is the rules that remain in RULES.md (in their original order).
    * ``archived`` is the rules that should be moved to RULES.archive.md, in
      the order they were chosen for eviction (oldest first when triggered
      by capacity).
    * ``reasons`` mirrors ``archived`` and explains why each rule was chosen
      (uses the ``ARCHIVE_REASON_*`` constants for stability).
    """

    keep: List[RuleEntry]
    archived: List[RuleEntry]
    reasons: List[str]

    def __bool__(self) -> bool:
        return bool(self.archived)


def _is_protected_from_archive(
    entry: RuleEntry,
    *,
    today: date,
    new_marker_days: int,
    recurrence_window_days: int,
) -> Tuple[bool, str]:
    """Whether ``entry`` is protected from age-based archiving.

    Returns ``(protected, reason)``.  Reason is informational and not used
    for the user-facing notification.
    """
    if entry.pinned:
        return True, "pinned"

    # Inside the [NEW] window — newly promoted rules deserve a fair chance.
    if entry.promoted_at and (today - entry.promoted_at).days <= new_marker_days:
        return True, "within_new_marker_window"

    # User edited recently — they touched it, hands off.
    if entry.last_edited and (today - entry.last_edited).days <= recurrence_window_days:
        return True, "recently_edited_by_user"

    # If the LRN that fed this rule recurred recently, the underlying issue
    # is still alive — keep the rule.
    if entry.last_recurrence and (
        (today - entry.last_recurrence).days <= recurrence_window_days
    ):
        return True, "recently_recurred"

    return False, ""


def _entry_age_days(entry: RuleEntry, today: date) -> int:
    """How long the rule has lived. Fall back to created → promoted_at → 0."""
    anchor = entry.created or entry.promoted_at
    if anchor is None:
        return 0
    return max(0, (today - anchor).days)


def auto_archive_rules(
    entries: List[RuleEntry],
    *,
    char_limit: int,
    today: Optional[date] = None,
    capacity_threshold: float = 0.80,
    age_days: int = 90,
    recurrence_window_days: int = 30,
    new_marker_days: int = DEFAULT_NEW_MARKER_DAYS,
    delimiter: str = "\n§\n",
) -> ArchiveDecision:
    """Decide which rules (if any) to archive right now.

    Two independent triggers, applied in order:

    **Trigger A — capacity protection.** When the serialized RULES.md exceeds
    ``capacity_threshold`` of ``char_limit``, evict the oldest non-pinned
    rules (by ``created``) until back under threshold. Manual rules participate
    here because the user authored them; a full bucket would block the
    self-learning loop. Pinned rules never participate.

    **Trigger B — age-based eviction.** Independently of capacity, any rule
    that was auto-promoted from a learning (``source=LRN-*``), is older than
    ``age_days``, has no recurrence in the last ``recurrence_window_days``,
    and was not edited or pinned by the user, is moved to the archive. This
    is what keeps the bucket lean over the long run.

    The function is **pure** — it does not touch the filesystem and does not
    update LCM. The caller (memory_tool) is responsible for writing
    RULES.archive.md and replaying the LCM bookkeeping.

    Args:
        entries: parsed rules in current on-disk order.
        char_limit: the configured character budget for RULES.md.
        today: date to evaluate against (default ``date.today()``).
        capacity_threshold: 0.0-1.0 fraction of ``char_limit`` triggering A.
        age_days: minimum age (in days) for trigger B candidates.
        recurrence_window_days: protective window for both ``last_recurrence``
            and ``last_edited`` checks.
        new_marker_days: protective window for ``promoted_at`` (the [NEW] phase).
        delimiter: how rule entries are joined when measuring char count.
    """
    if today is None:
        today = date.today()

    # Local mutable copy — we treat ``entries`` as immutable input.
    keep: List[RuleEntry] = list(entries)
    archived: List[RuleEntry] = []
    reasons: List[str] = []

    def _serialized_size(items: List[RuleEntry]) -> int:
        return len(
            delimiter.join(
                serialize_rule_entry(e)
                for e in items
                if e is not None and e.text.strip()
            )
        )

    # ── Trigger A: capacity protection ─────────────────────────────────────
    if char_limit > 0 and capacity_threshold > 0:
        cutoff = int(char_limit * capacity_threshold)
        if _serialized_size(keep) > cutoff:
            # Sort archivable candidates by (created or epoch) ascending —
            # oldest first.  Indexes preserved so we can re-insert into the
            # right slots.
            order_key = []
            for idx, entry in enumerate(keep):
                if entry.pinned:
                    continue
                anchor = entry.created or entry.promoted_at or date.min
                order_key.append((anchor, idx))
            order_key.sort()

            for _, idx in order_key:
                if _serialized_size(keep) <= cutoff:
                    break
                victim = keep[idx]
                # Stage for removal — we'll filter at the end so indexes stay
                # valid during this loop.
                if victim is None:
                    continue
                keep[idx] = None  # type: ignore[assignment]
                archived.append(victim)
                reasons.append(ARCHIVE_REASON_CAPACITY)

            keep = [e for e in keep if e is not None]

    # ── Trigger B: age-based eviction (LRN-promoted rules only) ────────────
    if age_days > 0:
        b_keep: List[RuleEntry] = []
        for entry in keep:
            if not entry.is_from_learning():
                b_keep.append(entry)
                continue
            protected, _ = _is_protected_from_archive(
                entry,
                today=today,
                new_marker_days=new_marker_days,
                recurrence_window_days=recurrence_window_days,
            )
            if protected:
                b_keep.append(entry)
                continue
            age = _entry_age_days(entry, today)
            if age < age_days:
                b_keep.append(entry)
                continue
            archived.append(entry)
            reasons.append(ARCHIVE_REASON_AGE)
        keep = b_keep

    return ArchiveDecision(keep=keep, archived=archived, reasons=reasons)
