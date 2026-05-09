"""Tests for the LCM context engine plugin.

Cover the basic round-trip:
  * Lexical embedder produces normalised vectors of the right shape.
  * ChunkStore round-trips inserts → search → recall.
  * LCMEngine.compress() drops middle messages and inserts an LCM marker.
  * lcm_search / lcm_recall tools return the indexed chunks.

We force the lexical embedder so tests stay hermetic (no network calls,
no model downloads).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from plugins.context_engine.lcm.embedder import (
    LexicalEmbedder,
    get_default_embedder,
)
from plugins.context_engine.lcm.engine import (
    LCMEngine,
    _LCM_MARKER_PREFIX,
    _sanitize_tool_pairs,
    _segment_long_text,
    _split_message_into_chunks,
    _TOOL_RESULT_SEGMENT_CHARS,
    _TOOL_RESULT_SOFT_LIMIT_CHARS,
)
from plugins.context_engine.lcm.store import ChunkStore, _chunk_content_hash


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("SILICON_FLOW_API_KEY", raising=False)
    return home


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


class TestLexicalEmbedder:
    def test_returns_correct_shape(self):
        emb = LexicalEmbedder(dim=128)
        out = emb.embed(["hello world", "another doc", "third one"])
        assert out.shape == (3, 128)
        assert out.dtype == np.float32

    def test_empty_input_returns_empty_matrix(self):
        emb = LexicalEmbedder(dim=64)
        out = emb.embed([])
        assert out.shape == (0, 64)

    def test_l2_normalised(self):
        emb = LexicalEmbedder(dim=64)
        out = emb.embed(["the quick brown fox jumps over"])
        norm = float(np.linalg.norm(out[0]))
        assert pytest.approx(norm, abs=1e-5) == 1.0

    def test_similar_texts_more_similar_than_distinct(self):
        emb = LexicalEmbedder(dim=512)
        v = emb.embed(
            [
                "支付流程在 pay module 里",
                "支付流程在 payment module 里",
                "完全无关的天气话题",
            ]
        )
        sim_close = float(np.dot(v[0], v[1]))
        sim_far = float(np.dot(v[0], v[2]))
        assert sim_close > sim_far


class TestEmbedderFallback:
    def test_default_falls_back_to_lexical_without_deps(self, monkeypatch):
        # Force sentence-transformers import to fail
        import importlib

        def _fake_import(name, *a, **kw):
            if name.startswith("sentence_transformers"):
                raise ImportError("forced")
            return importlib.__import__(name, *a, **kw)

        monkeypatch.setattr("builtins.__import__", _fake_import)
        emb = get_default_embedder(siliconflow_api_key=None)
        assert emb.name == "lexical-hash"


# ---------------------------------------------------------------------------
# ChunkStore
# ---------------------------------------------------------------------------


class TestChunkStore:
    def _make_store(self, tmp_path: Path) -> ChunkStore:
        return ChunkStore(tmp_path / "lcm" / "store.db")

    def test_round_trip_insert_search_recall(self, tmp_path):
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=128)

        chunks = [
            {"role": "user", "content": "支付流程的代码在哪里"},
            {"role": "assistant", "content": "在 pay module 下，文件 pay_service.dart"},
            {"role": "user", "content": "今天天气怎么样"},
        ]
        embeddings = emb.embed([c["content"] for c in chunks])
        ids = store.add("session-1", chunks, embeddings, embedder_name="lexical-hash")

        assert len(ids) == 3
        assert all(isinstance(i, int) for i in ids)
        assert store.session_chunk_count("session-1") == 3

        # Search for payment-related — should rank pay chunks higher than weather
        query_emb = emb.embed(["支付 pay module"])[0]
        results = store.search("session-1", query_emb, k=3)
        assert len(results) == 3
        # The weather chunk should have lowest score
        weather = [r for r in results if "天气" in r["preview"]][0]
        non_weather = [r for r in results if "天气" not in r["preview"]]
        assert all(r["score"] >= weather["score"] for r in non_weather)

        # Recall full content
        rows = store.recall([ids[0], ids[1]])
        assert len(rows) == 2
        assert rows[0]["id"] == ids[0]
        assert "支付流程" in rows[0]["content"]
        assert "pay_service.dart" in rows[1]["content"]

    def test_search_isolates_by_session(self, tmp_path):
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        chunks_a = [{"role": "user", "content": "session A content"}]
        chunks_b = [{"role": "user", "content": "session B content"}]
        store.add("A", chunks_a, emb.embed(["session A content"]), "lexical-hash")
        store.add("B", chunks_b, emb.embed(["session B content"]), "lexical-hash")

        query = emb.embed(["session content"])[0]
        a_results = store.search("A", query, k=10)
        b_results = store.search("B", query, k=10)
        assert len(a_results) == 1
        assert len(b_results) == 1
        assert "A" in a_results[0]["preview"]
        assert "B" in b_results[0]["preview"]

    def test_search_filters_by_dim(self, tmp_path):
        """Mismatched embedder dims should be filtered out."""
        store = self._make_store(tmp_path)
        emb_64 = LexicalEmbedder(dim=64)
        emb_128 = LexicalEmbedder(dim=128)
        store.add(
            "S",
            [{"role": "user", "content": "old"}],
            emb_64.embed(["old"]),
            "lexical-hash",
        )
        store.add(
            "S",
            [{"role": "user", "content": "new"}],
            emb_128.embed(["new"]),
            "lexical-hash",
        )

        # Query with 128-dim — should only see the 128-dim chunk
        results = store.search("S", emb_128.embed(["query"])[0], k=5)
        assert len(results) == 1
        assert "new" in results[0]["preview"]

    def test_delete_session(self, tmp_path):
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        store.add(
            "to-delete",
            [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}],
            emb.embed(["x", "y"]),
            "lexical-hash",
        )
        assert store.session_chunk_count("to-delete") == 2
        deleted = store.delete_session("to-delete")
        assert deleted == 2
        assert store.session_chunk_count("to-delete") == 0


# ---------------------------------------------------------------------------
# Engine.compress()
# ---------------------------------------------------------------------------


def _make_engine(tmp_path) -> LCMEngine:
    """Engine with lexical embedder + tmpdir store, ready to use.

    We override ``threshold_tokens`` directly so the small messages in
    these tests actually exceed the tail budget — production uses 100K+
    context windows where the natural calculation is fine, but
    ``update_model`` floors at MINIMUM_CONTEXT_LENGTH (64K) which would
    let all 12 short test messages fit in the tail.
    """
    store = ChunkStore(tmp_path / "lcm" / "store.db")
    emb = LexicalEmbedder(dim=128)
    eng = LCMEngine(
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=3,
        store=store,
        embedder=emb,
    )
    eng.update_model("test-model", context_length=64_000)
    eng.threshold_tokens = 800  # force a small tail budget for the tests
    eng.on_session_start("test-session")
    return eng


class TestLCMEngineCompress:
    def test_compress_returns_unchanged_when_too_few_messages(self, tmp_path):
        eng = _make_engine(tmp_path)
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = eng.compress(msgs, current_tokens=100)
        assert out == msgs

    def test_compress_indexes_middle_and_inserts_marker(self, tmp_path):
        eng = _make_engine(tmp_path)
        # Make messages long enough that they exceed the test tail budget
        # (~200 tokens / ~800 chars).
        long_chunk = "x " * 200  # ~400 chars ≈ 100 tokens per message
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "starter question about pay module"},
            {"role": "assistant", "content": "first answer " + long_chunk},
            # middle (will be indexed)
            {"role": "user", "content": "middle question 1 " + long_chunk},
            {"role": "assistant", "content": "middle answer 1 " + long_chunk},
            {"role": "user", "content": "middle question 2 " + long_chunk},
            {"role": "assistant", "content": "middle answer 2 " + long_chunk},
            {"role": "user", "content": "middle question 3 " + long_chunk},
            {"role": "assistant", "content": "middle answer 3 " + long_chunk},
            # tail (kept verbatim)
            {"role": "user", "content": "recent question " + long_chunk},
            {"role": "assistant", "content": "recent answer " + long_chunk},
            {"role": "user", "content": "latest"},
        ]
        out = eng.compress(msgs, current_tokens=8000)

        # Should be shorter
        assert len(out) < len(msgs)

        # Marker must be present
        marker_msgs = [
            m for m in out if isinstance(m.get("content"), str)
            and _LCM_MARKER_PREFIX in m["content"]
        ]
        assert len(marker_msgs) == 1, "exactly one LCM marker should be inserted"

        # Compression count incremented
        assert eng.compression_count == 1

        # Some chunks should now be in the store
        chunk_count = eng._ensure_store().session_chunk_count("test-session")
        assert chunk_count > 0

        # Head must be preserved verbatim
        assert out[0] == msgs[0]
        assert out[1] == msgs[1]

        # Tail (last message) must be preserved verbatim
        assert out[-1] == msgs[-1]

    def test_compress_falls_back_to_passthrough_when_embedder_init_fails(
        self, tmp_path, monkeypatch
    ):
        eng = _make_engine(tmp_path)
        # Force the embedder lookup path to blow up
        eng._embedder = None
        eng._store = None  # Force re-init

        def _boom(*a, **kw):
            raise RuntimeError("simulated embedder failure")

        monkeypatch.setattr(eng, "_ensure_embedder", _boom)
        msgs = [{"role": "system", "content": "system"}]
        for i in range(20):
            msgs.append(
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            )
        out = eng.compress(msgs, current_tokens=8000)
        # Returned unchanged on failure
        assert out == msgs


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


class TestLCMTools:
    def test_search_returns_matches_after_compress(self, tmp_path):
        eng = _make_engine(tmp_path)
        msgs = [{"role": "system", "content": "sys"}]
        # Build enough messages to trigger compression
        for i in range(25):
            role = "user" if i % 2 == 0 else "assistant"
            content = (
                f"支付 pay module 流程 step {i}"
                if i < 10
                else f"weather chat number {i}"
            )
            msgs.append({"role": role, "content": content})
        eng.compress(msgs, current_tokens=10000)

        result = json.loads(
            eng.handle_tool_call("lcm_search", {"query": "支付 pay", "k": 3})
        )
        assert "matches" in result
        assert len(result["matches"]) > 0
        # The top match should mention pay/支付, not weather
        top = result["matches"][0]
        assert "支付" in top["preview"] or "pay" in top["preview"].lower()

    def test_recall_returns_full_content(self, tmp_path):
        eng = _make_engine(tmp_path)
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(15):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"unique-token-{i} more content here"})
        eng.compress(msgs, current_tokens=10000)

        # Search WITHOUT neighbour expansion so the only entry in
        # `matches` is the actual top-scoring chunk for our query.
        # (Default neighbors=1 would also return id-1 / id+1 for
        # context, which is desirable in real use but adds noise here.)
        search = json.loads(
            eng.handle_tool_call(
                "lcm_search",
                {"query": "unique-token-3", "k": 1, "neighbors": 0},
            )
        )
        assert search.get("matches")
        # Pick the chunk explicitly tagged as a real hit, not a neighbour.
        matched = [m for m in search["matches"] if m.get("matched")]
        assert matched, f"no matched=true entry in {search['matches']!r}"
        chunk_id = matched[0]["id"]

        # Recall it
        recall = json.loads(
            eng.handle_tool_call("lcm_recall", {"chunk_ids": [chunk_id]})
        )
        assert "chunks" in recall
        assert len(recall["chunks"]) == 1
        assert recall["chunks"][0]["id"] == chunk_id
        assert "unique-token-3" in recall["chunks"][0]["content"]

    def test_search_with_empty_query_returns_error(self, tmp_path):
        eng = _make_engine(tmp_path)
        result = json.loads(eng.handle_tool_call("lcm_search", {"query": "  "}))
        assert "error" in result

    def test_search_finds_memory_archive_bucket(self, tmp_path):
        """Regression: ``MemoryStore._archive_oldest_to_lcm_locked`` writes
        overflowed MEMORY.md entries into the bucket
        ``"memory:" + session_id`` so they're segregated from compression
        chunks. The previous ``lcm_search`` only looked at the bare
        session bucket, which made the ``Auto-archived to LCM`` notice
        misleading: chunks existed but the agent could never retrieve
        them. ``_handle_search`` now searches both buckets — verify by
        seeding the archive bucket directly and checking the chunk
        comes back through the tool surface.
        """
        eng = _make_engine(tmp_path)
        store = eng._ensure_store()
        embedder = eng._ensure_embedder()

        archived_text = (
            "占位记忆条目 archived-token-7 关于 ssh StrictHostKeyChecking 配置"
        )
        emb_array = embedder.embed([archived_text])
        store.add(
            session_id=f"memory:{eng._session_id}",
            chunks=[{
                "role": "memory_archive",
                "content": archived_text,
                "chunk_type": "memory_archive",
            }],
            embeddings=emb_array,
            embedder_name=embedder.name,
        )

        result = json.loads(
            eng.handle_tool_call(
                "lcm_search",
                {"query": "archived-token-7 ssh", "k": 3, "neighbors": 0},
            )
        )

        # The tool now reports both buckets.
        assert result.get("total_indexed") == 0
        assert result.get("total_indexed_archive") == 1

        # And the archived entry is reachable.
        matched = [m for m in result.get("matches", []) if m.get("matched")]
        assert matched, f"archived chunk not retrieved: {result!r}"
        assert matched[0]["role"] == "memory_archive"
        assert "archived-token-7" in matched[0]["preview"]

    def test_search_merges_compression_and_archive_results(self, tmp_path):
        """Both buckets' content must be visible to lcm_search.

        Pre-fix: only the bare ``self._session_id`` bucket was searched, so
        archive chunks were entirely invisible. The contract we lock in
        here is "results from both buckets reach the agent" — the actual
        score ordering depends on the embedder (LexicalEmbedder is word-
        bag hashing, so we don't assert on score ranking which would make
        this a flaky change-detector test).
        """
        eng = _make_engine(tmp_path)

        # Seed compression bucket via the normal compress path
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({
                "role": role,
                "content": f"compression-bucket-token-{i} weather discussion",
            })
        eng.compress(msgs, current_tokens=10000)

        # Seed archive bucket with the unique query tokens
        store = eng._ensure_store()
        embedder = eng._ensure_embedder()
        target_text = "archive-bucket-token alpha bravo charlie precise match"
        store.add(
            session_id=f"memory:{eng._session_id}",
            chunks=[{
                "role": "memory_archive",
                "content": target_text,
                "chunk_type": "memory_archive",
            }],
            embeddings=embedder.embed([target_text]),
            embedder_name=embedder.name,
        )

        result = json.loads(
            eng.handle_tool_call(
                "lcm_search",
                {"query": "archive-bucket-token alpha", "k": 10, "neighbors": 0},
            )
        )

        # Both bucket counts surface
        assert result.get("total_indexed_archive") == 1
        assert result.get("total_indexed", 0) > 0  # compression seeded chunks too

        # Critical contract: the archive chunk is reachable; pre-fix it
        # would never appear in matches at all.
        archive_hits = [
            m for m in result.get("matches", [])
            if m.get("matched") and m.get("role") == "memory_archive"
        ]
        assert archive_hits, (
            f"archive chunk missing from cross-bucket results: {result!r}"
        )
        assert "archive-bucket-token" in archive_hits[0]["preview"]

    def test_recall_with_invalid_ids_returns_error(self, tmp_path):
        eng = _make_engine(tmp_path)
        result = json.loads(
            eng.handle_tool_call("lcm_recall", {"chunk_ids": ["not-an-int"]})
        )
        assert "error" in result

    def test_unknown_tool_returns_error(self, tmp_path):
        eng = _make_engine(tmp_path)
        result = json.loads(eng.handle_tool_call("lcm_unknown", {}))
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool-pair sanitization
# ---------------------------------------------------------------------------


class TestSanitizeToolPairs:
    def test_drops_orphan_tool_message(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "x", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            # Orphan: no matching assistant call
            {"role": "tool", "tool_call_id": "call_99", "content": "lost"},
        ]
        out = _sanitize_tool_pairs(msgs)
        assert all(m.get("tool_call_id") != "call_99" for m in out)
        # Valid pair preserved
        assert any(m.get("tool_call_id") == "call_1" for m in out)

    def test_drops_assistant_call_without_result(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "talking",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "x", "arguments": "{}"}}
                ],
            },
            # No matching tool message
            {"role": "user", "content": "next"},
        ]
        out = _sanitize_tool_pairs(msgs)
        # Assistant message kept (it has content) but tool_calls stripped
        assistant_msgs = [m for m in out if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert "tool_calls" not in assistant_msgs[0]


# ---------------------------------------------------------------------------
# Fine-grained chunking
# ---------------------------------------------------------------------------


class TestSplitMessageIntoChunks:
    """Each message produces multiple targeted chunks instead of one blob."""

    def test_system_message_produces_no_chunks(self):
        # System prompt is rebuilt each turn — never indexed.
        out = _split_message_into_chunks(
            {"role": "system", "content": "you are helpful"}
        )
        assert out == []

    def test_plain_user_message_produces_single_user_text_chunk(self):
        out = _split_message_into_chunks(
            {"role": "user", "content": "find the pay flow"}
        )
        assert len(out) == 1
        assert out[0]["chunk_type"] == "user_text"
        assert out[0]["role"] == "user"
        assert "find the pay flow" in out[0]["content"]
        # Tag prefix is included so the embedder sees role context.
        assert out[0]["content"].startswith("[USER]")

    def test_assistant_text_only_message_produces_single_decision_chunk(self):
        out = _split_message_into_chunks(
            {"role": "assistant", "content": "let me look at the pay module"}
        )
        assert len(out) == 1
        assert out[0]["chunk_type"] == "assistant_decision"
        assert "pay module" in out[0]["content"]

    def test_assistant_with_tool_calls_inlines_them_in_decision_chunk(self):
        out = _split_message_into_chunks(
            {
                "role": "assistant",
                "content": "I'll grep then read",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "search_files",
                            "arguments": '{"pattern": "pay"}',
                        },
                    },
                    {
                        "id": "c2",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "pay.dart"}',
                        },
                    },
                ],
            }
        )
        # Decision chunk includes both tool_call summaries inline so a
        # search for "search_files pay" or "read_file pay.dart" finds it.
        assert len(out) == 1
        body = out[0]["content"]
        assert "[TOOL CALL] search_files" in body
        assert "[TOOL CALL] read_file" in body

    def test_assistant_with_only_tool_calls_no_text_still_produces_chunk(self):
        out = _split_message_into_chunks(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "ls",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        )
        assert len(out) == 1
        assert "[TOOL CALL] ls" in out[0]["content"]

    def test_tool_result_short_produces_single_chunk(self):
        out = _split_message_into_chunks(
            {"role": "tool", "tool_call_id": "c1",
             "name": "search_files", "content": "found 3 matches in pay.dart"}
        )
        assert len(out) == 1
        assert out[0]["chunk_type"] == "tool_result"
        assert "[TOOL RESULT search_files]" in out[0]["content"]
        assert "found 3 matches" in out[0]["content"]

    def test_tool_result_large_is_segmented(self):
        # Comfortably above the segmenter's soft cap so several
        # segments must be emitted.
        big_text = "line of pay logic " + ("x " * 8000)  # ~16KB
        assert len(big_text) > _TOOL_RESULT_SOFT_LIMIT_CHARS * 2
        out = _split_message_into_chunks(
            {"role": "tool", "tool_call_id": "c1",
             "name": "read_file", "content": big_text}
        )
        # Multiple chunks
        assert len(out) >= 3, f"expected at least 3 segments, got {len(out)}"
        # Every chunk tagged correctly
        for c in out:
            assert c["chunk_type"] == "tool_result"
            assert c["content"].startswith("[TOOL RESULT")
        # No segment exceeds the body cap (header adds a few chars)
        max_body = max(len(c["content"]) for c in out)
        assert max_body <= _TOOL_RESULT_SEGMENT_CHARS + 50

    def test_user_message_large_is_also_segmented(self):
        big_user = "context dump " + ("y " * 5000)
        assert len(big_user) > _TOOL_RESULT_SOFT_LIMIT_CHARS
        out = _split_message_into_chunks({"role": "user", "content": big_user})
        assert len(out) >= 2

    def test_segment_long_text_overlap_is_present(self):
        text = "ABCDEFGH" * 1500  # 12k chars
        segs = _segment_long_text(text)
        assert len(segs) >= 2
        # Adjacent segments must share at least the overlap window so a
        # query landing on the boundary still finds context.
        overlap = segs[0][-100:]
        assert overlap in segs[1]

    def test_segment_short_text_returns_unchanged(self):
        """Inputs under the soft cap must come back as a single segment."""
        text = "短文本，无需切分。" * 50  # ~450 chars, well under 6000
        assert _segment_long_text(text) == [text]

    def test_segment_long_text_cuts_at_chinese_sentence_boundary(self):
        """Chinese 。！？ should win over mid-word cuts when in slack range."""
        # Build a text well above the 6000-char soft limit so segmentation
        # actually fires.  Each sentence ~23 chars; we want >7000 chars.
        sentence = "这是一段中文句子，描述了一个很长的逻辑过程。"  # 23 chars
        text = sentence * 400  # ~9.2 KB — comfortably triggers segmenter
        segs = _segment_long_text(text)
        assert len(segs) >= 2
        # First segment must end right after a 。 (possibly + trailing
        # whitespace), NOT mid-character.
        assert segs[0].rstrip().endswith("。"), (
            f"expected first chunk to end at a Chinese sentence boundary, "
            f"got tail: {segs[0][-30:]!r}"
        )

    def test_segment_long_text_cuts_at_english_sentence_boundary(self):
        """English . ! ? followed by whitespace should win in slack range."""
        sentence = "This is a fairly long English sentence about systems. "
        text = sentence * 120  # ~6.5 KB
        segs = _segment_long_text(text)
        assert len(segs) >= 2
        # First chunk must end at "...systems." (possibly + space) — never
        # in the middle of "systems" or "fairly".
        tail = segs[0].rstrip()
        assert tail.endswith("."), f"expected en sentence end, got: {tail[-30:]!r}"

    def test_segment_long_text_falls_back_when_no_boundary_in_window(self):
        """Pure binary / base64-style content has no boundaries — must still split."""
        # 12 KB of contiguous ASCII letters with NO whitespace, NO punctuation.
        text = "ABCDEFGHIJ" * 1200
        segs = _segment_long_text(text)
        # Must still produce >=2 segments (correctness > prettiness).
        assert len(segs) >= 2
        # No segment may exceed segment chars + slack budget.
        from plugins.context_engine.lcm.engine import (
            _TOOL_RESULT_SEGMENT_CHARS as _SC,
            _TOOL_RESULT_SEGMENT_SLACK as _SL,
        )
        for s in segs[:-1]:  # last segment may be shorter — that's fine
            assert len(s) <= _SC + _SL

    def test_segment_long_text_overlap_starts_at_boundary(self):
        """Next chunk's start should not be mid-word when boundaries exist."""
        # Chinese paragraph with regular sentence terminators.
        sentence = "这是一段说明文字，用于触发自动切分行为。"  # 21 chars
        text = sentence * 400  # ~8.4 KB
        segs = _segment_long_text(text)
        assert len(segs) >= 2
        # Each non-first chunk should begin right AFTER a boundary char,
        # i.e. start with a "fresh" sentence (the first char shouldn't be
        # a punctuation continuation).
        for s in segs[1:]:
            # The chunk should not begin with a boundary punctuation
            # (which would indicate the cut landed mid-sentence).
            assert s[:1] not in ("。", "！", "？", "，", "；"), (
                f"chunk starts mid-sentence at boundary punct: {s[:30]!r}"
            )

    def test_segment_makes_progress_on_pathological_short_slack(self):
        """Even when no boundary exists at all, the loop must terminate."""
        # 50 KB of a single repeated character — worst case for boundary
        # search.  We just check that this returns in finite time and
        # produces a sensible number of chunks rather than looping
        # forever or returning [].
        text = "x" * 50_000
        segs = _segment_long_text(text)
        assert len(segs) >= 2
        assert "".join(segs) != ""  # not empty

    def test_empty_messages_produce_no_chunks(self):
        assert _split_message_into_chunks({"role": "user", "content": ""}) == []
        assert _split_message_into_chunks({"role": "tool", "content": ""}) == []
        assert _split_message_into_chunks(
            {"role": "assistant", "content": "", "tool_calls": []}
        ) == []


# ---------------------------------------------------------------------------
# Compress integration: each input message yields ≥1 chunk, and an
# assistant-with-tool-calls + its tool result yield distinct chunks
# ---------------------------------------------------------------------------


class TestFineGrainedCompressIntegration:
    def test_compress_creates_more_chunks_than_input_messages(self, tmp_path):
        """A message with multiple tool_calls + a big tool result should
        expand into more chunks than the raw message count."""
        eng = _make_engine(tmp_path)

        long_chunk = "x " * 200
        # 12 short messages PLUS one large tool result so the segmenter
        # actually fires.  Segmenter triggers at >6000 chars.
        big_result = "tool output " + ("y " * 4000)  # ~8KB
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "starter " + long_chunk},
            {"role": "assistant", "content": "first " + long_chunk},
            # middle (will be indexed) — 8 messages here
            {"role": "user", "content": "Q1 " + long_chunk},
            {
                "role": "assistant", "content": "thinking",
                "tool_calls": [
                    {"id": "c1", "function":
                        {"name": "read_file", "arguments": '{"path":"a"}'}},
                    {"id": "c2", "function":
                        {"name": "search", "arguments": '{"q":"pay"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file",
             "content": big_result},
            {"role": "tool", "tool_call_id": "c2", "name": "search",
             "content": "matches in pay.dart"},
            {"role": "user", "content": "Q2 " + long_chunk},
            {"role": "assistant", "content": "answer Q2 " + long_chunk},
            {"role": "user", "content": "Q3 " + long_chunk},
            {"role": "assistant", "content": "answer Q3 " + long_chunk},
            # tail
            {"role": "user", "content": "recent " + long_chunk},
            {"role": "assistant", "content": "recent ans " + long_chunk},
            {"role": "user", "content": "latest"},
        ]

        eng.compress(msgs, current_tokens=20000)
        chunk_count = eng._ensure_store().session_chunk_count("test-session")
        # Middle is 8 messages; with fine-grained chunking AND the big
        # tool result being segmented, we expect more chunks than the
        # message count.
        assert chunk_count > 8, (
            f"expected >8 chunks from fine-grained chunking, got {chunk_count}"
        )

    def test_assistant_decision_and_tool_result_become_distinct_chunks(
        self, tmp_path,
    ):
        """The whole point of fine-grained chunking: an assistant turn
        with tool_calls + the matching tool_result message must produce
        SEPARATE chunks (one ``assistant_decision``, one
        ``tool_result``) — not a single fused chunk per message group.

        Verified by inspecting the store directly rather than going
        through ``lcm_search``, because the test runs against the
        deterministic LexicalEmbedder where retrieval is dominated by
        whichever bag-of-words tokens happen to repeat most — not a
        fair stand-in for bge-m3's semantic ranking.  What matters
        for THIS test is the chunking schema, not the embedder.
        """
        eng = _make_engine(tmp_path)
        long_chunk = "x " * 200
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "intro " + long_chunk},
            {"role": "assistant", "content": "intro ans " + long_chunk},
            # middle
            {"role": "user", "content": "find table merge logic " + long_chunk},
            {
                "role": "assistant",
                "content": "I'll search the merge code",
                "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "search_files",
                        "arguments": '{"pattern":"mergeTable"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "search_files",
             "content": "found mergeTable in tables/merge_table.dart line 87"},
            {"role": "user", "content": "ok " + long_chunk},
            {"role": "assistant", "content": "done " + long_chunk},
            {"role": "user", "content": "Q3 " + long_chunk},
            {"role": "assistant", "content": "ans Q3 " + long_chunk},
            # tail
            {"role": "user", "content": "recent " + long_chunk},
            {"role": "assistant", "content": "recent ans " + long_chunk},
            {"role": "user", "content": "latest"},
        ]
        eng.compress(msgs, current_tokens=20000)

        # Inspect the raw chunks for this session.
        store = eng._ensure_store()
        with store._lock:
            rows = store._conn.execute(
                "SELECT chunk_type, role, content FROM chunks "
                "WHERE session_id = ? ORDER BY id ASC",
                ("test-session",),
            ).fetchall()
        chunk_types = [r[0] for r in rows]

        # The assistant decision (with the tool_call) is its own chunk
        assistant_decision_chunks = [
            (i, r) for i, r in enumerate(rows) if r[0] == "assistant_decision"
        ]
        assert assistant_decision_chunks, "no assistant_decision chunk emitted"
        # And it carries the tool_call summary inline so it's separately
        # searchable from the tool_result
        decision_bodies = " ".join(r[2] for _, r in assistant_decision_chunks)
        assert "search_files" in decision_bodies
        assert "mergeTable" in decision_bodies

        # The tool_result message landed in its OWN chunk(s).
        tool_result_chunks = [
            (i, r) for i, r in enumerate(rows) if r[0] == "tool_result"
        ]
        assert tool_result_chunks, "no tool_result chunk emitted"
        result_bodies = " ".join(r[2] for _, r in tool_result_chunks)
        assert "merge_table.dart" in result_bodies
        assert "line 87" in result_bodies

        # And the two chunk kinds are NOT the same row — i.e. fine
        # grained really did happen, not coalesced.
        assert assistant_decision_chunks[0][0] != tool_result_chunks[0][0]
        # Multiple chunk kinds present (the precise mix depends on
        # which middle messages get indexed, but at minimum we want
        # both assistant_decision AND tool_result represented).
        assert "assistant_decision" in chunk_types
        assert "tool_result" in chunk_types


# ---------------------------------------------------------------------------
# Neighbour expansion in lcm_search
# ---------------------------------------------------------------------------


class TestNeighborExpansion:
    def _populate_session(self, eng, n_msgs: int = 10):
        long_chunk = "x " * 200
        msgs = [{"role": "system", "content": "sys"}]
        # head
        msgs.append({"role": "user", "content": "intro " + long_chunk})
        msgs.append({"role": "assistant", "content": "ack " + long_chunk})
        # middle: alternating user/assistant with a unique sentinel each
        for i in range(n_msgs):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({
                "role": role,
                "content": f"sentinel-{i:02d} {long_chunk}",
            })
        # tail
        msgs.append({"role": "user", "content": "recent " + long_chunk})
        msgs.append({"role": "assistant", "content": "rec ans " + long_chunk})
        msgs.append({"role": "user", "content": "latest"})
        eng.compress(msgs, current_tokens=20000)

    def test_search_neighbors_zero_returns_only_matches(self, tmp_path):
        eng = _make_engine(tmp_path)
        self._populate_session(eng, n_msgs=8)
        result = json.loads(eng.handle_tool_call(
            "lcm_search",
            {"query": "sentinel-04", "k": 1, "neighbors": 0},
        ))
        assert result["neighbor_count"] == 0
        for m in result["matches"]:
            assert m.get("matched") is True

    def test_search_default_neighbors_pulls_adjacent_chunks(self, tmp_path):
        eng = _make_engine(tmp_path)
        self._populate_session(eng, n_msgs=8)
        result = json.loads(eng.handle_tool_call(
            "lcm_search",
            {"query": "sentinel-04", "k": 1},  # default neighbors=1
        ))
        # Exactly 1 hit + up to 2 neighbours (id-1, id+1) when in bounds.
        assert result["matched_count"] == 1
        assert result["neighbor_count"] >= 1
        # Neighbour entries point at the matched id and are NOT marked matched.
        matched_ids = [m["id"] for m in result["matches"] if m.get("matched")]
        for m in result["matches"]:
            if not m.get("matched"):
                assert m.get("neighbor_of") in matched_ids

    def test_search_neighbors_capped_at_three(self, tmp_path):
        eng = _make_engine(tmp_path)
        self._populate_session(eng, n_msgs=8)
        result = json.loads(eng.handle_tool_call(
            "lcm_search",
            {"query": "sentinel-04", "k": 1, "neighbors": 99},
        ))
        # Engine clamps to 3, so a single hit yields at most 7 chunks
        # (3 before + match + 3 after) — ignoring DB boundary.
        assert result["neighbors_window"] == 3
        assert len(result["matches"]) <= 7

    def test_neighbors_do_not_cross_session_boundary(self, tmp_path):
        """A search in session B must only return chunks ATTACHED to
        session B, even when adjacent ``chunks.id`` values were first
        inserted by session A.

        Under the dedup schema two sessions can legitimately share a
        chunk row when their content matches — so we verify the
        attachment side via ``chunk_sessions`` rather than the legacy
        ``chunks.session_id`` (which only records the first session).
        Session A is populated with one set of sentinels and session B
        with a *different* set, so no dedup overlap should occur and
        every returned chunk must come back as B-attached.
        """
        eng = _make_engine(tmp_path)

        def _populate_with_prefix(prefix: str, n_msgs: int = 4) -> None:
            long_chunk = "x " * 200
            msgs = [{"role": "system", "content": "sys"}]
            msgs.append({"role": "user", "content": f"intro {prefix} " + long_chunk})
            msgs.append({"role": "assistant", "content": f"ack {prefix} " + long_chunk})
            for i in range(n_msgs):
                role = "user" if i % 2 == 0 else "assistant"
                msgs.append({
                    "role": role,
                    "content": f"{prefix}-sentinel-{i:02d} {long_chunk}",
                })
            msgs.append({"role": "user", "content": f"recent {prefix} " + long_chunk})
            msgs.append({"role": "assistant", "content": f"rec ans {prefix} " + long_chunk})
            msgs.append({"role": "user", "content": f"latest-{prefix}"})
            eng.compress(msgs, current_tokens=20000)

        eng.on_session_start("session-a")
        _populate_with_prefix("alpha")
        a_chunk_count = eng._ensure_store().session_chunk_count("session-a")
        assert a_chunk_count > 0

        eng.on_session_start("session-b")
        _populate_with_prefix("bravo")
        b_chunk_count = eng._ensure_store().session_chunk_count("session-b")
        assert b_chunk_count > 0

        result = json.loads(eng.handle_tool_call(
            "lcm_search",
            {"query": "bravo-sentinel-01", "k": 5, "neighbors": 3},
        ))
        ids = [m["id"] for m in result["matches"]]
        assert ids, "expected at least one match for bravo-sentinel-01"
        store = eng._ensure_store()
        with store._lock:
            attached_to_b = {
                int(r[0]) for r in store._conn.execute(
                    f"SELECT chunk_id FROM chunk_sessions "
                    f"WHERE chunk_id IN ({','.join('?' * len(ids))}) "
                    f"AND session_id = ?",
                    ids + ["session-b"],
                ).fetchall()
            }
        for cid in ids:
            assert cid in attached_to_b, (
                f"chunk {cid} not attached to session-b — leaked across sessions"
            )


# ---------------------------------------------------------------------------
# Cross-session content dedup (the actual bug we're fixing)
# ---------------------------------------------------------------------------


class TestCrossSessionDedup:
    """Identical content under two session_ids must share one chunk row.

    Real-world triggers for the same content reaching two session_ids:
    * ``hermes --resume <old_sid>`` then natural compression in the
      resumed session — old + replayed messages compress under the new
      session_id.
    * ACP ``fork_session()`` deep-copies history into a fresh session_id.
    * ``run_agent.py`` rotates ``session_id`` on every compression
      boundary (line 9411), so a single conversation legitimately spans
      multiple session_ids over its lifetime.

    Before this fix we'd write the SAME content twice — exactly what
    the user hit (315 unique chunks → 630 rows).  These tests pin down
    the cross-session sharing contract.
    """

    def _make_store(self, tmp_path: Path) -> ChunkStore:
        return ChunkStore(tmp_path / "lcm" / "store.db")

    def test_same_content_two_sessions_shares_one_row(self, tmp_path):
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        chunks = [
            {"role": "user", "content": "shared question about pay"},
            {"role": "assistant", "content": "shared answer in pay.dart"},
        ]
        embeddings = emb.embed([c["content"] for c in chunks])

        ids_a = store.add("session-A", chunks, embeddings, "lexical-hash")
        ids_b = store.add("session-B", chunks, embeddings, "lexical-hash")

        # Same chunk IDs returned — dedup hit on every chunk.
        assert ids_a == ids_b
        # Only ONE row per unique content in the chunks table.
        with store._lock:
            row_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
        assert row_count == len(chunks), (
            f"expected {len(chunks)} unique rows after dedup, got {row_count}"
        )
        # Each session sees both chunks via its attachments.
        assert store.session_chunk_count("session-A") == len(chunks)
        assert store.session_chunk_count("session-B") == len(chunks)

    def test_same_content_same_session_idempotent(self, tmp_path):
        """Calling add() twice with the same content in one session
        must not create duplicate attachments."""
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        chunks = [{"role": "user", "content": "repeated"}]
        embeddings = emb.embed(["repeated"])

        ids_first = store.add("S", chunks, embeddings, "lexical-hash")
        ids_second = store.add("S", chunks, embeddings, "lexical-hash")
        assert ids_first == ids_second
        assert store.session_chunk_count("S") == 1
        with store._lock:
            row_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
        assert row_count == 1

    def test_different_embedders_do_not_dedup(self, tmp_path):
        """Two embedders produce incompatible vectors — must keep both rows.

        Otherwise a cross-embedder search would hit the wrong vector
        space and return garbage scores.
        """
        store = self._make_store(tmp_path)
        emb_a = LexicalEmbedder(dim=64)
        emb_b = LexicalEmbedder(dim=128)
        chunk = [{"role": "user", "content": "same text"}]

        store.add("S", chunk, emb_a.embed(["same text"]), "lexical-hash-a")
        store.add("S", chunk, emb_b.embed(["same text"]), "lexical-hash-b")

        with store._lock:
            row_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
        assert row_count == 2, (
            "different embedders must keep separate rows for the same content"
        )

    def test_role_difference_breaks_dedup(self, tmp_path):
        """Identical body under different roles is semantically different."""
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        body = "ambiguous text"
        store.add(
            "S",
            [{"role": "user", "content": body}],
            emb.embed([body]),
            "lexical-hash",
        )
        store.add(
            "S",
            [{"role": "assistant", "content": body}],
            emb.embed([body]),
            "lexical-hash",
        )
        with store._lock:
            row_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
        assert row_count == 2

    def test_search_finds_shared_chunk_from_either_session(self, tmp_path):
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=128)
        shared = [
            {"role": "user", "content": "shared payment lookup"},
            {"role": "assistant", "content": "shared answer about pay flow"},
        ]
        store.add("A", shared, emb.embed([c["content"] for c in shared]), "lex")
        store.add("B", shared, emb.embed([c["content"] for c in shared]), "lex")

        q = emb.embed(["payment"])[0]
        results_a = store.search("A", q, k=5)
        results_b = store.search("B", q, k=5)
        # Both sessions return the same shared chunks.
        assert {r["id"] for r in results_a} == {r["id"] for r in results_b}
        assert len(results_a) == 2

    def test_delete_session_keeps_chunks_shared_with_others(self, tmp_path):
        """Deleting session A must NOT remove chunks B still references."""
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        shared = [{"role": "user", "content": "shared"}]
        a_only = [{"role": "user", "content": "only-in-A"}]

        store.add("A", shared + a_only,
                  emb.embed(["shared", "only-in-A"]), "lex")
        store.add("B", shared, emb.embed(["shared"]), "lex")

        # Sanity: A has 2, B has 1 (deduped).
        assert store.session_chunk_count("A") == 2
        assert store.session_chunk_count("B") == 1

        store.delete_session("A")

        # B's attachment survived; its chunk wasn't GC'd.
        assert store.session_chunk_count("B") == 1
        b_results = store.search("B", emb.embed(["shared"])[0], k=5)
        assert any("shared" in r["preview"] for r in b_results)
        # The A-only chunk WAS garbage-collected (no other session held it).
        with store._lock:
            row_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
        assert row_count == 1, "orphan chunk should be GC'd after delete_session"

    def test_neighbors_use_per_session_seq_not_chunk_id(self, tmp_path):
        """Two sessions with different content interleaved in chunks.id —
        neighbours of a B-chunk must come from B's own seq, never from A.

        Without per-session seq, neighbours-by-id would pull A chunks
        whose autoincrement id happens to land next to a B chunk's id.
        """
        store = self._make_store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        # Interleave A and B inserts so their chunk_ids alternate.
        for i in range(3):
            store.add("A", [{"role": "user", "content": f"a-{i}"}],
                      emb.embed([f"a-{i}"]), "lex")
            store.add("B", [{"role": "user", "content": f"b-{i}"}],
                      emb.embed([f"b-{i}"]), "lex")

        # Find B's middle chunk (b-1).
        results = store.search("B", emb.embed(["b-1"])[0], k=1)
        assert results, "expected b-1 to be found in session B"
        b_mid_id = results[0]["id"]
        # Pull a wide neighbour window — every returned chunk must
        # belong to B (no A leakage via id-adjacency).
        neighbours = store.neighbors("B", b_mid_id, before=5, after=5)
        assert neighbours
        for n in neighbours:
            assert "a-" not in n["preview"], (
                f"session A leaked into B's neighbours: {n['preview']!r}"
            )


# ---------------------------------------------------------------------------
# Schema migration — pre-dedup DBs must boot without losing data
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    """Boot a ChunkStore against a hand-crafted pre-dedup DB and check
    that it backfills ``content_hash`` + ``chunk_sessions`` so existing
    sessions keep working after the user upgrades."""

    def _build_legacy_db(self, db_path: Path) -> None:
        """Create a DB with the old (no-dedup) schema and a few rows."""
        import sqlite3
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE chunks (
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
        """)
        # Insert a few legacy rows with deterministic embeddings so the
        # migration's content_hash computation is the only step under
        # test (we don't exercise the embedder here).
        legacy_rows = [
            ("legacy-S1", "user", "first legacy turn", 1.0),
            ("legacy-S1", "assistant", "legacy reply", 2.0),
            ("legacy-S2", "user", "another session start", 3.0),
        ]
        for sid, role, content, ts in legacy_rows:
            conn.execute(
                "INSERT INTO chunks "
                "(session_id, role, content, preview, embedding, embedder, "
                " dim, chunk_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid, role, content, content[:50],
                    np.zeros(64, dtype=np.float32).tobytes(),
                    "lex", 64, "user_text" if role == "user" else "assistant_decision",
                    ts,
                ),
            )
        conn.commit()
        conn.close()

    def test_migration_adds_column_and_backfills_attachments(self, tmp_path):
        db_path = tmp_path / "lcm" / "store.db"
        self._build_legacy_db(db_path)

        # Open under the new schema — migration should run automatically.
        store = ChunkStore(db_path)

        with store._lock:
            cols = {
                row[1]
                for row in store._conn.execute("PRAGMA table_info(chunks)")
            }
            assert "content_hash" in cols, "ALTER TABLE should add content_hash"

            # Every legacy row got a chunk_sessions attachment.
            attach_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunk_sessions"
            ).fetchone()[0]
            chunk_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
            assert attach_count == chunk_count == 3

            # content_hash backfilled for every row.
            null_hashes = store._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE content_hash IS NULL"
            ).fetchone()[0]
            assert null_hashes == 0

            # Hashes are correct (computable from role + chunk_type + content).
            sample = store._conn.execute(
                "SELECT role, chunk_type, content, content_hash FROM chunks"
            ).fetchall()
            for role, ctype, content, h in sample:
                assert h == _chunk_content_hash(role, ctype or "message", content)

        # Reading via the public API still works.
        assert store.session_chunk_count("legacy-S1") == 2
        assert store.session_chunk_count("legacy-S2") == 1

    def test_migration_is_idempotent(self, tmp_path):
        """Running migration twice must not duplicate chunk_sessions rows."""
        db_path = tmp_path / "lcm" / "store.db"
        self._build_legacy_db(db_path)
        ChunkStore(db_path).close()
        store = ChunkStore(db_path)  # second open → migration runs again

        with store._lock:
            attach_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunk_sessions"
            ).fetchone()[0]
        assert attach_count == 3, (
            f"expected 3 attachments after re-migration, got {attach_count}"
        )

    def test_migration_dedups_subsequent_inserts_against_legacy(self, tmp_path):
        """After migration, fresh add() with content matching a legacy row
        should reuse the existing chunk row instead of re-embedding."""
        db_path = tmp_path / "lcm" / "store.db"
        self._build_legacy_db(db_path)
        store = ChunkStore(db_path)

        emb = LexicalEmbedder(dim=64)
        # Same content as legacy row 1 ("first legacy turn") under role=user.
        ids = store.add(
            "fresh-session",
            [{"role": "user", "chunk_type": "user_text",
              "content": "first legacy turn"}],
            emb.embed(["first legacy turn"]),
            "lex",
        )
        assert ids == [1], (
            f"expected dedup to reuse legacy chunk id 1, got {ids}"
        )
        # Total rows in chunks unchanged — no new row was created.
        with store._lock:
            row_count = store._conn.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0]
        assert row_count == 3
        # But the new session is now ATTACHED to the legacy chunk.
        assert store.session_chunk_count("fresh-session") == 1


# ---------------------------------------------------------------------------
# ChunkStore.last_add_stats — dedup vs new accounting (Scheme A)
# ---------------------------------------------------------------------------


class TestChunkStoreAddStats:
    """Verify ``ChunkStore.add()`` exposes accurate per-call dedup stats.

    Why this matters: ``run_agent.py`` used to mis-report a 0-delta in
    ``session_chunk_count`` as an embedder failure (false alarm).  The
    fix surfaces ``store.last_add_stats`` so the engine — and ultimately
    the user-facing ``+N indexed`` line — can tell apart "all hits were
    dedup" from "embedder actually failed".
    """

    def _store(self, tmp_path: Path) -> ChunkStore:
        return ChunkStore(tmp_path / "lcm" / "store.db")

    def test_first_time_insert_records_new_count(self, tmp_path):
        store = self._store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        chunks = [
            {"role": "user", "content": "alpha"},
            {"role": "assistant", "content": "beta"},
        ]
        store.add("S", chunks, emb.embed(["alpha", "beta"]), "lex")

        assert store.last_add_stats == {
            "new": 2, "reused": 0, "already_attached": 0, "input_chunks": 2,
        }

    def test_same_session_re_add_records_already_attached(self, tmp_path):
        """When the same session adds the same content twice, the second
        call should report ``already_attached`` (INSERT OR IGNORE no-ops)
        — this is the *exact* path that caused the false-alarm warning
        in production: a re-run of compression on overlapping messages."""
        store = self._store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        chunks = [{"role": "user", "content": "same content"}]

        store.add("S", chunks, emb.embed(["same content"]), "lex")
        assert store.last_add_stats["new"] == 1

        store.add("S", chunks, emb.embed(["same content"]), "lex")
        assert store.last_add_stats == {
            "new": 0, "reused": 0, "already_attached": 1, "input_chunks": 1,
        }

    def test_cross_session_dedup_records_reused(self, tmp_path):
        """Same content under a different session_id should reuse the
        existing chunk row (cross-session dedup) and report it as
        ``reused`` — neither a "new" nor a "real failure"."""
        store = self._store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        chunks = [{"role": "user", "content": "shared content"}]

        store.add("session-A", chunks, emb.embed(["shared content"]), "lex")
        store.add("session-B", chunks, emb.embed(["shared content"]), "lex")

        assert store.last_add_stats == {
            "new": 0, "reused": 1, "already_attached": 0, "input_chunks": 1,
        }

    def test_empty_input_clears_stats_to_zero(self, tmp_path):
        """Empty add() must still populate stats so the engine never
        sees a stale ``last_add_stats`` from a previous call."""
        store = self._store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        store.add("S", [{"role": "user", "content": "x"}], emb.embed(["x"]), "lex")
        assert store.last_add_stats["new"] == 1

        store.add("S", [], np.zeros((0, 64), dtype=np.float32), "lex")
        assert store.last_add_stats == {
            "new": 0, "reused": 0, "already_attached": 0, "input_chunks": 0,
        }

    def test_mixed_input_breaks_down_correctly(self, tmp_path):
        """A single add() with a mix of (truly new + dedup-hit) content
        must report each bucket independently — not collapse them."""
        store = self._store(tmp_path)
        emb = LexicalEmbedder(dim=64)
        # Pre-seed: chunk that will be dedup-hit on the second batch.
        store.add(
            "S", [{"role": "user", "content": "preexisting"}],
            emb.embed(["preexisting"]), "lex",
        )

        # Second batch: 1 already-attached + 2 truly new.
        chunks = [
            {"role": "user", "content": "preexisting"},  # dedup hit
            {"role": "assistant", "content": "fresh-1"},
            {"role": "user", "content": "fresh-2"},
        ]
        store.add(
            "S", chunks,
            emb.embed(["preexisting", "fresh-1", "fresh-2"]), "lex",
        )

        assert store.last_add_stats == {
            "new": 2, "reused": 0, "already_attached": 1, "input_chunks": 3,
        }


# ---------------------------------------------------------------------------
# LCMEngine._last_compress_status — surface-level contract (Scheme A)
# ---------------------------------------------------------------------------


class TestLCMEngineCompressStatus:
    """The engine must expose an unambiguous status for the most-recent
    compression so ``run_agent.py`` can distinguish *real* failure from
    *dedup-hit* / *nothing-to-index* — the previous logic mis-reported
    all 0-delta cases as ``⚠ embedder likely failed``.
    """

    def _bulk_msgs(self, tag: str, n: int = 25):
        msgs = [{"role": "system", "content": "sys"}]
        long_chunk = "x " * 200
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"{tag}-{i} {long_chunk}"})
        return msgs

    def test_status_ok_with_new_count_on_first_compress(self, tmp_path):
        eng = _make_engine(tmp_path)
        eng.compress(self._bulk_msgs("first"), current_tokens=10000)
        st = eng._last_compress_status
        assert st is not None
        assert st["status"] == "ok"
        assert st["error"] is None
        assert st["new"] > 0
        assert st["reused"] == 0
        assert st["already_attached"] == 0

    def test_status_ok_with_dedup_on_second_compress_same_messages(
        self, tmp_path
    ):
        """The bug we are fixing: identical messages compressed twice.
        First run = ``new>0``; second run = ``new==0`` and
        ``already_attached>0``.  Crucially, status must still be ``ok``
        — NOT a failure."""
        eng = _make_engine(tmp_path)
        msgs = self._bulk_msgs("dup")
        eng.compress(msgs, current_tokens=10000)
        first_new = eng._last_compress_status["new"]
        assert first_new > 0

        eng.compress(msgs, current_tokens=10000)
        st = eng._last_compress_status
        assert st["status"] == "ok"
        assert st["error"] is None
        assert st["new"] == 0
        assert st["already_attached"] >= first_new

    def test_status_init_failed_when_ensure_embedder_raises(
        self, tmp_path, monkeypatch
    ):
        eng = _make_engine(tmp_path)
        eng._embedder = None
        eng._store = None
        monkeypatch.setattr(
            eng, "_ensure_embedder",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        eng.compress(self._bulk_msgs("init"), current_tokens=10000)
        st = eng._last_compress_status
        assert st["status"] == "init_failed"
        assert "boom" in (st["error"] or "")
        assert st["new"] == 0

    def test_status_embed_failed_when_embedder_embed_raises(
        self, tmp_path, monkeypatch
    ):
        eng = _make_engine(tmp_path)
        eng._ensure_store()
        embedder = eng._ensure_embedder()
        monkeypatch.setattr(
            embedder, "embed",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("embed-go-boom")),
        )
        eng.compress(self._bulk_msgs("embed"), current_tokens=10000)
        st = eng._last_compress_status
        assert st["status"] == "embed_failed"
        assert "embed-go-boom" in (st["error"] or "")
        assert st["new"] == 0

    def test_status_store_add_failed_when_store_add_raises(
        self, tmp_path, monkeypatch
    ):
        eng = _make_engine(tmp_path)
        store = eng._ensure_store()
        eng._ensure_embedder()
        monkeypatch.setattr(
            store, "add",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("store-broken")),
        )
        eng.compress(self._bulk_msgs("storefail"), current_tokens=10000)
        st = eng._last_compress_status
        assert st["status"] == "store_add_failed"
        assert "store-broken" in (st["error"] or "")
        assert st["new"] == 0

    def test_status_nothing_to_index_when_middle_redacts_to_empty(
        self, tmp_path, monkeypatch
    ):
        """If every middle chunk's content is redacted to empty text,
        ``chunk_records`` ends up empty and we should report
        ``nothing_to_index`` — not a failure."""
        eng = _make_engine(tmp_path)
        # Force the redactor to return empty strings for everything.
        monkeypatch.setattr(
            "plugins.context_engine.lcm.engine.redact_sensitive_text",
            lambda text, **kw: "",
        )
        eng.compress(self._bulk_msgs("redact"), current_tokens=10000)
        st = eng._last_compress_status
        assert st["status"] == "nothing_to_index"
        assert st["error"] is None
        assert st["new"] == 0

    def test_get_status_exposes_lcm_last_compress(self, tmp_path):
        """End-to-end contract: the fields the UI reads are present."""
        eng = _make_engine(tmp_path)
        eng.compress(self._bulk_msgs("status"), current_tokens=10000)
        public = eng.get_status()
        assert "lcm_last_compress" in public
        snap = public["lcm_last_compress"]
        for key in ("status", "error", "new", "reused",
                    "already_attached", "input_chunks"):
            assert key in snap, f"missing '{key}' in lcm_last_compress"
        assert snap["status"] == "ok"

    def test_get_status_omits_lcm_last_compress_before_first_compress(
        self, tmp_path
    ):
        """A fresh engine that never compressed should not advertise a
        stale ``lcm_last_compress`` — the field is opt-in once we have
        something to report."""
        eng = _make_engine(tmp_path)
        public = eng.get_status()
        assert "lcm_last_compress" not in public
