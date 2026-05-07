"""M2 white-box tool-layer probe.

Covers:
- learning_record / learning_list / learning_resolve handler input validation
- learning_record JSON shape (success / error)
- auto-promote chain end-to-end (learning_record → MemoryStore.add_rule_with_lifecycle)
- project_knowledge_search / view / save handler input + path traversal safety
- project_knowledge_promote requires store; correct source encoding
- memory_tool dispatch: invalid action / invalid target / required args
- memory_tool three-bucket add+remove cycle
- **Repo-wide cross-tool reference scan** (broader than M0 — catches all
  tool descriptions, not just the new ones)
"""
from __future__ import annotations

import json
from datetime import date

import pytest


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ learning_tool — handler input validation + JSON contract                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture
def isolated_learning_store(tmp_path, monkeypatch):
    """Force the learning_tool to use a fresh store under tmp_path.

    Without this, learning_record would write to ~/.hermes/learning_store.db
    (the autouse HERMES_HOME redirect already protects us, but pinning the
    db path makes assertions deterministic).
    """
    from agent.learning_store import LearningStore
    import tools.learning_tool as lt

    fresh_store = LearningStore(db_path=tmp_path / "lt.db")
    monkeypatch.setattr(lt, "_GLOBAL_STORE", fresh_store)
    yield fresh_store
    fresh_store.close()


class TestLearningRecordHandler:
    def _call(self, **args):
        from tools.learning_tool import learning_record_handler
        return json.loads(learning_record_handler(args))

    def test_success_minimal(self, isolated_learning_store):
        r = self._call(
            category="learning",
            pattern_key="agent.scope.foo",
            summary="confirm before bulk edits",
        )
        assert r["success"] is True
        assert r["id"].startswith("LRN-")
        assert r["recurrence_count"] == 1

    def test_missing_category(self, isolated_learning_store):
        r = self._call(pattern_key="x", summary="y")
        assert r["success"] is False
        assert "category" in r["error"].lower()

    def test_missing_pattern_key(self, isolated_learning_store):
        r = self._call(category="error", summary="y")
        assert r["success"] is False
        assert "pattern_key" in r["error"].lower()

    def test_missing_summary(self, isolated_learning_store):
        r = self._call(category="error", pattern_key="x")
        assert r["success"] is False
        assert "summary" in r["error"].lower()

    def test_invalid_category_returns_error_not_crash(
        self, isolated_learning_store
    ):
        r = self._call(category="bogus", pattern_key="x", summary="y")
        assert r["success"] is False
        assert "category" in r["error"].lower()

    def test_dedupe_increments_recurrence(self, isolated_learning_store):
        """Recording the same pattern twice should bump recurrence_count."""
        r1 = self._call(
            category="error", pattern_key="dup.k", summary="hit 1",
        )
        r2 = self._call(
            category="error", pattern_key="dup.k", summary="hit 2",
        )
        assert r2["id"] == r1["id"]
        assert r2["recurrence_count"] == 2

    def test_returns_eligible_flag(self, isolated_learning_store):
        r = self._call(category="learning", pattern_key="elig.k", summary="x")
        assert "eligible_for_promotion" in r
        assert isinstance(r["eligible_for_promotion"], bool)


class TestAutoPromoteChain:
    """End-to-end: when an entry becomes eligible AND a MemoryStore is
    threaded through kwargs, learning_record should auto-promote it to RULES.md.
    """

    def test_third_recurrence_across_two_tasks_promotes(
        self, tmp_path, isolated_learning_store, monkeypatch
    ):
        # Stand up a real MemoryStore with isolated dirs.
        import tools.memory_tool as mt
        monkeypatch.setattr(mt, "get_memory_dir", lambda: tmp_path / "mem")
        store = mt.MemoryStore(rules_char_limit=10_000)
        store.load_from_disk()

        from tools.learning_tool import learning_record_handler

        # Three hits across two distinct tasks — meets default thresholds
        # (3 recurrences, 2 distinct tasks within 30 days).
        for task_id in ("t1", "t2", "t1"):
            r = json.loads(learning_record_handler(
                {
                    "category": "error",
                    "pattern_key": "auto.promo.k",
                    "summary": "always confirm scope",
                    "suggested_action": "Always confirm scope before bulk edits.",
                },
                task_id=task_id,
                store=store,
            ))
        # Final call should report promotion
        assert r.get("auto_promoted") is True, f"final response: {r}"
        assert r["promoted_to"] == "rules"

        # And the rule must be in MemoryStore.rules_entries
        assert any("Always confirm scope" in s for s in store.rules_entries)

    def test_no_promotion_without_memory_store(
        self, isolated_learning_store
    ):
        """Without a store kwarg, the entry stays pending even when eligible."""
        from tools.learning_tool import learning_record_handler

        for task_id in ("t1", "t2", "t1"):
            r = json.loads(learning_record_handler(
                {
                    "category": "error",
                    "pattern_key": "no.store.k",
                    "summary": "x",
                    "suggested_action": "x",
                },
                task_id=task_id,
                # NOTE: no store kwarg
            ))

        assert r.get("auto_promoted") is not True
        assert r["eligible_for_promotion"] is True

    def test_no_promotion_when_no_rule_text(
        self, tmp_path, isolated_learning_store, monkeypatch
    ):
        """Promotion requires a non-empty rule body (suggested_action OR summary)."""
        import tools.memory_tool as mt
        monkeypatch.setattr(mt, "get_memory_dir", lambda: tmp_path / "mem")
        store = mt.MemoryStore(rules_char_limit=10_000)
        store.load_from_disk()

        from tools.learning_tool import learning_record_handler

        # Use single-character summary so eligibility hits but rule body is short
        # (still gets promoted because summary is non-empty fallback)
        for task_id in ("t1", "t2", "t1"):
            r = json.loads(learning_record_handler(
                {
                    "category": "error",
                    "pattern_key": "x.shortrule",
                    "summary": "x",
                    # No suggested_action → falls back to summary
                },
                task_id=task_id,
                store=store,
            ))
        # 'x' is non-empty, so promotion happens; rule_text == "x"
        assert r.get("auto_promoted") is True
        assert r["rule_text"] == "x"


class TestLearningListHandler:
    def test_returns_compact_form(self, isolated_learning_store):
        from tools.learning_tool import (
            learning_list_handler,
            learning_record_handler,
        )
        learning_record_handler({
            "category": "learning", "pattern_key": "lst.k", "summary": "z",
        })
        r = json.loads(learning_list_handler({}))
        assert r["success"] is True
        assert r["count"] == 1
        # Must NOT leak full row fields (details, related_files_json, etc)
        keys = set(r["entries"][0].keys())
        forbidden = {"details", "related_files_json", "first_seen", "last_seen"}
        assert not (keys & forbidden), (
            f"learning_list leaked verbose fields: {keys & forbidden}"
        )


class TestLearningResolveHandler:
    def test_resolve_success(self, isolated_learning_store):
        from tools.learning_tool import (
            learning_record_handler,
            learning_resolve_handler,
        )
        rec = json.loads(learning_record_handler({
            "category": "learning", "pattern_key": "res.k", "summary": "z",
        }))
        out = json.loads(learning_resolve_handler({"learning_id": rec["id"]}))
        assert out.get("success") is True

    def test_missing_id(self, isolated_learning_store):
        from tools.learning_tool import learning_resolve_handler
        out = json.loads(learning_resolve_handler({}))
        assert out["success"] is False
        assert "learning_id" in out["error"].lower()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ project_knowledge_tool — handlers + path safety                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture
def isolated_pk(tmp_path, monkeypatch):
    """Pin project_knowledge to a tmp dir + fixed project name.

    NOTE: ``tools/project_knowledge_tool.py`` does ``from agent.project_knowledge
    import detect_project_name, get_project_dir`` at module load — so we MUST
    monkeypatch the bound names on the consumer module, not the producer module.
    """
    import agent.project_knowledge as pk
    import tools.project_knowledge_tool as pkt

    pk_root = tmp_path / "pk"

    def _detect():
        return "test-proj"

    def _get_dir(name):
        return pk_root / name

    monkeypatch.setattr(pk, "detect_project_name", _detect)
    monkeypatch.setattr(pk, "get_project_dir", _get_dir)
    monkeypatch.setattr(pkt, "detect_project_name", _detect)
    monkeypatch.setattr(pkt, "get_project_dir", _get_dir)
    return pk_root / "test-proj"


class TestProjectKnowledgeSave:
    def test_save_creates_file(self, isolated_pk):
        from tools.project_knowledge_tool import project_knowledge_save
        r = json.loads(project_knowledge_save("notes/x.md", "hello"))
        assert r["success"] is True
        assert (isolated_pk / "notes" / "x.md").read_text() == "hello"

    def test_save_rejects_traversal(self, isolated_pk):
        from tools.project_knowledge_tool import project_knowledge_save
        r = json.loads(project_knowledge_save("../escape.md", "bad"))
        assert r["success"] is False

    def test_save_rejects_absolute_path(self, isolated_pk):
        from tools.project_knowledge_tool import project_knowledge_save
        r = json.loads(project_knowledge_save("/etc/passwd", "bad"))
        assert r["success"] is False

    def test_save_rejects_invalid_mode(self, isolated_pk):
        from tools.project_knowledge_tool import project_knowledge_save
        r = json.loads(project_knowledge_save("a.md", "x", mode="overwrite"))
        assert r["success"] is False

    def test_save_append_mode(self, isolated_pk):
        from tools.project_knowledge_tool import project_knowledge_save
        json.loads(project_knowledge_save("a.md", "first\n", mode="write"))
        json.loads(project_knowledge_save("a.md", "second\n", mode="append"))
        assert (isolated_pk / "a.md").read_text() == "first\nsecond\n"


class TestProjectKnowledgeView:
    def test_view_paging(self, isolated_pk):
        from tools.project_knowledge_tool import (
            project_knowledge_save,
            project_knowledge_view,
        )
        json.loads(project_knowledge_save(
            "big.md", "\n".join(f"line {i}" for i in range(100))
        ))
        r = json.loads(project_knowledge_view("big.md", offset=10, limit=5))
        assert r["lines_returned"] == 5
        assert r["total_lines"] == 100
        assert r["more_available"] is True

    def test_view_rejects_traversal(self, isolated_pk):
        from tools.project_knowledge_tool import (
            project_knowledge_save,
            project_knowledge_view,
        )
        # First create a file inside so the dir exists
        json.loads(project_knowledge_save("ok.md", "x"))
        r = json.loads(project_knowledge_view("../etc/passwd"))
        assert r["success"] is False

    def test_view_missing_file(self, isolated_pk):
        from tools.project_knowledge_tool import (
            project_knowledge_save,
            project_knowledge_view,
        )
        json.loads(project_knowledge_save("init.md", "x"))  # creates dir
        r = json.loads(project_knowledge_view("nonexistent.md"))
        assert r["success"] is False


class TestProjectKnowledgeSearch:
    def test_search_returns_hits(self, isolated_pk):
        from tools.project_knowledge_tool import (
            project_knowledge_save,
            project_knowledge_search,
        )
        json.loads(project_knowledge_save("a.md", "hello world\nfoo bar\n"))
        r = json.loads(project_knowledge_search("hello"))
        assert r["success"] is True
        assert r["hit_count"] >= 1
        assert any("hello" in h.get("preview", "") for h in r["hits"])

    def test_search_empty_query_rejected(self, isolated_pk):
        from tools.project_knowledge_tool import project_knowledge_search
        r = json.loads(project_knowledge_search(""))
        assert r["success"] is False

    def test_search_missing_dir_returns_friendly(self, tmp_path, monkeypatch):
        import agent.project_knowledge as pk
        import tools.project_knowledge_tool as pkt
        monkeypatch.setattr(pk, "detect_project_name", lambda: "missing-proj")
        monkeypatch.setattr(
            pk, "get_project_dir", lambda name: tmp_path / "missing"
        )
        monkeypatch.setattr(pkt, "detect_project_name", lambda: "missing-proj")
        monkeypatch.setattr(
            pkt, "get_project_dir", lambda name: tmp_path / "missing"
        )
        from tools.project_knowledge_tool import project_knowledge_search
        r = json.loads(project_knowledge_search("foo"))
        assert r["success"] is True
        assert r["exists"] is False
        assert r["hits"] == []


class TestProjectKnowledgePromote:
    def test_promote_requires_store(self):
        from tools.project_knowledge_tool import project_knowledge_promote
        r = json.loads(project_knowledge_promote(
            rule_text="something", source_relpath="x.md"
        ))
        # tool_error() returns {"error": "..."} without a success field
        assert "error" in r
        assert "store" in r["error"].lower() or "memorystore" in r["error"].lower()
        assert r.get("success", True) is False or "success" not in r

    def test_promote_requires_rule_text(self, tmp_path, monkeypatch):
        import tools.memory_tool as mt
        monkeypatch.setattr(mt, "get_memory_dir", lambda: tmp_path / "mem")
        store = mt.MemoryStore(rules_char_limit=10_000)
        store.load_from_disk()

        from tools.project_knowledge_tool import project_knowledge_promote
        r = json.loads(project_knowledge_promote(
            rule_text="", store=store
        ))
        assert "error" in r
        assert "rule_text" in r["error"].lower()

    def test_promote_writes_to_rules_with_pk_source(
        self, tmp_path, monkeypatch
    ):
        import tools.memory_tool as mt
        monkeypatch.setattr(mt, "get_memory_dir", lambda: tmp_path / "mem")
        store = mt.MemoryStore(rules_char_limit=10_000)
        store.load_from_disk()

        from tools.project_knowledge_tool import project_knowledge_promote
        r = json.loads(project_knowledge_promote(
            rule_text="Always lint before commit.",
            source_relpath="ci/lint.md",
            store=store,
        ))
        assert r["success"] is True
        assert r["promoted_to"] == "rules"
        assert r["source"].startswith("PK:")
        # rule must be in store.rules_entries with PK source meta
        joined = "\n".join(store.rules_entries)
        assert "Always lint before commit" in joined
        assert "PK:ci/lint.md" in joined


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ memory_tool dispatch — three buckets                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    import tools.memory_tool as mt
    monkeypatch.setattr(mt, "get_memory_dir", lambda: tmp_path / "mem")
    store = mt.MemoryStore(rules_char_limit=10_000, memory_char_limit=10_000,
                           user_char_limit=10_000)
    store.load_from_disk()
    return store


class TestMemoryToolDispatch:
    def test_invalid_action(self, isolated_memory):
        from tools.memory_tool import memory_tool
        r = json.loads(memory_tool("frobnicate", "memory", "x", store=isolated_memory))
        assert r["success"] is False
        assert "action" in r["error"].lower()

    def test_invalid_target(self, isolated_memory):
        from tools.memory_tool import memory_tool
        r = json.loads(memory_tool("add", "wrong_bucket", "x", store=isolated_memory))
        assert r["success"] is False
        assert "target" in r["error"].lower()

    def test_add_requires_content(self, isolated_memory):
        from tools.memory_tool import memory_tool
        r = json.loads(memory_tool("add", "memory", None, store=isolated_memory))
        assert r["success"] is False

    def test_no_store_returns_error(self):
        from tools.memory_tool import memory_tool
        r = json.loads(memory_tool("add", "memory", "x", store=None))
        assert r["success"] is False
        assert "memory" in r["error"].lower()

    def test_three_bucket_add_isolation(self, isolated_memory):
        """Adding to one bucket must not leak into others."""
        from tools.memory_tool import memory_tool
        for target in ("rules", "memory", "user"):
            json.loads(memory_tool(
                "add", target, f"text-for-{target}", store=isolated_memory
            ))
        # Each list contains only its own content
        assert any("text-for-rules" in s for s in isolated_memory.rules_entries)
        assert not any("text-for-rules" in s for s in isolated_memory.memory_entries)
        assert not any("text-for-rules" in s for s in isolated_memory.user_entries)
        assert any("text-for-memory" in s for s in isolated_memory.memory_entries)
        assert any("text-for-user" in s for s in isolated_memory.user_entries)

    def test_add_then_remove_roundtrip(self, isolated_memory):
        from tools.memory_tool import memory_tool
        json.loads(memory_tool("add", "memory", "rm-me", store=isolated_memory))
        assert any("rm-me" in s for s in isolated_memory.memory_entries)
        json.loads(memory_tool("remove", "memory", old_text="rm-me",
                               store=isolated_memory))
        assert not any("rm-me" in s for s in isolated_memory.memory_entries)


class TestAddRuleWithLifecycle:
    def test_manual_source_no_promoted_at(self, isolated_memory):
        result = isolated_memory.add_rule_with_lifecycle(
            text="manual rule", source="manual"
        )
        assert result["success"]
        from agent.rules_lifecycle import parse_rule_entry
        e = parse_rule_entry(isolated_memory.rules_entries[0])
        assert e.source == "manual"
        assert e.promoted_at is None

    def test_lrn_source_sets_promoted_at(self, isolated_memory):
        result = isolated_memory.add_rule_with_lifecycle(
            text="learned rule",
            source="LRN-20260501-ABC",
            recurrence=4,
            pattern_key="x.y.z",
        )
        assert result["success"]
        from agent.rules_lifecycle import parse_rule_entry
        e = parse_rule_entry(isolated_memory.rules_entries[0])
        assert e.source == "LRN-20260501-ABC"
        assert e.promoted_at is not None
        assert e.recurrence == 4
        assert e.pattern_key == "x.y.z"

    def test_pk_source_sets_promoted_at(self, isolated_memory):
        """PK: prefix should also be treated as 'from learning' for NEW marker."""
        result = isolated_memory.add_rule_with_lifecycle(
            text="pk rule", source="PK:notes.md"
        )
        assert result["success"]
        from agent.rules_lifecycle import parse_rule_entry
        e = parse_rule_entry(isolated_memory.rules_entries[0])
        assert e.source == "PK:notes.md"
        assert e.promoted_at is not None  # eligible for [NEW] marker

    def test_empty_text_rejected(self, isolated_memory):
        result = isolated_memory.add_rule_with_lifecycle(text="")
        assert result["success"] is False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Repo-wide cross-tool reference scan (broader than M0)                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestRepoWideCrossToolReferences:
    """Scan EVERY registered tool's description for hard-coded references
    to other tools (per AGENTS.md red line).

    There is a baseline of violations on ``main`` that pre-date this work
    (mostly in the 7 large legacy tools — terminal, delegate_task, browser,
    read_file, etc.). We freeze the baseline so:

      - Pre-existing violations don't block new work
      - But ANY new violation introduced by THIS branch fails the test

    If you legitimately need to add a violation, update ``BASELINE_*`` below
    and explain why in the PR description. If you're removing a violation,
    delete it from the baseline so we don't drift.
    """

    # Pre-existing violations on main as of 2026-05-01. These are NOT
    # caused by the rules-lifecycle / self-learning work. Reducing this
    # list is welcome — adding to it requires justification.
    BASELINE_BACKTICK: set = set()  # backtick form — none on main

    BASELINE_BAREWORD = {
        ("read_file", "use vision_analyze"),
        ("browser_cdp", "use web_extract"),
        ("browser_navigate", "prefer web_search"),
        ("delegate_task", "use execute_code"),
        ("delegate_task", "use clarify"),
        ("terminal", "use write_file"),
        ("terminal", "use read_file"),
        ("terminal", "use patch"),
        ("terminal", "use search_files"),
    }

    @staticmethod
    def _registered_tool_names():
        import model_tools  # noqa: F401  triggers discovery
        from tools.registry import registry
        return registry.get_all_tool_names()

    def _scan_backticks(self):
        import model_tools  # noqa: F401
        from tools.registry import registry
        names = set(self._registered_tool_names())
        found = set()
        for entry in registry._snapshot_entries():
            desc = entry.schema.get("description", "") or ""
            for other in names:
                if other == entry.name:
                    continue
                if registry.get_toolset_for_tool(other) == entry.toolset:
                    continue
                if f"`{other}`" in desc or f"``{other}``" in desc:
                    found.add((entry.name, other))
        return found

    def _scan_bareword(self):
        import re
        import model_tools  # noqa: F401
        from tools.registry import registry
        names = set(self._registered_tool_names())
        found = set()
        verbs = ("call", "use", "prefer", "invoke")
        for entry in registry._snapshot_entries():
            desc = (entry.schema.get("description", "") or "").lower()
            for other in names:
                if other == entry.name:
                    continue
                if registry.get_toolset_for_tool(other) == entry.toolset:
                    continue
                for verb in verbs:
                    pattern = re.compile(rf"\b{verb}\s+{re.escape(other)}\b")
                    if pattern.search(desc):
                        found.add((entry.name, f"{verb} {other}"))
        return found

    def test_no_new_backtick_references(self):
        found = self._scan_backticks()
        new_violations = found - self.BASELINE_BACKTICK
        assert not new_violations, (
            "NEW backtick cross-toolset references introduced by this branch:\n  "
            + "\n  ".join(f"{a} → ``{b}``" for a, b in sorted(new_violations))
            + "\nFix the description (use generic terms) or update BASELINE_BACKTICK."
        )

    def test_no_new_bareword_references(self):
        found = self._scan_bareword()
        new_violations = found - self.BASELINE_BAREWORD
        assert not new_violations, (
            "NEW bareword cross-toolset references introduced by this branch:\n  "
            + "\n  ".join(f"{a} → {b}" for a, b in sorted(new_violations))
            + "\nFix the description (use generic terms) or update BASELINE_BAREWORD."
        )

    def test_baseline_violations_still_present(self):
        """Sanity: if the baseline violations have been fixed, we should
        update the test to remove them rather than letting drift accumulate
        unnoticed."""
        found = self._scan_bareword()
        gone = self.BASELINE_BAREWORD - found
        assert not gone, (
            "These baseline violations are GONE — please remove them from "
            "BASELINE_BAREWORD so future regressions are caught:\n  "
            + "\n  ".join(f"{a} → {b}" for a, b in sorted(gone))
        )
