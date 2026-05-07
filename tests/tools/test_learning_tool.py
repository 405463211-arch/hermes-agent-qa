"""Tests for tools/learning_tool.py — schema, handlers, auto-promote chain."""

from __future__ import annotations

import json

import pytest

from tools import learning_tool as lt
from agent.learning_store import LearningStore


# ---------------------------------------------------------------------------
# Fixture: isolate the module-level singleton store per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Replace the module singleton with a tmp-path store for each test."""
    lt._reset_store_for_tests()
    fresh = LearningStore(db_path=tmp_path / "ls.db")
    monkeypatch.setattr(lt, "_GLOBAL_STORE", fresh)
    yield fresh
    fresh.close()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_record_schema_required_fields(self):
        s = lt.LEARNING_RECORD_SCHEMA
        assert s["name"] == "learning_record"
        required = s["parameters"]["required"]
        assert set(required) == {"category", "pattern_key", "summary"}

    def test_record_schema_category_enum(self):
        s = lt.LEARNING_RECORD_SCHEMA
        cats = s["parameters"]["properties"]["category"]["enum"]
        assert set(cats) == {"learning", "error", "feature_request"}

    def test_record_schema_description_mentions_promotion_threshold(self):
        # The model needs to see "≥3" so it understands the dedupe contract.
        d = lt.LEARNING_RECORD_SCHEMA["description"].lower()
        assert "pattern_key" in d
        # Don't pin the literal number — but the description should explain
        # there IS a recurrence threshold for auto-promotion.
        assert "auto-promot" in d or "auto promot" in d

    def test_list_schema_status_enum(self):
        statuses = lt.LEARNING_LIST_SCHEMA["parameters"]["properties"]["status"]["enum"]
        assert "pending" in statuses
        assert "all" in statuses

    def test_resolve_schema_requires_id(self):
        s = lt.LEARNING_RESOLVE_SCHEMA
        assert s["parameters"]["required"] == ["learning_id"]


# ---------------------------------------------------------------------------
# learning_record handler — basic
# ---------------------------------------------------------------------------


class TestLearningRecordHandler:
    def test_records_first_occurrence(self):
        out = json.loads(lt.learning_record_handler({
            "category": "learning",
            "pattern_key": "agent.scope.unconfirmed",
            "summary": "Confirm scope before bulk edit",
        }))
        assert out["success"] is True
        assert out["recurrence_count"] == 1
        assert out["status"] == "pending"
        assert out["eligible_for_promotion"] is False
        assert out["id"].startswith("LRN-")

    def test_validates_required_fields(self):
        for missing in ("category", "pattern_key", "summary"):
            args = {
                "category": "learning",
                "pattern_key": "k",
                "summary": "s",
            }
            del args[missing]
            out = json.loads(lt.learning_record_handler(args))
            assert out["success"] is False
            assert missing in out["error"]

    def test_invalid_category_returns_error(self):
        out = json.loads(lt.learning_record_handler({
            "category": "nonsense",
            "pattern_key": "k",
            "summary": "s",
        }))
        assert out["success"] is False

    def test_dedupe_increments_recurrence(self):
        for _ in range(3):
            lt.learning_record_handler({
                "category": "learning",
                "pattern_key": "k",
                "summary": "s",
            })
        out = json.loads(lt.learning_record_handler({
            "category": "learning",
            "pattern_key": "k",
            "summary": "s",
        }))
        assert out["recurrence_count"] == 4

    def test_task_id_threading(self):
        # Pass task_id via kwarg the way handle_function_call would.
        out1 = json.loads(lt.learning_record_handler(
            {"category": "learning", "pattern_key": "k", "summary": "s"},
            task_id="task-A",
        ))
        out2 = json.loads(lt.learning_record_handler(
            {"category": "learning", "pattern_key": "k", "summary": "s"},
            task_id="task-B",
        ))
        assert out2["distinct_tasks"] == 2


# ---------------------------------------------------------------------------
# Auto-promote chain
# ---------------------------------------------------------------------------


class _FakeMemoryStore:
    """Minimal stub matching MemoryStore.add_rule_with_lifecycle."""

    def __init__(self, succeed: bool = True):
        self.calls = []
        self.succeed = succeed

    def add_rule_with_lifecycle(self, *, text, pinned, source, recurrence, pattern_key):
        self.calls.append({
            "text": text,
            "pinned": pinned,
            "source": source,
            "recurrence": recurrence,
            "pattern_key": pattern_key,
        })
        if self.succeed:
            return {"success": True, "message": "ok"}
        return {"success": False, "error": "RULES.md full"}


class TestAutoPromote:
    def test_eligible_entry_promotes_to_rules(self, monkeypatch):
        memstore = _FakeMemoryStore(succeed=True)
        # 3 distinct tasks → eligibility threshold met on the third call.
        for tid in ("t1", "t2"):
            lt.learning_record_handler(
                {
                    "category": "learning",
                    "pattern_key": "agent.x",
                    "summary": "summary line",
                    "suggested_action": "do not X",
                },
                store=memstore,
                task_id=tid,
            )
        out = json.loads(lt.learning_record_handler(
            {
                "category": "learning",
                "pattern_key": "agent.x",
                "summary": "summary line",
                "suggested_action": "do not X",
            },
            store=memstore,
            task_id="t3",
        ))
        assert out["auto_promoted"] is True
        assert out["promoted_to"] == "rules"
        assert out["rule_text"] == "do not X"
        # Memory store was called with the right metadata.
        assert len(memstore.calls) == 1
        call = memstore.calls[0]
        assert call["source"].startswith("LRN-")
        assert call["recurrence"] == 3
        assert call["pattern_key"] == "agent.x"

    def test_promotion_falls_back_to_summary_without_suggested_action(self):
        memstore = _FakeMemoryStore(succeed=True)
        for tid in ("t1", "t2", "t3"):
            out = json.loads(lt.learning_record_handler(
                {
                    "category": "learning",
                    "pattern_key": "no.action.given",
                    "summary": "fallback to summary",
                },
                store=memstore,
                task_id=tid,
            ))
        assert out["auto_promoted"] is True
        assert memstore.calls[0]["text"] == "fallback to summary"

    def test_promotion_blocked_when_memory_store_returns_failure(self):
        memstore = _FakeMemoryStore(succeed=False)
        for tid in ("t1", "t2", "t3"):
            out = json.loads(lt.learning_record_handler(
                {
                    "category": "learning",
                    "pattern_key": "x",
                    "summary": "s",
                    "suggested_action": "a",
                },
                store=memstore,
                task_id=tid,
            ))
        # Promotion attempted but did not succeed → no auto_promoted flag.
        assert out.get("auto_promoted") is not True

    def test_promotion_skipped_when_no_memory_store(self):
        for tid in ("t1", "t2", "t3"):
            out = json.loads(lt.learning_record_handler(
                {
                    "category": "learning",
                    "pattern_key": "x",
                    "summary": "s",
                },
                task_id=tid,
            ))
        # No store kwarg supplied → eligible but not promoted.
        assert out["eligible_for_promotion"] is True
        assert out.get("auto_promoted") is not True


# ---------------------------------------------------------------------------
# learning_list / learning_resolve
# ---------------------------------------------------------------------------


class TestListResolve:
    def test_list_returns_compact_form(self):
        lt.learning_record_handler({
            "category": "learning", "pattern_key": "a", "summary": "s",
        })
        out = json.loads(lt.learning_list_handler({}))
        assert out["success"] is True
        assert out["count"] == 1
        keys = set(out["entries"][0].keys())
        # Compact projection — no 'details', 'first_seen', etc. that would
        # blow out the model's context.
        assert "details" not in keys
        assert "first_seen" not in keys
        assert {"id", "summary", "pattern_key", "status"} <= keys

    def test_list_filters_by_status(self):
        a = json.loads(lt.learning_record_handler(
            {"category": "learning", "pattern_key": "a", "summary": "s"}
        ))
        b = json.loads(lt.learning_record_handler(
            {"category": "learning", "pattern_key": "b", "summary": "s"}
        ))
        lt.learning_resolve_handler({"learning_id": b["id"]})

        pending = json.loads(lt.learning_list_handler({"status": "pending"}))
        resolved = json.loads(lt.learning_list_handler({"status": "resolved"}))
        assert pending["count"] == 1
        assert resolved["count"] == 1

    def test_resolve_requires_id(self):
        out = json.loads(lt.learning_resolve_handler({}))
        assert out["success"] is False

    def test_resolve_persists_status(self):
        rec = json.loads(lt.learning_record_handler(
            {"category": "learning", "pattern_key": "k", "summary": "s"}
        ))
        out = json.loads(lt.learning_resolve_handler(
            {"learning_id": rec["id"], "notes": "fixed"}
        ))
        assert out["success"] is True
