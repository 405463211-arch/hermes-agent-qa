#!/usr/bin/env python3
"""
Learning Store — SQLite-backed transient learning ledger
========================================================

Captures three categories of in-flight knowledge so the agent can:

  1. Track recurring patterns of mistakes / corrections / feature requests
  2. Auto-promote durable patterns to RULES.md (via the rules-lifecycle layer)
  3. Avoid polluting MEMORY.md with one-off entries that would later need
     manual cleanup

Design notes
------------

* SQLite (not markdown), because:
  - Pattern-Key dedupe needs O(1) index lookup
  - ``recurrence_count`` increments must be atomic
  - 30-day window queries need an index on ``last_seen``

* Pure storage layer — no LLM, no prompts.  The ``learning_record`` tool
  (``tools/learning_tool.py``) is the only caller in the production hot
  path; CLI commands (``/learn ...``) call ``list``/``get``/``stats``.

* No PII scrubbing here — the tool layer is responsible for refusing to
  store credentials or transcripts.  This module just persists what it's
  given.

Schema (kept stable so future migrations don't churn)
-----------------------------------------------------

::

    CREATE TABLE learnings (
        id              TEXT PRIMARY KEY,        -- LRN-YYYYMMDD-XXX / ERR-... / FEAT-...
        category        TEXT NOT NULL,           -- 'learning' | 'error' | 'feature_request'
        subcategory     TEXT,                    -- correction | knowledge_gap | best_practice | insight
        pattern_key     TEXT NOT NULL,           -- stable dedupe key
        summary         TEXT NOT NULL,           -- one-line description
        details         TEXT,                    -- full context
        suggested_action TEXT,                   -- becomes the rule body when promoted
        priority        TEXT DEFAULT 'medium',
        status          TEXT DEFAULT 'pending',
        area            TEXT,
        recurrence_count INTEGER DEFAULT 1,
        first_seen      REAL NOT NULL,           -- unix timestamp
        last_seen       REAL NOT NULL,
        distinct_tasks  INTEGER DEFAULT 1,
        last_task_id    TEXT,
        promoted_to     TEXT,                    -- 'rules' | 'skill:<name>' | NULL
        promoted_at     REAL,
        resolution_notes TEXT,
        related_files_json TEXT                  -- JSON list of paths
    );
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


VALID_CATEGORIES = {"learning", "error", "feature_request"}
VALID_STATUSES = {"pending", "in_progress", "resolved", "promoted", "promoted_to_skill", "wont_fix"}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}

ID_PREFIX = {
    "learning": "LRN",
    "error": "ERR",
    "feature_request": "FEAT",
}


def get_learning_store_path() -> Path:
    """Return the on-disk SQLite path. Honours profile via ``get_hermes_home``."""
    return get_hermes_home() / "learning_store.db"


def _utcnow() -> float:
    return time.time()


def _format_id(category: str, when: float) -> str:
    """Generate ``LRN-YYYYMMDD-XXXXXX`` style id (suffix is uuid-derived).

    Six hex chars give 16M combinations per day-bucket; the original 3-char
    suffix had only 4096 and started colliding under modest write rates
    (~70% collision probability at 500 same-day rows by birthday paradox).
    Old 3-char ids on disk are unaffected — the column is TEXT.
    """
    prefix = ID_PREFIX.get(category, "LRN")
    date_str = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:6].upper()
    return f"{prefix}-{date_str}-{suffix}"


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learnings (
    id              TEXT PRIMARY KEY,
    category        TEXT NOT NULL,
    subcategory     TEXT,
    pattern_key     TEXT NOT NULL,
    summary         TEXT NOT NULL,
    details         TEXT,
    suggested_action TEXT,
    priority        TEXT DEFAULT 'medium',
    status          TEXT DEFAULT 'pending',
    area            TEXT,
    recurrence_count INTEGER DEFAULT 1,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    distinct_tasks  INTEGER DEFAULT 1,
    last_task_id    TEXT,
    promoted_to     TEXT,
    promoted_at     REAL,
    resolution_notes TEXT,
    related_files_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_pattern_key ON learnings(pattern_key);
CREATE INDEX IF NOT EXISTS idx_status_lastseen ON learnings(status, last_seen);
CREATE INDEX IF NOT EXISTS idx_category_priority ON learnings(category, priority);
"""


# ---------------------------------------------------------------------------
# Promotion eligibility — pure function, exposed so tools can preview
# ---------------------------------------------------------------------------

@dataclass
class PromotionRule:
    min_recurrence: int = 3
    min_distinct_tasks: int = 2
    window_days: int = 30


def is_eligible_for_promotion(entry: Dict[str, Any], rule: PromotionRule = PromotionRule()) -> bool:
    """Whether ``entry`` qualifies for auto-promotion to RULES.md.

    All four conditions must hold (matches the OpenClaw self-improving-agent
    ladder, with the values tunable via :class:`PromotionRule`):

      1. ``status == 'pending'`` — already promoted/resolved entries don't requalify
      2. ``recurrence_count >= rule.min_recurrence``
      3. ``distinct_tasks >= rule.min_distinct_tasks``
      4. ``last_seen - first_seen <= rule.window_days`` (reasonably recent burst,
         not "happened twice years apart")
    """
    if (entry.get("status") or "pending") != "pending":
        return False
    if entry.get("promoted_to"):
        return False
    if int(entry.get("recurrence_count") or 0) < rule.min_recurrence:
        return False
    if int(entry.get("distinct_tasks") or 0) < rule.min_distinct_tasks:
        return False

    first = float(entry.get("first_seen") or 0)
    last = float(entry.get("last_seen") or 0)
    if first <= 0 or last <= 0:
        return False
    span_days = (last - first) / 86400.0
    if span_days > rule.window_days:
        return False
    return True


# ---------------------------------------------------------------------------
# LearningStore
# ---------------------------------------------------------------------------

class LearningStore:
    """SQLite-backed store with file-locked write paths.

    Connection is opened lazily and re-used per process; SQLite's own file
    lock handles cross-process write concurrency.  We use ``with self._conn``
    transactions for atomicity within a single op.
    """

    def __init__(self, db_path: Optional[Path] = None, *, promotion_rule: Optional[PromotionRule] = None):
        self.db_path: Path = Path(db_path) if db_path else get_learning_store_path()
        self.promotion_rule: PromotionRule = promotion_rule or PromotionRule()
        self._conn: Optional[sqlite3.Connection] = None

    # -- Connection lifecycle ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    # -- Core operations -----------------------------------------------------

    def record(
        self,
        category: str,
        pattern_key: str,
        summary: str,
        *,
        details: str = "",
        suggested_action: str = "",
        subcategory: str = "",
        priority: str = "medium",
        area: str = "",
        task_id: str = "",
        related_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Insert or merge a learning entry.

        If an entry with the same ``pattern_key`` already exists in
        ``pending`` state, ``recurrence_count`` is incremented, ``last_seen``
        refreshed, and ``distinct_tasks`` bumped if ``task_id`` differs from
        ``last_task_id``.  Otherwise a fresh row is inserted.

        Returns a dict that mirrors the row plus an ``eligible_for_promotion``
        boolean computed against the current promotion rule.
        """
        category = (category or "").strip().lower()
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(VALID_CATEGORIES)}, got {category!r}"
            )
        pattern_key = (pattern_key or "").strip()
        summary = (summary or "").strip()
        if not pattern_key:
            raise ValueError("pattern_key is required and must be non-empty")
        if not summary:
            raise ValueError("summary is required and must be non-empty")
        if priority not in VALID_PRIORITIES:
            priority = "medium"

        now = _utcnow()
        related_json = json.dumps(list(related_files or []))

        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM learnings WHERE pattern_key = ? AND status = 'pending' "
                "ORDER BY last_seen DESC LIMIT 1",
                (pattern_key,),
            ).fetchone()

            if existing is not None:
                new_recurrence = int(existing["recurrence_count"]) + 1
                new_distinct = int(existing["distinct_tasks"])
                if task_id and task_id != (existing["last_task_id"] or ""):
                    new_distinct += 1
                conn.execute(
                    "UPDATE learnings SET "
                    "  recurrence_count = ?,"
                    "  last_seen = ?,"
                    "  distinct_tasks = ?,"
                    "  last_task_id = COALESCE(?, last_task_id),"
                    "  details = CASE WHEN ?<>'' THEN ? ELSE details END,"
                    "  suggested_action = CASE WHEN ?<>'' THEN ? ELSE suggested_action END,"
                    "  priority = CASE WHEN ?<>'' THEN ? ELSE priority END,"
                    "  area = CASE WHEN ?<>'' THEN ? ELSE area END,"
                    "  related_files_json = CASE WHEN ?<>'' THEN ? ELSE related_files_json END "
                    "WHERE id = ?",
                    (
                        new_recurrence,
                        now,
                        new_distinct,
                        task_id or None,
                        details, details,
                        suggested_action, suggested_action,
                        priority, priority,
                        area, area,
                        related_json if related_files else "",
                        related_json,
                        existing["id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM learnings WHERE id = ?", (existing["id"],)
                ).fetchone()
            else:
                # ID-collision retry loop. With 6-char hex suffix the
                # daily key space is 16M; under heavy same-day write
                # rates (5k+ records / day) the birthday-paradox
                # collision probability is non-trivial (~70% at 5k).
                # Try a few fresh IDs before bubbling up the error.
                # See [BUG-M13-1].
                last_exc: Optional[sqlite3.IntegrityError] = None
                row = None
                for _attempt in range(8):
                    new_id = _format_id(category, now)
                    try:
                        conn.execute(
                            "INSERT INTO learnings ("
                            "  id, category, subcategory, pattern_key, summary, details, "
                            "  suggested_action, priority, status, area, recurrence_count, "
                            "  first_seen, last_seen, distinct_tasks, last_task_id, "
                            "  related_files_json"
                            ") VALUES (?,?,?,?,?,?,?,?, 'pending',?,1,?,?,1,?,?)",
                            (
                                new_id,
                                category,
                                subcategory or "",
                                pattern_key,
                                summary,
                                details,
                                suggested_action,
                                priority,
                                area,
                                now,
                                now,
                                task_id or None,
                                related_json,
                            ),
                        )
                        row = conn.execute(
                            "SELECT * FROM learnings WHERE id = ?", (new_id,)
                        ).fetchone()
                        last_exc = None
                        break
                    except sqlite3.IntegrityError as exc:
                        last_exc = exc
                        # Loop: _format_id pulls a fresh uuid4() each call.
                        continue
                if row is None:
                    # All retries collided — vanishingly unlikely (8
                    # tries × 16M space ≈ ε). Re-raise so the caller
                    # sees the real error rather than masking it.
                    if last_exc is not None:
                        raise last_exc
                    raise sqlite3.IntegrityError(
                        "could not allocate a fresh learning id after retries"
                    )

        result = self._row_to_dict(row)
        result["eligible_for_promotion"] = is_eligible_for_promotion(
            result, self.promotion_rule
        )
        return result

    def get(self, learning_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM learnings WHERE id = ?", (learning_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        area: Optional[str] = None,
        limit: int = 50,
        order_by: str = "last_seen DESC",
    ) -> List[Dict[str, Any]]:
        """Return entries filtered by status / category / area.

        ``status='all'`` or ``None`` returns every status.  Same for
        ``category`` and ``area``.  The ``order_by`` argument is white-listed
        to prevent SQL injection — only a handful of safe orderings allowed.
        """
        conn = self._connect()

        clauses: List[str] = []
        params: List[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if category and category != "all":
            clauses.append("category = ?")
            params.append(category)
        if area:
            clauses.append("area = ?")
            params.append(area)

        # Safe whitelist for ORDER BY to avoid SQL injection.
        allowed_orders = {
            "last_seen DESC",
            "last_seen ASC",
            "first_seen DESC",
            "first_seen ASC",
            "recurrence_count DESC",
            "priority DESC",
        }
        if order_by not in allowed_orders:
            order_by = "last_seen DESC"

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM learnings {where} ORDER BY {order_by} LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def mark_promoted(self, learning_id: str, *, target: str) -> Dict[str, Any]:
        """Mark ``learning_id`` as promoted and snapshot ``promoted_at``.

        ``target`` is free-form: ``rules`` for a rule promotion or
        ``skill:<name>`` for a skill extraction.
        """
        if not target:
            raise ValueError("target is required for mark_promoted")
        now = _utcnow()
        new_status = "promoted_to_skill" if target.startswith("skill:") else "promoted"
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE learnings SET status = ?, promoted_to = ?, promoted_at = ? "
                "WHERE id = ?",
                (new_status, target, now, learning_id),
            )
            if cur.rowcount == 0:
                return {"success": False, "error": f"learning {learning_id!r} not found"}
        return {"success": True, "id": learning_id, "promoted_to": target, "status": new_status}

    def mark_resolved(self, learning_id: str, *, notes: str = "") -> Dict[str, Any]:
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE learnings SET status = 'resolved', resolution_notes = ? "
                "WHERE id = ?",
                (notes or "", learning_id),
            )
            if cur.rowcount == 0:
                return {"success": False, "error": f"learning {learning_id!r} not found"}
        return {"success": True, "id": learning_id, "status": "resolved"}

    def stats(self) -> Dict[str, Any]:
        """High-level counts for the ``/learn stats`` command."""
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]

        by_status: Dict[str, int] = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM learnings GROUP BY status"
        ):
            by_status[row[0]] = int(row[1])

        by_category: Dict[str, int] = {}
        for row in conn.execute(
            "SELECT category, COUNT(*) FROM learnings GROUP BY category"
        ):
            by_category[row[0]] = int(row[1])

        # Recently active = anything seen in the last 24h.
        cutoff_24h = _utcnow() - 86400
        recent_24h = conn.execute(
            "SELECT COUNT(*) FROM learnings WHERE last_seen >= ?", (cutoff_24h,)
        ).fetchone()[0]

        eligible = 0
        for row in conn.execute(
            "SELECT * FROM learnings WHERE status = 'pending' AND promoted_to IS NULL"
        ):
            if is_eligible_for_promotion(self._row_to_dict(row), self.promotion_rule):
                eligible += 1

        return {
            "total": int(total),
            "by_status": by_status,
            "by_category": by_category,
            "recent_24h": int(recent_24h),
            "eligible_for_promotion": int(eligible),
        }

    def eligible_pending(self) -> List[Dict[str, Any]]:
        """Return all entries currently eligible for auto-promotion."""
        conn = self._connect()
        out: List[Dict[str, Any]] = []
        for row in conn.execute(
            "SELECT * FROM learnings WHERE status = 'pending' AND promoted_to IS NULL "
            "ORDER BY recurrence_count DESC, last_seen DESC"
        ):
            entry = self._row_to_dict(row)
            if is_eligible_for_promotion(entry, self.promotion_rule):
                out.append(entry)
        return out

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        d = dict(row)
        # Normalise empty strings → None for optional fields the consumers
        # check via ``if d.get(...)``.
        if not d.get("promoted_to"):
            d["promoted_to"] = None
        return d
