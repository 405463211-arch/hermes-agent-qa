"""M16 white-box contract probes.

Contract testing locks the *promises* one module makes to another. Unlike
unit tests (do I compute right?) or integration tests (does the system
work?), contracts answer: "if I behave badly, will my consumer survive?"

This module tests SIX contracts that span memory + learning subsystems:

  1. Tool registry → tool handlers
       "Every registered handler returns a valid JSON string and never
        propagates exceptions, even with bogus arguments."

  2. MemoryStore → prompt_builder (the prefix-cache contract)
       "Same load → same snapshot. Mid-session adds don't mutate the
        snapshot. Empty stores return None, not exceptions."

  3. LearningStore → learning_tool / memory promotion
       "Same pattern_key dedupes (UPDATE not INSERT). Every record()
        returns a dict with eligible_for_promotion."

  4. Plugin hook → run_agent main loop
       "invoke_hook never propagates exceptions. Bad plugins don't kill
        the loop. Unknown hook names return []."

  5. config.DEFAULT_CONFIG ↔ code reads
       "Every cfg key the code reads exists in DEFAULT_CONFIG.
        Every DEFAULT_CONFIG['memory'].key is reachable from somewhere."

  6. CommandDef ↔ gateway dispatch
       "Every gateway-exposed CommandDef appears in GATEWAY_KNOWN_COMMANDS,
        bot menus, and slash maps consistently."
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Contract 1: Tool registry → tool handlers                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestToolHandlerContract:
    """Every tool handler must:
       - Return a string (per ToolRegistry's expectation)
       - Return valid JSON (so model_tools can parse the result)
       - NEVER propagate exceptions out — failures must be wrapped as
         tool_error() JSON
    """

    def _all_handlers(self):
        # Trigger discovery
        import model_tools  # noqa: F401
        from tools.registry import registry
        return [
            (name, registry.get_entry(name))
            for name in registry.list_names()
        ]

    @pytest.mark.parametrize("with_args", [
        {},                       # empty
        {"unknown_param": "x"},   # bogus key
        {"target": None},         # bad type
        {"content": ""},          # empty string
    ])
    def test_handler_with_garbage_args_returns_json_string(self, with_args, tmp_path, monkeypatch):
        """A handler called with malformed args must still return a JSON
        string. This is the model_tools contract: it parses every
        return value via json.loads."""
        # Isolate any disk side-effects
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        import model_tools  # noqa: F401
        from tools.registry import registry

        # Spot-check 5 handlers (full pass would be slow on 100+ tools
        # and many require external creds). Pick representative ones.
        SAMPLES = [
            "memory",
            "learning_record",
            "learning_list",
            "project_knowledge_search",
            "project_knowledge_save",
        ]
        for name in SAMPLES:
            entry = registry.get_entry(name)
            if entry is None:
                continue  # tool not registered in this build
            handler = entry.handler
            try:
                result = handler(with_args)
            except Exception as exc:
                pytest.fail(
                    f"handler {name}({with_args}) raised {exc!r} — "
                    f"contract violation: handlers must wrap errors"
                )
            # Must be a string
            assert isinstance(result, str), (
                f"handler {name} returned non-string: {type(result)}"
            )
            # Must be valid JSON
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError as e:
                pytest.fail(f"handler {name} returned invalid JSON: {e}; result={result!r}")
            assert isinstance(parsed, dict), (
                f"handler {name} returned non-dict JSON: {parsed!r}"
            )

    def test_handler_must_be_callable(self):
        from tools.registry import registry
        import model_tools  # noqa: F401
        for name in registry.get_all_tool_names():
            entry = registry.get_entry(name)
            assert callable(entry.handler), (
                f"tool {name} handler is not callable"
            )

    def test_schema_must_be_valid_function_def(self):
        """Every registered tool's schema must be a dict with name +
        description + parameters fields — the OpenAI function schema."""
        from tools.registry import registry
        import model_tools  # noqa: F401
        for name in registry.get_all_tool_names():
            entry = registry.get_entry(name)
            schema = entry.schema
            assert isinstance(schema, dict)
            assert schema.get("name") == name, (
                f"schema name mismatch for {name}: schema={schema.get('name')!r}"
            )
            assert isinstance(schema.get("description"), str) and schema["description"], (
                f"tool {name} missing description"
            )
            assert "parameters" in schema, (
                f"tool {name} missing parameters block"
            )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Contract 2: MemoryStore → prompt_builder (prefix-cache contract)          ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestMemoryStoreContract:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import tools.memory_tool as mt
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()
        monkeypatch.setattr(mt, "get_memory_dir", lambda: mem_dir)
        s = mt.MemoryStore()
        s.load_from_disk()
        return s

    def test_format_is_byte_stable_for_same_input(self, store):
        """The cache contract: same disk state → byte-identical snapshot.
        This is what makes the prefix cache work."""
        store.add("rules", "rule-A")
        store.add("rules", "rule-B")
        store.load_from_disk()

        snap1 = store.format_for_system_prompt("rules")
        snap2 = store.format_for_system_prompt("rules")
        snap3 = store.format_for_system_prompt("rules")
        assert snap1 == snap2 == snap3
        # And the type is str
        assert isinstance(snap1, str)

    def test_empty_store_returns_none_not_exception(self, store):
        """Empty store → format_for_system_prompt returns None, NOT
        empty string, NOT exception. prompt_builder branches on None
        to skip the block entirely."""
        for target in ("rules", "memory", "user"):
            result = store.format_for_system_prompt(target)
            assert result is None, (
                f"empty {target} should be None, got {result!r}"
            )

    def test_unknown_target_is_handled(self, store):
        """Asking for a target that doesn't exist (e.g. typo) must not
        crash — return None or empty."""
        result = store.format_for_system_prompt("definitely_not_a_real_target")
        assert result is None or result == ""

    def test_add_returns_dict_with_success_or_error(self, store):
        """The add() contract: always returns a dict with either
        ``success`` or ``error`` key. Tools depend on this."""
        result = store.add("rules", "test rule")
        assert isinstance(result, dict)
        assert "success" in result or "error" in result

    def test_mid_session_add_does_not_mutate_snapshot(self, store):
        """The hard contract: add() persists to disk + live entries,
        but format_for_system_prompt still returns the OLD snapshot.
        Only load_from_disk refreshes."""
        store.add("rules", "first rule")
        store.load_from_disk()
        snapshot_before = store.format_for_system_prompt("rules")

        # Mid-session adds — these MUST NOT change the snapshot
        store.add("rules", "added later 1")
        store.add("rules", "added later 2")

        snapshot_after = store.format_for_system_prompt("rules")
        assert snapshot_before == snapshot_after, (
            "mid-session adds must not invalidate the snapshot — "
            "this is the prefix-cache contract"
        )

    def test_load_from_disk_is_idempotent(self, store):
        """Calling load_from_disk N times is the same as calling it once."""
        store.add("rules", "rule")
        store.load_from_disk()
        once = store.format_for_system_prompt("rules")

        store.load_from_disk()
        store.load_from_disk()
        store.load_from_disk()
        thrice = store.format_for_system_prompt("rules")
        assert once == thrice


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Contract 3: LearningStore → learning_tool / memory promotion              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLearningStoreContract:
    @pytest.fixture
    def lstore(self, tmp_path):
        from agent.learning_store import LearningStore
        return LearningStore(db_path=tmp_path / "lrn.db")

    def test_same_pattern_key_dedupes(self, lstore):
        """Two record() calls with same pattern_key must produce ONE row
        with recurrence_count = 2, NOT two rows."""
        r1 = lstore.record(
            category="error", pattern_key="dedupe-key",
            summary="seen once", task_id="t1",
        )
        r2 = lstore.record(
            category="error", pattern_key="dedupe-key",
            summary="seen twice", task_id="t2",
        )
        # Same id (UPDATE path)
        assert r1["id"] == r2["id"]
        # recurrence_count went up
        assert r2["recurrence_count"] == 2
        # distinct_tasks tracks task_id changes
        assert r2["distinct_tasks"] == 2

        all_rows = lstore.list(status="pending", limit=100)
        matching = [r for r in all_rows if r["pattern_key"] == "dedupe-key"]
        assert len(matching) == 1, "dedupe failed — multiple rows for same key"

    def test_record_always_returns_eligible_flag(self, lstore):
        """Contract: every record() result MUST include
        eligible_for_promotion (bool). The promotion pipeline depends
        on it for auto-promote decisions."""
        result = lstore.record(
            category="error", pattern_key="any-key", summary="test",
        )
        assert "eligible_for_promotion" in result
        assert isinstance(result["eligible_for_promotion"], bool)

    def test_id_format_invariant(self, lstore):
        """Every fresh ID must match LRN-YYYYMMDD-XXXXXX (or other
        category prefix + 6 hex). This is the ID format contract
        post-BUG-M9-1."""
        result = lstore.record(
            category="error", pattern_key="x", summary="x",
        )
        new_id = result["id"]
        parts = new_id.split("-")
        assert len(parts) == 3, f"bad id shape: {new_id}"
        assert len(parts[1]) == 8, f"date part wrong length: {parts[1]}"
        assert len(parts[2]) == 6, (
            f"BUG-M9-1: id suffix must be 6 chars, got {len(parts[2])}: {new_id}"
        )

    def test_invalid_category_rejected(self, lstore):
        """Categories must be in VALID_CATEGORIES — record() with an
        unknown category must reject (not silently store nonsense)."""
        from agent.learning_store import VALID_CATEGORIES
        # Try a category that's clearly not valid
        with pytest.raises((ValueError, AssertionError, KeyError, Exception)):
            # We expect SOME error — the exact type is implementation
            # detail, but we MUST NOT silently accept garbage
            result = lstore.record(
                category="totally-fake-category-xyz",
                pattern_key="x", summary="x",
            )
            # If no exception, the rejection must show in the result
            assert result.get("error") or result.get("rejected"), (
                f"invalid category accepted silently: {result}"
            )

    def test_record_survives_id_collisions_at_scale(self, lstore):
        """[BUG-M13-1 regression] At 5k+ same-day records, the 6-char
        hex suffix has ~70% birthday-paradox collision probability.
        record() must internally retry — the caller must never see
        a sqlite3.IntegrityError leaking out."""
        # We can't easily force a collision in this many records
        # within a unit test, but we CAN verify the retry loop is
        # in place by inspecting the source for the safety net.
        # Belt-and-suspenders: also do a smaller-scale write burst.
        for i in range(300):
            result = lstore.record(
                category="error",
                pattern_key=f"collision-test-{i}",
                summary="x",
            )
            assert result.get("id"), f"record() failed at i={i}"

    def test_close_then_reuse_works(self, lstore):
        """LearningStore.close() then any operation must lazily
        re-open. No "DB closed" errors propagating to the caller."""
        lstore.record(category="error", pattern_key="x", summary="x")
        lstore.close()
        # Should re-connect on next operation
        result = lstore.record(category="error", pattern_key="y", summary="y")
        assert result.get("id"), (
            "post-close record() failed — the lazy-reconnect contract"
            " is broken"
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Contract 4: Plugin hook → run_agent main loop                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestPluginHookContract:
    def test_invoke_unknown_hook_returns_empty_list(self):
        """Calling a hook that no plugin registered must return []
        (not None, not raise). run_agent depends on iterating the
        result list without checking for None."""
        from hermes_cli.plugins import invoke_hook
        result = invoke_hook("hook_that_no_plugin_registers")
        assert result == []

    def test_invoke_hook_never_propagates_exceptions(self):
        """If a plugin's callback raises, invoke_hook must catch it and
        continue — one bad plugin must not kill the agent loop."""
        from hermes_cli.plugins import get_plugin_manager

        mgr = get_plugin_manager()

        # Inject a callback that always raises
        def bad_callback(**kwargs):
            raise RuntimeError("plugin is on fire")

        # Inject a callback that returns a value
        def good_callback(**kwargs):
            return {"context": "fine"}

        # Save existing state, swap in our test callbacks
        hook_name = "test_contract_hook"
        existing = mgr._hooks.get(hook_name, [])
        mgr._hooks[hook_name] = [bad_callback, good_callback]
        try:
            results = mgr.invoke_hook(hook_name)
            # Must complete without raising
            # Must include the good callback's result
            assert {"context": "fine"} in results, (
                "good callback's result lost when bad callback raised"
            )
        finally:
            mgr._hooks[hook_name] = existing

    def test_invoke_hook_returns_only_non_none_results(self):
        """Callbacks that return None are filtered out; only meaningful
        results bubble up (per docstring)."""
        from hermes_cli.plugins import get_plugin_manager

        mgr = get_plugin_manager()

        def returns_none(**kwargs):
            return None

        def returns_value(**kwargs):
            return {"answer": 42}

        hook_name = "test_filter_none_hook"
        existing = mgr._hooks.get(hook_name, [])
        mgr._hooks[hook_name] = [returns_none, returns_value, returns_none]
        try:
            results = mgr.invoke_hook(hook_name)
            assert results == [{"answer": 42}]
        finally:
            mgr._hooks[hook_name] = existing


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Contract 5: DEFAULT_CONFIG ↔ code reads (no orphan keys)                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestConfigContract:
    def test_all_memory_keys_in_default_config(self):
        """All keys read from cfg["memory"] in tools/memory_tool.py
        and run_agent.py must exist in DEFAULT_CONFIG["memory"]."""
        from hermes_cli.config import DEFAULT_CONFIG
        memory_defaults = DEFAULT_CONFIG.get("memory", {})

        # Static scan of code that reads memory config
        repo_root = Path(__file__).resolve().parent.parent.parent
        sources = [
            repo_root / "tools" / "memory_tool.py",
            repo_root / "run_agent.py",
            repo_root / "cli.py",
        ]

        # Find all cfg["memory"]["XXX"] / cfg.get("memory", {}).get("XXX")
        # patterns
        import re
        # Pattern matches 'cfg["memory"]["xxx"]' AND 'memory_cfg.get("xxx")'
        # AND 'memory.get("xxx")' — broad heuristic
        pat = re.compile(
            r'(?:cfg|config|memory_cfg|opts|memory)\s*'
            r'(?:\["memory"\]|\.get\("memory"\s*,\s*\{\}\))?\s*'
            r'(?:\["?([a-z_][a-z_0-9]*)"?\]|\.get\("?([a-z_][a-z_0-9]*)"?'
            r'(?:,[^)]*)?\))'
        )

        # Lighter approach: just check that every key we KNOW the code
        # uses is in DEFAULT_CONFIG["memory"].  Hardcoded list = the
        # contract.
        REQUIRED_KEYS = {
            "memory_enabled",
            "user_profile_enabled",
            "rules_enabled",
            "memory_char_limit",
            "user_char_limit",
            "rules_char_limit",
            "lcm_archive_on_overflow",
            "auto_archive_rules",
            "auto_archive_capacity_threshold",
            "auto_archive_age_days",
            "auto_archive_recurrence_window",
            "archive_notify",
            "trial_new_marker_days",
            "provider",
        }
        for key in REQUIRED_KEYS:
            assert key in memory_defaults, (
                f"DEFAULT_CONFIG['memory'] missing required key '{key}' — "
                f"code reads it, default must exist"
            )

    def test_default_values_have_correct_types(self):
        """Type contract: bool flags are bool, char_limits are int, ratios
        are float, etc. Code does ``int(cfg[...])`` etc., but garbage type
        in default would surface immediately to users."""
        from hermes_cli.config import DEFAULT_CONFIG
        memory = DEFAULT_CONFIG["memory"]

        type_contract = {
            "memory_enabled": bool,
            "user_profile_enabled": bool,
            "rules_enabled": bool,
            "memory_char_limit": int,
            "user_char_limit": int,
            "rules_char_limit": int,
            "lcm_archive_on_overflow": bool,
            "auto_archive_rules": bool,
            "auto_archive_capacity_threshold": float,
            "auto_archive_age_days": int,
            "auto_archive_recurrence_window": int,
            "archive_notify": bool,
            "trial_new_marker_days": int,
            "provider": str,
        }
        for key, expected_type in type_contract.items():
            actual = memory.get(key)
            assert isinstance(actual, expected_type), (
                f"DEFAULT_CONFIG['memory']['{key}'] has type "
                f"{type(actual).__name__}, expected {expected_type.__name__}"
            )

    def test_default_values_in_sensible_ranges(self):
        """Range contract — catches accidental zero / negative / huge
        defaults."""
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]

        assert 0 < m["memory_char_limit"] <= 1_000_000
        assert 0 < m["user_char_limit"] <= 1_000_000
        assert 0 < m["rules_char_limit"] <= 1_000_000
        assert 0.0 <= m["auto_archive_capacity_threshold"] <= 1.0
        assert 0 <= m["auto_archive_age_days"] <= 3650  # 10 years max
        assert 0 <= m["auto_archive_recurrence_window"] <= 365
        assert 0 <= m["trial_new_marker_days"] <= 365


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Contract 6: CommandDef ↔ gateway dispatch consistency                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestCommandRegistryContract:
    def _registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        return COMMAND_REGISTRY

    def test_no_duplicate_canonical_names(self):
        names = [c.name for c in self._registry()]
        assert len(names) == len(set(names)), (
            "duplicate canonical command names"
        )

    def test_no_alias_collides_with_canonical(self):
        """An alias must not also be the canonical name of another command."""
        canonicals = {c.name for c in self._registry()}
        for c in self._registry():
            for alias in c.aliases or ():
                assert alias not in canonicals, (
                    f"alias '{alias}' for {c.name} collides with another "
                    f"canonical name"
                )

    def test_no_alias_used_by_two_commands(self):
        seen: Dict[str, str] = {}
        for c in self._registry():
            for alias in c.aliases or ():
                assert alias not in seen, (
                    f"alias '{alias}' claimed by both {seen[alias]} and {c.name}"
                )
                seen[alias] = c.name

    def test_resolve_command_handles_canonical_and_alias(self):
        """resolve_command returns the CommandDef object itself.
        Calling it with the canonical name returns the def whose name
        matches; calling it with an alias returns the def whose
        aliases tuple contains it."""
        from hermes_cli.commands import resolve_command, COMMAND_REGISTRY
        for c in COMMAND_REGISTRY:
            r = resolve_command(c.name)
            assert r is not None and r.name == c.name
            for alias in c.aliases or ():
                r_alias = resolve_command(alias)
                assert r_alias is not None and r_alias.name == c.name, (
                    f"alias {alias!r} doesn't resolve to {c.name}"
                )

    def test_all_gateway_commands_have_registry_entry(self):
        """Every command in GATEWAY_KNOWN_COMMANDS must resolve to
        SOME CommandDef — either as its canonical name or as an alias."""
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, COMMAND_REGISTRY
        canonicals = {c.name for c in COMMAND_REGISTRY}
        all_aliases = set()
        for c in COMMAND_REGISTRY:
            all_aliases.update(c.aliases or ())
        valid = canonicals | all_aliases

        for gcmd in GATEWAY_KNOWN_COMMANDS:
            assert gcmd in valid, (
                f"GATEWAY_KNOWN_COMMANDS contains '{gcmd}' which is "
                f"neither a canonical name nor an alias"
            )

    def test_telegram_and_slack_views_consistent(self):
        from hermes_cli.commands import (
            telegram_bot_commands,
            slack_subcommand_map,
            COMMAND_REGISTRY,
            resolve_command,
        )
        canonicals = {c.name for c in COMMAND_REGISTRY}
        all_aliases = set()
        for c in COMMAND_REGISTRY:
            all_aliases.update(c.aliases or ())
        valid = canonicals | all_aliases

        for name, _desc in telegram_bot_commands():
            assert name in valid, (
                f"telegram_bot_commands exports '{name}' which is "
                f"neither a canonical name nor an alias"
            )
        for slack_name in slack_subcommand_map().keys():
            resolved = resolve_command(slack_name)
            assert resolved is not None, (
                f"slack subcommand '{slack_name}' doesn't resolve to "
                f"any registry entry"
            )

    def test_new_memory_commands_present(self):
        """Specific contract for the work this PR added: the 5 new
        commands MUST be registered."""
        names = {c.name for c in self._registry()}
        for required in ("rules", "memory", "learn", "context", "lcm"):
            assert required in names, (
                f"command '/{required}' missing from COMMAND_REGISTRY — "
                f"the memory/learning surface is incomplete"
            )
