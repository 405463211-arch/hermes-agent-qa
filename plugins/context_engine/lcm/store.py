"""SQLite-backed chunk store with cosine-similarity search.

One DB file per Hermes home (``$HERMES_HOME/lcm/store.db``).  Each chunk
row holds the verbatim text + its embedding vector and is associated to
one or more sessions through the ``chunk_sessions`` link table.  This
many-to-many shape lets us **dedup identical content across sessions**
(``--resume``, fork, parallel processes, compression-driven session
rotation in ``run_agent.py``) without losing per-session retrieval —
the same chunk row is simply attached to every session that produced
the same text.

Search is plain brute-force cosine similarity in numpy.  For the
message volumes a single conversation produces (a few thousand chunks
max), this runs in tens of milliseconds — no need for an ANN index.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Schema notes:
# * ``chunks.session_id`` is kept for backward compatibility — it now
#   records the FIRST session that ever observed this content.  All
#   per-session reads/writes go through ``chunk_sessions``.
# * ``content_hash`` is sha256 over (role, chunk_type, content) so a
#   tool result and an assistant decision with the same text are still
#   treated as different chunks.  Used together with ``(dim, embedder)``
#   as the dedup key — different embedders produce incompatible vectors.
# * ``chunk_sessions.seq`` is a per-session monotonic sequence used by
#   neighbour expansion (id-based windows broke once chunks could be
#   shared, since "id+1" might belong to a different session).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    preview TEXT NOT NULL,
    embedding BLOB,
    embedder TEXT,
    dim INTEGER,
    chunk_type TEXT DEFAULT 'message',
    content_hash TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_session_dim ON chunks(session_id, dim);
-- idx_chunks_hash_lookup is created in _migrate_legacy() because legacy
-- DBs hit this script with a chunks table that pre-dates the
-- content_hash column — building the index here would fail on those.

CREATE TABLE IF NOT EXISTS chunk_sessions (
    chunk_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    attached_at REAL NOT NULL,
    PRIMARY KEY (chunk_id, session_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunk_sessions_session_seq
    ON chunk_sessions(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_chunk_sessions_chunk
    ON chunk_sessions(chunk_id);
"""


def _make_preview(content: str, max_chars: int = 200) -> str:
    """Collapse whitespace and clip to a 1-line preview."""
    if not content:
        return ""
    s = " ".join(str(content).split())
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."


def _chunk_content_hash(role: str, chunk_type: str, content: str) -> str:
    """Stable SHA-256 over (role, chunk_type, content) for dedup lookup.

    Including ``role``/``chunk_type`` keeps a tool result distinct from
    an assistant decision that happens to render to the same body —
    they retrieve differently and shouldn't share a row.  ``content``
    is whatever ``_split_message_into_chunks`` produced, which already
    embeds the ``[ROLE]`` / ``[TOOL RESULT name]`` header so two
    identical message bodies under different tool names won't collide.
    """
    h = hashlib.sha256()
    h.update((role or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((chunk_type or "message").encode("utf-8"))
    h.update(b"\x00")
    h.update((content or "").encode("utf-8"))
    return h.hexdigest()


class ChunkStore:
    """Persistent chunk + embedding store."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because Hermes touches the engine from
        # the main thread and the compaction-progress ticker; we still
        # serialise writes through ``self._lock``.
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        # ON DELETE CASCADE on chunk_sessions.chunk_id requires foreign
        # keys turned on for THIS connection — SQLite defaults it off.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()
        # Per-write dedup statistics from the most recent ``add()`` call.
        # The engine reads this to surface accurate "+N indexed / N reused
        # / 0 new (dedup hit)" status, so the user can tell apart a real
        # embedder failure from the "second compression on the same
        # messages" / "cross-session content already stored" cases.
        # Keys: new (int), reused (int, hit on existing chunks attached
        # via INSERT in chunk_sessions), already_attached (int, both
        # chunk and chunk_sessions row already existed → INSERT OR IGNORE
        # no-op), input_chunks (int, total inputs).
        self.last_add_stats: Optional[Dict[str, int]] = None
        self._migrate_legacy()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate_legacy(self) -> None:
        """Bring older DBs up to the current schema (idempotent).

        Pre-dedup DBs only had the ``chunks`` table with a single
        ``session_id`` column.  We need to:

        1. Add the ``content_hash`` column if it's missing (older
           ``CREATE TABLE`` ran without it).
        2. Backfill ``chunk_sessions`` from ``chunks.session_id`` so
           every legacy row has exactly one attachment row.
        3. Compute SHA-256 hashes for any rows where ``content_hash``
           is still NULL (SQLite has no built-in sha256 — done in
           Python).

        All three steps are no-ops on a fresh DB and on a DB already
        migrated, so it's safe to run on every ``__init__``.
        """
        with self._lock:
            cols = {
                row[1] for row in self._conn.execute("PRAGMA table_info(chunks)")
            }
            if "content_hash" not in cols:
                self._conn.execute(
                    "ALTER TABLE chunks ADD COLUMN content_hash TEXT"
                )
            # Always (re)create the dedup-lookup index here, AFTER we
            # know content_hash exists.  CREATE INDEX IF NOT EXISTS is
            # cheap when it's already there and avoids fragile ordering
            # in _SCHEMA against legacy tables.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_hash_lookup "
                "ON chunks(content_hash, dim, embedder)"
            )

            # Backfill chunk_sessions for chunks that have no attachment
            # row yet.  ROW_NUMBER() over PARTITION BY session_id keeps
            # the per-session insertion order — SQLite has supported this
            # since 3.25 (Sept 2018), comfortably below our floor.
            self._conn.execute(
                "INSERT OR IGNORE INTO chunk_sessions "
                "(chunk_id, session_id, seq, attached_at) "
                "SELECT c.id, c.session_id, "
                "       ROW_NUMBER() OVER ("
                "           PARTITION BY c.session_id ORDER BY c.id"
                "       ), "
                "       c.created_at "
                "FROM chunks c "
                "WHERE NOT EXISTS ("
                "    SELECT 1 FROM chunk_sessions cs WHERE cs.chunk_id = c.id"
                ")"
            )

            rows = self._conn.execute(
                "SELECT id, role, chunk_type, content "
                "FROM chunks WHERE content_hash IS NULL"
            ).fetchall()
            if rows:
                cur = self._conn.cursor()
                cur.execute("BEGIN")
                try:
                    for cid, role, chunk_type, content in rows:
                        h = _chunk_content_hash(
                            role, chunk_type or "message", content or ""
                        )
                        cur.execute(
                            "UPDATE chunks SET content_hash = ? WHERE id = ?",
                            (h, cid),
                        )
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
                logger.info(
                    "LCM store: backfilled content_hash for %d legacy chunks",
                    len(rows),
                )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(
        self,
        session_id: str,
        chunks: List[Dict[str, Any]],
        embeddings: np.ndarray,
        embedder_name: str,
    ) -> List[int]:
        """Insert chunks (with cross-session content dedup).

        For each input chunk we look up ``(content_hash, dim, embedder)``;
        if a matching row already exists anywhere in the DB we reuse it
        and just attach the existing chunk to ``session_id`` via
        ``chunk_sessions``.  Otherwise we insert a new ``chunks`` row
        AND its first ``chunk_sessions`` attachment.

        This is what stops the same content being stored twice when a
        conversation is replayed under a different ``session_id`` (the
        ``--resume`` / fork / compression-rotation paths in run_agent).

        Returns the chunk IDs (one per input chunk) in input order — IDs
        may belong to rows that pre-dated this call when dedup hit.

        Side effect: populates ``self.last_add_stats`` with
        ``{"new", "reused", "already_attached", "input_chunks"}`` so the
        engine can surface dedup-vs-truly-new accurately to the user
        instead of mis-reporting a 0-delta as an "embedder failure".
        """
        if not chunks:
            self.last_add_stats = {
                "new": 0,
                "reused": 0,
                "already_attached": 0,
                "input_chunks": 0,
            }
            return []
        if len(chunks) != embeddings.shape[0]:
            raise ValueError(
                f"chunks ({len(chunks)}) and embeddings ({embeddings.shape[0]}) "
                "must have the same length"
            )

        now = time.time()
        dim = int(embeddings.shape[1])
        ids: List[int] = []
        new_count = 0
        reused_count = 0
        already_attached_count = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                # Per-session monotonic sequence for chunk_sessions —
                # neighbour expansion walks ±N positions from a hit, so
                # we need a dense ordering that's stable across dedup.
                row = cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM chunk_sessions "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                next_seq = int(row[0]) + 1

                for i, ch in enumerate(chunks):
                    role = ch.get("role", "user")
                    content = ch.get("content", "") or ""
                    preview = ch.get("preview") or _make_preview(content)
                    chunk_type = ch.get("chunk_type", "message")
                    chash = _chunk_content_hash(role, chunk_type, content)

                    existing = cur.execute(
                        "SELECT id FROM chunks "
                        "WHERE content_hash = ? AND dim = ? AND embedder = ? "
                        "LIMIT 1",
                        (chash, dim, embedder_name),
                    ).fetchone()

                    if existing:
                        chunk_id = int(existing[0])
                        # INSERT OR IGNORE handles the "same content
                        # already attached to this session" case (e.g.
                        # if compress() ran twice on overlapping ranges).
                        cur.execute(
                            "INSERT OR IGNORE INTO chunk_sessions "
                            "(chunk_id, session_id, seq, attached_at) "
                            "VALUES (?, ?, ?, ?)",
                            (chunk_id, session_id, next_seq, now),
                        )
                        if cur.rowcount > 0:
                            next_seq += 1
                            reused_count += 1
                        else:
                            already_attached_count += 1
                        ids.append(chunk_id)
                        continue

                    emb = embeddings[i].astype(np.float32, copy=False).tobytes()
                    cur.execute(
                        "INSERT INTO chunks "
                        "(session_id, role, content, preview, embedding, "
                        " embedder, dim, chunk_type, content_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            role,
                            content,
                            preview,
                            emb,
                            embedder_name,
                            dim,
                            chunk_type,
                            chash,
                            now,
                        ),
                    )
                    chunk_id = int(cur.lastrowid)
                    cur.execute(
                        "INSERT INTO chunk_sessions "
                        "(chunk_id, session_id, seq, attached_at) "
                        "VALUES (?, ?, ?, ?)",
                        (chunk_id, session_id, next_seq, now),
                    )
                    next_seq += 1
                    new_count += 1
                    ids.append(chunk_id)

                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

        if reused_count or already_attached_count:
            logger.info(
                "LCM dedup: session=%s new=%d reused_from_other_session=%d "
                "already_in_session=%d",
                session_id, new_count, reused_count, already_attached_count,
            )
        self.last_add_stats = {
            "new": new_count,
            "reused": reused_count,
            "already_attached": already_attached_count,
            "input_chunks": len(chunks),
        }
        return ids

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def search(
        self,
        session_id: str,
        query_embedding: np.ndarray,
        k: int = 5,
        embedder_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Top-K cosine-similarity search within a session."""
        if k <= 0:
            return []
        target = np.asarray(query_embedding, dtype=np.float32).ravel()
        if target.size == 0:
            return []
        target_norm = float(np.linalg.norm(target)) + 1e-8

        # JOIN chunk_sessions so a session sees every chunk attached to
        # it — including chunks first inserted by another session that
        # got deduped (same content_hash + dim + embedder).
        sql = (
            "SELECT c.id, c.role, c.preview, c.content, c.embedding, "
            "       c.dim, c.embedder, c.created_at "
            "FROM chunks c "
            "JOIN chunk_sessions cs ON cs.chunk_id = c.id "
            "WHERE cs.session_id = ? AND c.embedding IS NOT NULL "
            "AND c.dim = ?"
        )
        params: List[Any] = [session_id, int(target.size)]
        if embedder_filter:
            sql += " AND c.embedder = ?"
            params.append(embedder_filter)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        scored: List[Dict[str, Any]] = []
        for cid, role, preview, content, emb_blob, dim, embedder, created_at in rows:
            if not emb_blob:
                continue
            emb = np.frombuffer(emb_blob, dtype=np.float32)
            denom = (float(np.linalg.norm(emb)) + 1e-8) * target_norm
            score = float(np.dot(emb, target) / denom)
            scored.append(
                {
                    "id": int(cid),
                    "role": role,
                    "preview": preview,
                    "content": content,
                    "score": score,
                    "embedder": embedder,
                    "created_at": created_at,
                }
            )

        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]

    def recall(self, chunk_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch full content for the given chunk IDs."""
        if not chunk_ids:
            return []
        ids = [int(x) for x in chunk_ids]
        placeholder = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, role, content, preview, created_at "
                f"FROM chunks WHERE id IN ({placeholder})",
                ids,
            ).fetchall()
        order = {cid: i for i, cid in enumerate(ids)}
        return sorted(
            (
                {
                    "id": int(r[0]),
                    "role": r[1],
                    "content": r[2],
                    "preview": r[3],
                    "created_at": r[4],
                }
                for r in rows
            ),
            key=lambda d: order.get(d["id"], 0),
        )

    def neighbors(
        self,
        session_id: str,
        chunk_id: int,
        before: int = 1,
        after: int = 1,
    ) -> List[Dict[str, Any]]:
        """Return chunks immediately surrounding ``chunk_id`` in this session.

        Used by the LCM engine to expand a search hit with its sibling
        chunks — when a single message gets split into multiple chunks
        (assistant decision + several tool results), retrieving only
        the matching one drops the surrounding causal context.

        ``before`` / ``after`` count chunks, NOT messages.  We walk the
        per-session ``chunk_sessions.seq`` rather than ``chunks.id``
        because dedup means "id+1" can belong to a completely different
        session — seq is a dense per-session sequence that survives
        row sharing.

        The matched chunk itself is included in the result; entries
        are ordered by ``seq`` ascending so the caller can render them
        as a coherent timeline.
        """
        if before < 0:
            before = 0
        if after < 0:
            after = 0
        cid = int(chunk_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT seq FROM chunk_sessions "
                "WHERE chunk_id = ? AND session_id = ?",
                (cid, session_id),
            ).fetchone()
            if not row:
                # Chunk isn't actually attached to this session — return
                # just the chunk itself if it exists at all so the
                # caller still gets the matched row back.  This keeps
                # the function defensive against stale ids passed by
                # callers without raising.
                solo = self._conn.execute(
                    "SELECT id, role, content, preview, chunk_type, created_at "
                    "FROM chunks WHERE id = ?",
                    (cid,),
                ).fetchone()
                if not solo:
                    return []
                return [
                    {
                        "id": int(solo[0]),
                        "role": solo[1],
                        "content": solo[2],
                        "preview": solo[3],
                        "chunk_type": solo[4],
                        "created_at": solo[5],
                    }
                ]
            seq = int(row[0])
            lo = seq - before
            hi = seq + after
            rows = self._conn.execute(
                "SELECT c.id, c.role, c.content, c.preview, c.chunk_type, "
                "       c.created_at "
                "FROM chunk_sessions cs "
                "JOIN chunks c ON c.id = cs.chunk_id "
                "WHERE cs.session_id = ? AND cs.seq BETWEEN ? AND ? "
                "ORDER BY cs.seq ASC",
                (session_id, lo, hi),
            ).fetchall()
        return [
            {
                "id": int(r[0]),
                "role": r[1],
                "content": r[2],
                "preview": r[3],
                "chunk_type": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def session_chunk_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM chunk_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def delete_session(self, session_id: str) -> int:
        """Detach this session and garbage-collect chunks no other session needs.

        Under the dedup schema a chunk row can be shared by multiple
        sessions, so we can't just ``DELETE FROM chunks WHERE session_id=?``
        — that would also nuke chunks the user still wants in *other*
        sessions that share the same content.  Instead:

        1. Drop attachments in ``chunk_sessions``.
        2. Then drop ``chunks`` rows that no longer have ANY attachment.
           (FK cascades from chunks → chunk_sessions only, not the
           reverse direction we need here.)

        Returns the number of attachment rows removed — same value
        the legacy implementation returned for the single-session
        DELETE path, so existing callers/tests stay green.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM chunk_sessions WHERE session_id = ?",
                (session_id,),
            )
            detached = int(cur.rowcount or 0)
            # GC orphan chunks.  Cheap subquery — we already have an
            # index on chunk_sessions.chunk_id.
            self._conn.execute(
                "DELETE FROM chunks "
                "WHERE id NOT IN (SELECT chunk_id FROM chunk_sessions)"
            )
        return detached

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
