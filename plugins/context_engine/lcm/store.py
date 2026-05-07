"""SQLite-backed chunk store with cosine-similarity search.

One DB file per Hermes home (``$HERMES_HOME/lcm/store.db``); session_id
is a column so old sessions can be cleaned up with a DELETE.

Search is plain brute-force cosine similarity in numpy. For the message
volumes a single conversation produces (a few thousand chunks max), this
runs in tens of milliseconds — no need for an ANN index.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

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
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_session_dim ON chunks(session_id, dim);
"""


def _make_preview(content: str, max_chars: int = 200) -> str:
    """Collapse whitespace and clip to a 1-line preview."""
    if not content:
        return ""
    s = " ".join(str(content).split())
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."


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
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()

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
        """Insert chunks with their embeddings. Returns list of new IDs."""
        if not chunks:
            return []
        if len(chunks) != embeddings.shape[0]:
            raise ValueError(
                f"chunks ({len(chunks)}) and embeddings ({embeddings.shape[0]}) "
                "must have the same length"
            )

        now = time.time()
        ids: List[int] = []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for i, ch in enumerate(chunks):
                    content = ch.get("content", "") or ""
                    preview = ch.get("preview") or _make_preview(content)
                    emb = embeddings[i].astype(np.float32, copy=False).tobytes()
                    cur.execute(
                        "INSERT INTO chunks "
                        "(session_id, role, content, preview, embedding, "
                        " embedder, dim, chunk_type, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            ch.get("role", "user"),
                            content,
                            preview,
                            emb,
                            embedder_name,
                            int(embeddings.shape[1]),
                            ch.get("chunk_type", "message"),
                            now,
                        ),
                    )
                    ids.append(int(cur.lastrowid))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
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

        sql = (
            "SELECT id, role, preview, content, embedding, dim, embedder, "
            "       created_at "
            "FROM chunks WHERE session_id = ? AND embedding IS NOT NULL "
            "AND dim = ?"
        )
        params: List[Any] = [session_id, int(target.size)]
        if embedder_filter:
            sql += " AND embedder = ?"
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

        ``before`` / ``after`` count chunks, NOT messages.  Chunks are
        identified by SQLite's autoincrement ``id``, which preserves
        insertion order across the entire DB but neighbours are
        further filtered to the same ``session_id`` so we never bleed
        across conversations even if their chunk ids happen to be
        adjacent.

        The matched chunk itself is included in the result; entries
        are ordered by ``id`` ascending so the caller can render them
        as a coherent timeline.
        """
        if before < 0:
            before = 0
        if after < 0:
            after = 0
        cid = int(chunk_id)
        lo = cid - before
        hi = cid + after
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, preview, chunk_type, created_at "
                "FROM chunks "
                "WHERE session_id = ? AND id BETWEEN ? AND ? "
                "ORDER BY id ASC",
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
                "SELECT COUNT(*) FROM chunks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def delete_session(self, session_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM chunks WHERE session_id = ?", (session_id,)
            )
        return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
