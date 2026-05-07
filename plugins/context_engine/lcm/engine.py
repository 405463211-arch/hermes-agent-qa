"""LCMEngine — retrieval-based context engine.

Replaces the built-in compressor's ``summarize-and-discard`` with
``embed-and-stash``: every message that would otherwise be dropped is
embedded into a local SQLite store, and the agent is given two tools
(``lcm_search`` / ``lcm_recall``) to pull specific old turns back on
demand instead of working off a lossy summary.

This keeps the active context tight while letting the agent recover any
specific detail (file path, error message, function name) it needs from
earlier in the conversation — the cost of recall is one extra tool call
per question, not a full LLM summary call per compression.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from agent.context_engine import ContextEngine
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    estimate_messages_tokens_rough,
    get_model_context_length,
)
from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home

from .embedder import Embedder, get_default_embedder
from .store import ChunkStore, _make_preview

logger = logging.getLogger(__name__)


_LCM_MARKER_PREFIX = "[LCM INDEX]"
_DEFAULT_TAIL_TOKEN_BUDGET_FRACTION = 0.20  # of threshold_tokens
_DEFAULT_PROTECT_FIRST_N = 3
_DEFAULT_PROTECT_LAST_N = 6


def _content_to_text(content: Any) -> str:
    """Flatten any OpenAI-format content (str / list of blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _message_searchable_text(msg: Dict[str, Any]) -> str:
    """Build the text we feed to the embedder for a single message.

    Kept for backward compatibility with code paths that still want a
    single big string per message (tests, fallback paths).  The
    ``compress`` path now uses :func:`_split_message_into_chunks`
    instead, which returns multiple sub-chunks per message for
    finer-grained retrieval.
    """
    role = msg.get("role", "user")
    content = _content_to_text(msg.get("content"))
    parts = [f"[{role.upper()}]", content] if content else [f"[{role.upper()}]"]

    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            name = fn.get("name") or "?"
            args_str = fn.get("arguments") or ""
        else:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "?") if fn else "?"
            args_str = getattr(fn, "arguments", "") if fn else ""
        if isinstance(args_str, str) and len(args_str) > 400:
            args_str = args_str[:400] + "..."
        parts.append(f"[TOOL CALL] {name}({args_str})")

    return "\n".join(parts).strip()


# ---------------------------------------------------------------------
# Fine-grained chunking
# ---------------------------------------------------------------------

# Soft upper bound (in characters, not tokens — cheap to count).  Long
# tool results (file reads, grep dumps, build logs) get split into
# overlapping segments so each chunk's embedding represents a coherent
# slice of content rather than averaging across an entire 50K-line
# dump.  Numbers chosen so a single bge-m3 chunk stays well below its
# 8192-token input cap (4 chars/token heuristic ≈ 6000 chars ≈ 1500
# tokens).
_TOOL_RESULT_SOFT_LIMIT_CHARS = 6000
_TOOL_RESULT_SEGMENT_CHARS = 4000
_TOOL_RESULT_OVERLAP_CHARS = 200
# How far we're willing to slide the cut to land on a sentence/paragraph
# boundary instead of mid-word.  10% of segment size is a common RAG
# heuristic — wider than this and the size guarantee weakens; narrower
# and we miss most natural boundaries in dense text.
_TOOL_RESULT_SEGMENT_SLACK = 400


# Boundary patterns for sentence-aware splitting, in priority order.  We
# prefer to cut at the strongest available boundary near the target
# position; only fall through to weaker boundaries when no stronger one
# is in range.  Both Chinese (。！？) and English (. ! ?) sentence
# terminators are matched.  The English pattern requires trailing
# whitespace / EOL so URLs ("example.com") and decimals ("3.14") aren't
# treated as sentence ends.
_BOUNDARY_PATTERNS: tuple = (
    ("paragraph", re.compile(r"\n\s*\n")),
    ("cn_sentence", re.compile(r"[。！？][」』）)\]】]*\s*")),
    ("en_sentence", re.compile(r"[.!?][\")\]]*(?=\s|$)")),
    ("newline", re.compile(r"\n")),
    ("cn_clause", re.compile(r"[；，][」』）)\]】]*")),
    ("en_clause", re.compile(r"[;,](?=\s|$)")),
    ("space", re.compile(r"\s+")),
)


def _find_smart_cut(text: str, target: int, slack: int) -> int:
    """Return a cut position near ``target`` aligned to a natural boundary.

    Searches the window ``[target - slack, target + slack]`` (clipped to
    the string) for matches of each boundary pattern in priority order.
    Returns the END position of the boundary closest to ``target``;
    falls back to ``target`` when no boundary is found in the window.

    Picking the closest match (rather than the latest one before
    ``target``) avoids systematically biasing every chunk slightly
    short — for prose where boundaries are dense this matters a lot.
    """
    n = len(text)
    target = max(0, min(target, n))
    lo = max(0, target - slack)
    hi = min(n, target + slack)
    if lo >= hi or hi <= 0:
        return target
    window = text[lo:hi]
    local_target = target - lo
    for _name, pat in _BOUNDARY_PATTERNS:
        best: Optional[int] = None
        best_dist: Optional[int] = None
        for m in pat.finditer(window):
            end = m.end()
            dist = abs(end - local_target)
            if best is None or dist < best_dist:  # type: ignore[operator]
                best = end
                best_dist = dist
        if best is not None:
            return lo + best
    return target


def _next_chunk_start(text: str, raw_start: int, prev_end: int) -> int:
    """Snap the next-chunk start to a boundary so overlap doesn't begin mid-word.

    We start ``_TOOL_RESULT_OVERLAP_CHARS`` characters before the previous
    cut to preserve cross-boundary context.  But if that landing position
    falls in the middle of a word, the overlap chunk's first sentence is
    a fragment ("...the file was created") which embeds noisily.  Walk
    forward from ``raw_start`` to the next clean boundary (capped by
    ``prev_end`` so we always make progress).
    """
    n = len(text)
    raw_start = max(0, min(raw_start, n))
    prev_end = max(raw_start, min(prev_end, n))
    if raw_start >= prev_end:
        return raw_start
    # Look at the slice from raw_start up to prev_end for the first
    # boundary; if found, use the position right after it.
    window = text[raw_start:prev_end]
    for _name, pat in _BOUNDARY_PATTERNS:
        m = pat.search(window)
        if m:
            return raw_start + m.end()
    return raw_start


def _summarize_tool_calls_for_decision(tool_calls: List[Any]) -> List[str]:
    """Render assistant tool_calls as compact one-liners for the decision chunk."""
    out: List[str] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            name = fn.get("name") or "?"
            args_str = fn.get("arguments") or ""
        else:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "?") if fn else "?"
            args_str = getattr(fn, "arguments", "") if fn else ""
        if isinstance(args_str, str) and len(args_str) > 400:
            args_str = args_str[:400] + "..."
        out.append(f"[TOOL CALL] {name}({args_str})")
    return out


def _segment_long_text(text: str) -> List[str]:
    """Split ``text`` into overlapping windows when it exceeds the soft cap.

    Sentence-aware: each cut is snapped to the nearest paragraph break,
    sentence terminator (Chinese 。！？ / English . ! ?), newline, or
    clause boundary within ±``_TOOL_RESULT_SEGMENT_SLACK`` characters of
    the target position.  Falls back to the raw character position only
    when the window contains no boundary at all (e.g. embedded base64,
    pure binary dumps).

    Why this matters: embedding "...he started writing the function" as
    one chunk and "void main() { ... }" as the next produces two clean
    semantic vectors.  A naive char-cut might land mid-word and split it
    into "...he started writ" and "ing the function void main()..." —
    both vectors then carry mixed signal and recall worse.

    Returns ``[text]`` unchanged for short content.  Overlap preserves
    cross-boundary context so a query mentioning a term that lies on a
    cut line still matches one of the adjacent segments.
    """
    n = len(text)
    if n <= _TOOL_RESULT_SOFT_LIMIT_CHARS:
        return [text]

    segments: List[str] = []
    start = 0
    # Hard guard: if the loop ever fails to make forward progress (should
    # be impossible — _next_chunk_start always returns >= raw_start) we
    # bail out instead of looping forever.  Use the conservative
    # estimate of len(text) // step iterations as the upper bound.
    max_iters = max(8, (n // max(1, _TOOL_RESULT_SEGMENT_CHARS)) + 4)
    iters = 0

    while start < n and iters < max_iters:
        iters += 1
        target_end = start + _TOOL_RESULT_SEGMENT_CHARS
        if target_end >= n:
            segments.append(text[start:n])
            break
        cut = _find_smart_cut(text, target_end, _TOOL_RESULT_SEGMENT_SLACK)
        # Defensive: never produce an empty segment if smart-cut snapped
        # backward past start.  Fall back to a raw window in that case.
        if cut <= start:
            cut = min(start + _TOOL_RESULT_SEGMENT_CHARS, n)
        segments.append(text[start:cut])
        if cut >= n:
            break
        # Step back by the overlap budget, then snap forward to the next
        # clean boundary so the overlap region begins at a sentence /
        # paragraph break rather than mid-word.
        raw_next = max(start + 1, cut - _TOOL_RESULT_OVERLAP_CHARS)
        start = _next_chunk_start(text, raw_next, cut)

    return segments


def _split_message_into_chunks(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split a single OpenAI-format message into one or more LCM chunks.

    Returns a list of chunk records (the same shape ``ChunkStore.add``
    expects, minus the embedding).  Each record has::

        {
            "role":        original message role,
            "content":     the text we'll embed AND store verbatim,
            "preview":     ChunkStore will fill this if missing,
            "chunk_type":  one of {assistant_decision, tool_call,
                                   tool_result, user_text, system_text},
        }

    Empty / system-noise messages return ``[]`` so the caller can
    skip them.

    Why we split (vs. one chunk per message):

    * A single assistant turn often carries a paragraph of reasoning
      AND 5+ tool_calls.  Embedding the whole thing averages the
      semantics so a query for a specific tool name (or one of the
      arguments) doesn't score nearly as well as embedding each
      ``[TOOL CALL] name(args)`` line independently.
    * Tool results from ``read_file`` / ``search_files`` /
      ``terminal`` can be enormous.  A 50 KB blob embedded as one
      vector ends up near the centroid of "long technical text" and
      retrieves badly.  Splitting into ~4000-char overlapping segments
      keeps each vector representative of an actual passage.

    The chunk's relative position in the returned list also matters:
    ``compress`` flushes them to the store in order, so SQLite's
    autoincrement ``id`` preserves chronology.  Neighbour expansion in
    ``store.search`` then walks ``id±N`` to recover sibling chunks
    from the same original message.
    """
    role = msg.get("role", "user")
    if role == "system":
        # System prompts are reconstructed each turn; never index them.
        return []

    text_content = _content_to_text(msg.get("content")).strip()
    tool_calls = msg.get("tool_calls") or []
    chunks: List[Dict[str, Any]] = []

    if role == "assistant":
        # 1) The assistant's reasoning + tool_call dispatch decision.
        #    Even when content is empty we still emit a tiny chunk
        #    listing the tool_calls so the decision is searchable.
        decision_parts: List[str] = ["[ASSISTANT]"]
        if text_content:
            decision_parts.append(text_content)
        decision_parts.extend(_summarize_tool_calls_for_decision(tool_calls))
        decision_text = "\n".join(decision_parts).strip()
        if decision_text and decision_text != "[ASSISTANT]":
            chunks.append(
                {
                    "role": role,
                    "content": decision_text,
                    "chunk_type": "assistant_decision",
                }
            )
        return chunks

    if role == "tool":
        # Tool-result message — content is what came back from the tool.
        if not text_content:
            return []
        tool_name = msg.get("name") or msg.get("tool_name") or ""
        header = f"[TOOL RESULT{f' {tool_name}' if tool_name else ''}]"
        for segment in _segment_long_text(text_content):
            chunks.append(
                {
                    "role": role,
                    "content": f"{header}\n{segment}",
                    "chunk_type": "tool_result",
                }
            )
        return chunks

    # user / fallback role
    if not text_content:
        return []
    for segment in _segment_long_text(text_content):
        chunks.append(
            {
                "role": role,
                "content": f"[{role.upper()}]\n{segment}",
                "chunk_type": (
                    "user_text" if role == "user" else f"{role}_text"
                ),
            }
        )
    return chunks


_SIMPLE_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "to", "of", "in", "on", "at", "for", "with",
        "by", "as", "this", "that", "it", "i", "you", "we", "they", "he", "she",
        "我", "你", "的", "了", "是", "在", "有", "和", "就", "都", "也", "上", "下",
        "tool", "user", "assistant", "system",
    }
)


def _topic_keywords(texts: List[str], k: int = 8) -> List[str]:
    """Cheap keyword summary so the agent has a hint about what's indexed."""
    counter: Counter[str] = Counter()
    for text in texts:
        for token in text.lower().split():
            tok = "".join(ch for ch in token if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
            if not tok or tok in _SIMPLE_STOPWORDS or tok.isnumeric():
                continue
            if len(tok) < 2:
                continue
            counter[tok] += 1
    return [t for t, _ in counter.most_common(k)]


def _align_boundary_forward(messages: List[Dict[str, Any]], idx: int) -> int:
    """Move *idx* forward past any tool-result messages so we don't split a pair."""
    n = len(messages)
    while idx < n:
        msg = messages[idx]
        role = msg.get("role")
        # If the boundary lands on a 'tool' result, advance past it so the
        # corresponding assistant tool_calls (which precedes) is preserved
        # together with its results.
        if role == "tool":
            idx += 1
            continue
        return idx
    return idx


def _find_tail_cut_by_tokens(
    messages: List[Dict[str, Any]], compress_start: int, tail_token_budget: int,
    min_tail: int,
) -> int:
    """Return the index where the tail begins (last messages we keep verbatim)."""
    n = len(messages)
    accumulated = 0
    boundary = n
    min_protect = min(min_tail, n - compress_start)
    for i in range(n - 1, compress_start - 1, -1):
        msg = messages[i]
        # Cheap token estimate: chars / 4 + 10
        text = _content_to_text(msg.get("content"))
        tokens = len(text) // 4 + 10
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                args = (tc.get("function") or {}).get("arguments", "") or ""
                tokens += len(args) // 4
        if accumulated + tokens > tail_token_budget and (n - i) >= min_protect:
            boundary = i + 1
            break
        accumulated += tokens
        boundary = i
    # Don't cross compress_start
    return max(boundary, compress_start)


def _sanitize_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip orphaned tool messages whose call_id no longer exists.

    After we drop the middle range, an assistant tool_call in the head may
    reference a tool result that's been moved out (or vice versa). The API
    rejects mismatched pairs with a 400; this drops the orphans.
    """
    valid_call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                cid = tc.get("id")
            else:
                cid = getattr(tc, "id", None)
            if cid:
                valid_call_ids.add(str(cid))

    cleaned: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid and str(cid) not in valid_call_ids:
                continue  # orphan — drop
        cleaned.append(msg)

    # Also drop assistant tool_calls whose result is missing — easier to drop
    # the call than fabricate a result.
    answered_call_ids: set[str] = set()
    for msg in cleaned:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                answered_call_ids.add(str(cid))

    final: List[Dict[str, Any]] = []
    for msg in cleaned:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            kept_calls = []
            for tc in msg["tool_calls"]:
                cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if cid and str(cid) in answered_call_ids:
                    kept_calls.append(tc)
            if kept_calls:
                msg = {**msg, "tool_calls": kept_calls}
            else:
                msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                if not _content_to_text(msg.get("content")):
                    continue  # nothing left to say
        final.append(msg)
    return final


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LCMEngine(ContextEngine):
    """Retrieval-based context engine."""

    name = "lcm"

    # ContextEngine fields read by run_agent.py
    threshold_percent: float = 0.75
    protect_first_n: int = _DEFAULT_PROTECT_FIRST_N
    protect_last_n: int = _DEFAULT_PROTECT_LAST_N

    def __init__(
        self,
        threshold_percent: float = 0.75,
        protect_first_n: int = _DEFAULT_PROTECT_FIRST_N,
        protect_last_n: int = _DEFAULT_PROTECT_LAST_N,
        store: Optional[ChunkStore] = None,
        embedder: Optional[Embedder] = None,
        embedder_model: Optional[str] = None,
        embedder_device: Optional[str] = None,
        prefer_local_embedder: bool = True,
        embedder_max_seq_length: Optional[int] = None,
        embedder_batch_size: Optional[int] = None,
    ):
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n

        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.context_length = 0
        self.threshold_tokens = 0
        self.compression_count = 0

        self.model = ""
        self.base_url = ""
        self.api_key = ""
        self.provider = ""

        self._session_id: Optional[str] = None

        # Lazy init of store + embedder so a missing API key / library
        # doesn't break engine construction at startup.
        self._store: Optional[ChunkStore] = store
        self._embedder: Optional[Embedder] = embedder
        self._init_failed: Optional[str] = None

        # User-overridable embedder selection (read by _ensure_embedder).
        # Defaults preserve previous behaviour (all-MiniLM-L6-v2 if local
        # sentence-transformers is installed). Recommend BAAI/bge-m3 for
        # multilingual + long-context workloads.
        self._embedder_model: str = embedder_model or "all-MiniLM-L6-v2"
        self._embedder_device: Optional[str] = embedder_device
        self._prefer_local_embedder: bool = prefer_local_embedder
        # Performance knobs for the local embedder.  Both default to None
        # so the embedder picks safe per-device defaults; users with bigger
        # GPUs (or who want to push closer to the model's native context
        # window) can override via lcm.embedder_max_seq_length /
        # lcm.embedder_batch_size in config.yaml.
        self._embedder_max_seq_length: Optional[int] = embedder_max_seq_length
        self._embedder_batch_size: Optional[int] = embedder_batch_size

    # ------------------------------------------------------------------
    # Lazy initialisation
    # ------------------------------------------------------------------

    def _ensure_store(self) -> ChunkStore:
        if self._store is not None:
            return self._store
        db_path = get_hermes_home() / "lcm" / "store.db"
        self._store = ChunkStore(db_path)
        logger.info("LCM store: %s", db_path)
        return self._store

    def _ensure_embedder(self) -> Embedder:
        if self._embedder is not None:
            return self._embedder
        # Try to reuse SiliconFlow creds if the user's main provider points
        # there. SiliconFlow gives free embeddings and the user already has
        # a key in config.yaml.
        sf_key: Optional[str] = None
        sf_base = "https://api.siliconflow.cn/v1"
        if self.base_url and "siliconflow" in self.base_url and self.api_key:
            sf_key = self.api_key
            sf_base = self.base_url.rstrip("/")
        else:
            sf_key = (
                os.getenv("SILICONFLOW_API_KEY")
                or os.getenv("SILICON_FLOW_API_KEY")
            )
        self._embedder = get_default_embedder(
            siliconflow_api_key=sf_key,
            siliconflow_base_url=sf_base,
            sentence_transformer_model=self._embedder_model,
            sentence_transformer_device=self._embedder_device,
            prefer_local=self._prefer_local_embedder,
            sentence_transformer_max_seq_length=self._embedder_max_seq_length,
            sentence_transformer_batch_size=self._embedder_batch_size,
        )
        return self._embedder

    # ------------------------------------------------------------------
    # ContextEngine interface
    # ------------------------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        if not isinstance(usage, dict):
            return
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(
            usage.get("total_tokens") or
            (self.last_prompt_tokens + self.last_completion_tokens)
        )

    def should_compress(self, prompt_tokens: int = None) -> bool:
        tokens = (
            prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        )
        if not self.threshold_tokens:
            return False
        return tokens >= self.threshold_tokens

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.provider = provider
        self.context_length = int(context_length or 0)
        self.threshold_tokens = max(
            int(self.context_length * self.threshold_percent),
            MINIMUM_CONTEXT_LENGTH,
        )

    def on_session_start(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        # Resolve context_length lazily if it wasn't already set via update_model
        if not self.context_length:
            try:
                self.context_length = get_model_context_length(
                    kwargs.get("model") or self.model,
                    base_url=kwargs.get("base_url") or self.base_url,
                    api_key=kwargs.get("api_key") or self.api_key,
                    provider=kwargs.get("provider") or self.provider,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("LCM: get_model_context_length failed: %s", e)
            self.threshold_tokens = max(
                int(self.context_length * self.threshold_percent),
                MINIMUM_CONTEXT_LENGTH,
            )

    def on_session_end(
        self, session_id: str, messages: List[Dict[str, Any]]
    ) -> None:
        # Keep DB across sessions; don't auto-delete. User can call
        # delete_session() externally if they want to clean up.
        return None

    # ------------------------------------------------------------------
    # Tool schemas
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "lcm_search",
                "description": (
                    "Semantic search over earlier messages that were moved to "
                    "the long-term memory store during context compression. "
                    "Use this when you need details from earlier in the "
                    "conversation that are no longer in your active context "
                    "(referenced by a [LCM INDEX] marker). Returns the top-K "
                    "matching chunks plus an optional window of neighbouring "
                    "chunks for causal context; each result is tagged "
                    "matched=true|false. Call lcm_recall with the IDs to "
                    "read full content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language description of "
                            "what you're looking for in past turns.",
                        },
                        "k": {
                            "type": "integer",
                            "description": "How many top matches to return (default 5, max 20).",
                            "default": 5,
                        },
                        "neighbors": {
                            "type": "integer",
                            "description": (
                                "How many adjacent chunks (per side) to "
                                "pull alongside each match for causal "
                                "context — 0 disables, default 1 (so each "
                                "hit also brings its previous + next "
                                "chunk), max 3."
                            ),
                            "default": 1,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "lcm_recall",
                "description": (
                    "Fetch the full content of one or more previously-indexed "
                    "chunks by ID. Get the IDs from lcm_search results."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chunk_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "List of chunk IDs returned by lcm_search.",
                        }
                    },
                    "required": ["chunk_ids"],
                },
            },
        ]

    def handle_tool_call(
        self, name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        try:
            if name == "lcm_search":
                return self._handle_search(args)
            if name == "lcm_recall":
                return self._handle_recall(args)
            return json.dumps({"error": f"Unknown LCM tool: {name}"})
        except Exception as e:  # noqa: BLE001 — surface to model as JSON
            logger.error("LCM tool %s failed: %s", name, e, exc_info=True)
            return json.dumps({"error": f"{name} failed: {e}"})

    def _handle_search(self, args: Dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "query is required"})
        k = max(1, min(int(args.get("k") or 5), 20))
        # Default to 1 — the cheap-and-useful "bring sibling context"
        # behaviour described in the tool schema.  Cap at 3 so a
        # k=20 / neighbors=3 query can't dump 140 chunks at the model.
        try:
            neighbors = int(args.get("neighbors", 1))
        except (TypeError, ValueError):
            neighbors = 1
        neighbors = max(0, min(neighbors, 3))

        if not self._session_id:
            return json.dumps({"error": "no active session"})

        store = self._ensure_store()
        embedder = self._ensure_embedder()
        query_emb = embedder.embed([query])[0]
        # Embedder mismatch (different dim than what was stored) is silently
        # filtered by store.search — fine, we just return whatever matches.
        results = store.search(
            self._session_id, query_emb, k=k, embedder_filter=embedder.name
        )
        # Fallback: if dim filter eliminated everything (e.g. embedder
        # changed mid-session), retry without filter.
        if not results:
            results = store.search(self._session_id, query_emb, k=k)

        # Build the matched core first so we can annotate scores.
        matched_by_id: Dict[int, Dict[str, Any]] = {}
        for r in results:
            matched_by_id[int(r["id"])] = {
                "id": int(r["id"]),
                "role": r["role"],
                "score": round(r["score"], 4),
                "preview": r["preview"],
                "matched": True,
            }

        # Pull neighbour windows around each hit.  Walk by id and keep
        # the first time we see each chunk so we don't duplicate when
        # adjacent matches' windows overlap.  The matched flag stays
        # True for genuine hits; pure neighbours get matched=false +
        # neighbor_of pointing at the hit that pulled them in.
        ordered: Dict[int, Dict[str, Any]] = {}
        for r in results:
            cid = int(r["id"])
            if neighbors == 0:
                if cid not in ordered:
                    ordered[cid] = matched_by_id[cid]
                continue
            window = store.neighbors(
                self._session_id, cid, before=neighbors, after=neighbors,
            )
            for nb in window:
                nb_id = int(nb["id"])
                if nb_id in ordered:
                    continue
                if nb_id in matched_by_id:
                    ordered[nb_id] = matched_by_id[nb_id]
                else:
                    ordered[nb_id] = {
                        "id": nb_id,
                        "role": nb["role"],
                        "preview": nb["preview"],
                        "matched": False,
                        "neighbor_of": cid,
                    }

        # Sort by id so the agent reads the slice in chronological
        # order — without this, a neighbour pulled in by an earlier
        # hit could appear after a later hit's main chunk.
        ordered_list = [ordered[k_] for k_ in sorted(ordered.keys())]

        return json.dumps(
            {
                "matches": ordered_list,
                "matched_count": sum(1 for r in ordered_list if r.get("matched")),
                "neighbor_count": sum(
                    1 for r in ordered_list if not r.get("matched")
                ),
                "neighbors_window": neighbors,
                "embedder": embedder.name,
                "total_indexed": store.session_chunk_count(self._session_id),
            },
            ensure_ascii=False,
        )

    def _handle_recall(self, args: Dict[str, Any]) -> str:
        raw_ids = args.get("chunk_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return json.dumps({"error": "chunk_ids must be a non-empty list of integers"})
        try:
            ids = [int(x) for x in raw_ids]
        except (TypeError, ValueError):
            return json.dumps({"error": "chunk_ids must contain integers only"})
        ids = ids[:20]  # cap to avoid blowing context
        store = self._ensure_store()
        rows = store.recall(ids)
        return json.dumps(
            {
                "chunks": [
                    {"id": r["id"], "role": r["role"], "content": r["content"]}
                    for r in rows
                ]
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # The main act: compress() — embed instead of summarize
    # ------------------------------------------------------------------

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        # focus_topic is accepted for signature compatibility with the
        # built-in ContextCompressor (which uses it to bias the LLM
        # summary).  LCM is retrieval-based — every chunk is preserved
        # verbatim in the SQLite store, and the agent can pull any
        # earlier turn back via lcm_search/lcm_recall — so a "focus"
        # hint adds no value here.  The arg is kept so run_agent.py's
        # _compress_context can call .compress(...) uniformly across
        # engines without branching on engine type.
        del focus_topic  # explicitly unused
        if not messages:
            return messages
        if not self._session_id:
            # Without a session we can't store anything; degrade silently.
            logger.warning("LCM.compress called with no active session — skipping")
            return messages

        n = len(messages)
        min_required = self.protect_first_n + self.protect_last_n + 1
        if n <= min_required:
            return messages

        compress_start = self.protect_first_n
        compress_start = _align_boundary_forward(messages, compress_start)

        # Tail budget = a fraction of the model's compression threshold,
        # with a small floor so tiny test conversations also exercise the
        # boundary logic (production threshold_tokens are 100K+ so the
        # fraction always dominates the floor).
        tail_budget = max(
            int((self.threshold_tokens or 8000) * _DEFAULT_TAIL_TOKEN_BUDGET_FRACTION),
            200,
        )
        compress_end = _find_tail_cut_by_tokens(
            messages, compress_start, tail_budget, self.protect_last_n,
        )

        if compress_start >= compress_end:
            return messages

        middle = messages[compress_start:compress_end]
        if not middle:
            return messages

        # Embed middle messages
        try:
            embedder = self._ensure_embedder()
            store = self._ensure_store()
        except Exception as e:  # noqa: BLE001
            self._init_failed = str(e)
            logger.error("LCM init failed (%s) — falling back to passthrough", e)
            return messages

        # Fine-grained chunking: each message is split into multiple
        # sub-chunks (assistant decision, individual tool results,
        # segments of large tool output).  Embedding each unit
        # independently dramatically improves retrieval precision over
        # the previous "one chunk per message" scheme — see
        # _split_message_into_chunks for the rationale.  Insertion
        # order is preserved so SQLite's autoincrement id keeps
        # chronology, which neighbour expansion in store.search relies
        # on to walk back to sibling chunks.
        chunk_records: List[Dict[str, Any]] = []
        searchable_texts: List[str] = []
        for msg in middle:
            for sub in _split_message_into_chunks(msg):
                text = redact_sensitive_text(sub["content"])
                if not text:
                    continue
                sub["content"] = text
                sub["preview"] = _make_preview(text)
                chunk_records.append(sub)
                searchable_texts.append(text)

        if not chunk_records:
            return messages

        try:
            embeddings = embedder.embed(searchable_texts)
        except Exception as e:  # noqa: BLE001
            logger.error("LCM embedding failed (%s) — falling back to passthrough", e)
            return messages

        try:
            new_ids = store.add(
                self._session_id, chunk_records, embeddings, embedder.name,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("LCM store.add failed (%s) — falling back to passthrough", e)
            return messages

        # Build the marker message that takes the middle's place
        keywords = _topic_keywords(searchable_texts)
        keyword_str = ", ".join(keywords) if keywords else ""
        first_id, last_id = new_ids[0], new_ids[-1]
        total_in_session = store.session_chunk_count(self._session_id)

        marker_text = (
            f"{_LCM_MARKER_PREFIX} {len(new_ids)} earlier turns "
            f"(chunk IDs #{first_id}-#{last_id}) were moved to the long-term "
            f"memory store. Total in this session: {total_in_session}.\n"
            f"To recall any of them, call lcm_search(\"<your query>\") then "
            f"lcm_recall(chunk_ids=[...]). Do NOT answer questions you see "
            f"summarised in earlier markers — they were already addressed."
        )
        if keyword_str:
            marker_text += f"\nTopics indexed: {keyword_str}"

        # Pick a role for the marker that doesn't break alternation
        last_head_role = (
            messages[compress_start - 1].get("role", "user")
            if compress_start > 0
            else "user"
        )
        marker_role = "user" if last_head_role == "assistant" else "assistant"

        compressed: List[Dict[str, Any]] = []
        compressed.extend(messages[:compress_start])
        compressed.append({"role": marker_role, "content": marker_text})
        compressed.extend(messages[compress_end:])

        compressed = _sanitize_tool_pairs(compressed)

        # Update accounting
        self.compression_count += 1
        try:
            new_estimate = estimate_messages_tokens_rough(compressed)
            saved = max(0, (current_tokens or 0) - new_estimate)
            logger.info(
                "LCM compression #%d: dropped %d msgs → indexed %d chunks "
                "(approx %d tokens freed; total chunks in session: %d, embedder=%s)",
                self.compression_count,
                len(middle),
                len(new_ids),
                saved,
                total_in_session,
                embedder.name,
            )
            self.last_prompt_tokens = new_estimate
            self.last_completion_tokens = 0
        except Exception:
            pass

        return compressed

    def on_session_reset(self) -> None:
        super().on_session_reset()
        # Don't blow away the store on /new — user may want to come back
        # to old chunks via a future query. New session_id will scope
        # searches correctly.

    # ------------------------------------------------------------------
    # Status / display
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status()
        if self._session_id:
            try:
                base["lcm_indexed_chunks"] = self._ensure_store().session_chunk_count(
                    self._session_id
                )
            except Exception:
                base["lcm_indexed_chunks"] = -1
        if self._embedder is not None:
            base["lcm_embedder"] = self._embedder.name
        return base

    # ------------------------------------------------------------------
    # Public introspection for the /lcm slash command
    # ------------------------------------------------------------------

    def get_db_path(self) -> Path:
        """Path to the SQLite store (creates parent dir if needed)."""
        return get_hermes_home() / "lcm" / "store.db"

    def get_db_size_bytes(self) -> int:
        try:
            p = self.get_db_path()
            return int(p.stat().st_size) if p.exists() else 0
        except Exception:
            return 0

    def get_total_chunks(self) -> int:
        """Total chunks across all sessions in this DB."""
        try:
            store = self._ensure_store()
            with store._lock:
                row = store._conn.execute(
                    "SELECT COUNT(*) FROM chunks"
                ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return -1

    def get_session_chunks(self) -> int:
        """Chunks indexed for the currently active session, or -1 if unknown."""
        if not self._session_id:
            return 0
        try:
            return int(self._ensure_store().session_chunk_count(self._session_id))
        except Exception:
            return -1

    def get_embedder_info(self) -> Dict[str, Any]:
        """Currently active embedder details. Triggers lazy init."""
        try:
            emb = self._ensure_embedder()
            model_name = getattr(emb, "model_name", None)
            if not model_name:
                _m = getattr(emb, "_model", None)
                if isinstance(_m, str):
                    model_name = _m
                elif _m is not None:
                    model_name = _m.__class__.__name__
            device = getattr(emb, "device", None) or self._embedder_device or "auto"
            return {
                "name": emb.name,
                "dim": int(getattr(emb, "dim", 0)),
                "model": model_name or "unknown",
                "device": str(device),
                "configured_model": self._embedder_model,
            }
        except Exception as e:
            return {
                "name": "unavailable",
                "dim": 0,
                "model": self._embedder_model or "unknown",
                "device": self._embedder_device or "auto",
                "error": str(e),
            }

    def clear_current_session(self) -> int:
        """Delete all chunks for the currently active session. Returns count deleted."""
        if not self._session_id:
            return 0
        try:
            return int(self._ensure_store().delete_session(self._session_id))
        except Exception:
            return -1
