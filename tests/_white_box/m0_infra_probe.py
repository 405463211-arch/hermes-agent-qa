"""M0 white-box infra probe (run via scripts/run_tests.sh).

Tests invariants that the existing test suite doesn't cover:
- All new memory.* config keys exist with sane types/values
- Both new toolsets (learning, project_knowledge) registered correctly
- _HERMES_CORE_TOOLS has all 7 new entries
- All 4 new tools register cleanly via tools/registry
- Plugin discovery finds bundled self_learning (auto_enable) and lcm
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Config layer ────────────────────────────────────────────────────────────

NEW_MEMORY_KEYS = {
    "rules_enabled": (bool, True),
    "rules_char_limit": (int, lambda v: v >= 1000),
    "lcm_archive_on_overflow": (bool, True),
    "auto_archive_rules": (bool, True),
    "auto_archive_capacity_threshold": ((int, float), lambda v: 0 < v <= 1),
    "auto_archive_age_days": (int, lambda v: v > 0),
    "auto_archive_recurrence_window": (int, lambda v: v > 0),
    "archive_notify": (bool, True),
    "trial_new_marker_days": (int, lambda v: v > 0),
}


class TestConfigLayer:
    def test_all_new_keys_present(self):
        from hermes_cli.config import DEFAULT_CONFIG
        memory = DEFAULT_CONFIG["memory"]
        for key in NEW_MEMORY_KEYS:
            assert key in memory, f"missing memory.{key}"

    def test_new_keys_have_correct_types(self):
        from hermes_cli.config import DEFAULT_CONFIG
        memory = DEFAULT_CONFIG["memory"]
        for key, (expected_type, _) in NEW_MEMORY_KEYS.items():
            v = memory[key]
            assert isinstance(v, expected_type), (
                f"memory.{key} expected {expected_type} got {type(v).__name__}"
            )

    def test_new_keys_have_sane_values(self):
        from hermes_cli.config import DEFAULT_CONFIG
        memory = DEFAULT_CONFIG["memory"]
        for key, (_, predicate) in NEW_MEMORY_KEYS.items():
            v = memory[key]
            if callable(predicate):
                assert predicate(v), f"memory.{key}={v!r} fails sanity check"
            else:
                assert v == predicate, f"memory.{key}={v!r} != {predicate!r}"

    def test_recurrence_window_smaller_than_age(self):
        """Invariant: age-based eviction must allow more time than the
        recurrence window, otherwise rules with recurrences would never
        get a chance to escape eviction."""
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]
        assert m["auto_archive_recurrence_window"] < m["auto_archive_age_days"], (
            "recurrence_window must be < age_days for the protection to be meaningful"
        )

    def test_trial_new_marker_smaller_than_age(self):
        """Invariant: NEW marker window must be < age_days, otherwise
        the trial period itself would trigger eviction."""
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]
        assert m["trial_new_marker_days"] < m["auto_archive_age_days"]


# ── Toolsets layer ──────────────────────────────────────────────────────────


class TestToolsetsLayer:
    def test_new_toolsets_present(self):
        from toolsets import TOOLSETS
        assert "learning" in TOOLSETS
        assert "project_knowledge" in TOOLSETS

    def test_new_toolsets_have_required_keys(self):
        from toolsets import TOOLSETS
        for ts in ("learning", "project_knowledge"):
            d = TOOLSETS[ts]
            assert "description" in d and d["description"]
            assert "tools" in d and d["tools"]
            assert isinstance(d["tools"], list)
            assert "includes" in d

    def test_learning_toolset_lists_three_tools(self):
        from toolsets import TOOLSETS
        assert set(TOOLSETS["learning"]["tools"]) == {
            "learning_record", "learning_list", "learning_resolve"
        }

    def test_project_knowledge_toolset_lists_four_tools(self):
        from toolsets import TOOLSETS
        assert set(TOOLSETS["project_knowledge"]["tools"]) == {
            "project_knowledge_search",
            "project_knowledge_view",
            "project_knowledge_save",
            "project_knowledge_promote",
        }

    def test_core_tools_include_all_new_entries(self):
        from toolsets import _HERMES_CORE_TOOLS
        new_tools = {
            "learning_record", "learning_list", "learning_resolve",
            "project_knowledge_search", "project_knowledge_view",
            "project_knowledge_save", "project_knowledge_promote",
        }
        missing = new_tools - set(_HERMES_CORE_TOOLS)
        assert not missing, f"missing from _HERMES_CORE_TOOLS: {missing}"

    def test_no_duplicate_tools_in_core(self):
        """Invariant: a tool name should appear at most once in _HERMES_CORE_TOOLS."""
        from toolsets import _HERMES_CORE_TOOLS
        seen = {}
        dups = []
        for t in _HERMES_CORE_TOOLS:
            if t in seen:
                dups.append(t)
            seen[t] = True
        assert not dups, f"duplicate tools in _HERMES_CORE_TOOLS: {dups}"


# ── Registry layer ──────────────────────────────────────────────────────────


NEW_TOOL_NAMES = (
    "learning_record", "learning_list", "learning_resolve",
    "project_knowledge_search", "project_knowledge_view",
    "project_knowledge_save", "project_knowledge_promote",
)


class TestRegistryLayer:
    def test_all_new_tools_registered(self):
        import model_tools  # noqa: F401  triggers discover_builtin_tools
        from tools.registry import registry
        for name in NEW_TOOL_NAMES:
            assert registry.get_entry(name) is not None, f"{name} not registered"

    def test_new_tool_schemas_well_formed(self):
        import model_tools  # noqa: F401
        from tools.registry import registry
        for name in NEW_TOOL_NAMES:
            entry = registry.get_entry(name)
            schema = entry.schema
            assert schema.get("name", name) in (name, ""), (
                f"schema name mismatch for {name}: {schema.get('name')!r}"
            )
            assert "description" in schema and schema["description"], (
                f"{name} missing description"
            )
            assert "parameters" in schema, f"{name} missing parameters"
            params = schema["parameters"]
            assert isinstance(params, dict), f"{name} parameters not dict"
            assert params.get("type") == "object", f"{name} parameters not object type"

    def test_new_tool_handlers_callable(self):
        import model_tools  # noqa: F401
        from tools.registry import registry
        for name in NEW_TOOL_NAMES:
            entry = registry.get_entry(name)
            assert callable(entry.handler), f"{name} handler not callable"

    def test_new_tools_belong_to_correct_toolset(self):
        """Invariant: registered toolset name must match toolsets.py declaration."""
        import model_tools  # noqa: F401
        from tools.registry import registry
        from toolsets import TOOLSETS
        # Build expected map: tool -> toolset (using the toolsets.py decl)
        expected = {}
        for ts_name in ("learning", "project_knowledge"):
            for tool_name in TOOLSETS[ts_name]["tools"]:
                expected[tool_name] = ts_name
        for name, expected_ts in expected.items():
            entry = registry.get_entry(name)
            assert entry.toolset == expected_ts, (
                f"{name} registered under {entry.toolset!r}, expected {expected_ts!r}"
            )

    def test_no_cross_tool_hardcoded_references_in_descriptions(self):
        """Per AGENTS.md: tool schemas must not name-reference tools from
        other toolsets (causes hallucinated calls when toolsets disabled).
        """
        import model_tools  # noqa: F401
        from tools.registry import registry
        from toolsets import TOOLSETS

        learning_tools = set(TOOLSETS["learning"]["tools"])
        pk_tools = set(TOOLSETS["project_knowledge"]["tools"])
        all_other_tools = set()
        for ts_name, ts in TOOLSETS.items():
            if ts_name in ("learning", "project_knowledge"):
                continue
            all_other_tools.update(ts.get("tools", []))

        for tool_name in learning_tools | pk_tools:
            entry = registry.get_entry(tool_name)
            desc = (entry.schema.get("description", "") or "").lower()
            siblings = learning_tools if tool_name in learning_tools else pk_tools
            for other in all_other_tools:
                if other in siblings or other == tool_name:
                    continue
                # reject only specific-tool references like "use other_tool"
                # accept generic word matches inside descriptions
                if f"`{other}`" in desc or f"call {other}" in desc:
                    pytest.fail(
                        f"{tool_name} description hard-references {other!r}: {desc!r}"
                    )


# ── Plugin discovery layer ──────────────────────────────────────────────────


@pytest.fixture
def fresh_plugin_manager(monkeypatch):
    """Fresh PluginManager with stubbed config getters.

    PluginManager reads disabled/enabled lists via load_config(); our autouse
    HERMES_HOME isolation gives a fresh config dir, but we still want explicit
    control to test the disabled-via-config path deterministically.
    """
    import hermes_cli.plugins as plugins_mod

    enabled_holder = {"value": None}  # None = opt-in default
    disabled_holder = {"value": set()}

    monkeypatch.setattr(
        plugins_mod, "_get_enabled_plugins", lambda: enabled_holder["value"]
    )
    monkeypatch.setattr(
        plugins_mod, "_get_disabled_plugins", lambda: disabled_holder["value"]
    )

    def make(disabled=None, enabled=None):
        disabled_holder["value"] = set(disabled or [])
        enabled_holder["value"] = set(enabled) if enabled is not None else None
        mgr = plugins_mod.PluginManager()
        mgr.discover_and_load(force=True)
        return mgr

    return make


class TestPluginDiscovery:
    def test_self_learning_discovered(self, fresh_plugin_manager):
        mgr = fresh_plugin_manager()
        names = {p["name"] for p in mgr.list_plugins()}
        assert "self_learning" in names, (
            f"self_learning not discovered. discovered={sorted(names)}"
        )

    def test_self_learning_auto_enabled(self, fresh_plugin_manager):
        """auto_enable=true bundled standalone plugins must be loaded
        even when not in plugins.enabled."""
        mgr = fresh_plugin_manager(enabled=[])  # explicit empty allow-list
        sl = next(
            (p for p in mgr.list_plugins() if p["name"] == "self_learning"), None
        )
        assert sl is not None
        assert sl["enabled"] is True, (
            f"self_learning not auto-loaded with empty plugins.enabled: "
            f"error={sl.get('error')!r}, source={sl.get('source')!r}"
        )
        # Sanity: the hooks list should be non-empty (post_tool_call + pre_llm_call)
        assert sl["hooks"] >= 1, (
            f"self_learning loaded but registered no hooks: {sl}"
        )

    def test_self_learning_disabled_via_config(self, fresh_plugin_manager):
        """Even auto_enable plugins must respect plugins.disabled."""
        mgr = fresh_plugin_manager(disabled=["self_learning"])
        sl = next(
            (p for p in mgr.list_plugins() if p["name"] == "self_learning"), None
        )
        assert sl is not None
        assert sl["enabled"] is False, (
            "self_learning should not load when in plugins.disabled"
        )
        assert sl.get("error") and "disabled" in sl["error"].lower(), (
            f"expected 'disabled' marker in error, got: {sl.get('error')!r}"
        )

    def test_lcm_context_engine_present_on_disk(self):
        """plugins/context_engine/lcm/ must exist; it has its own loader
        (general PluginManager skips context_engine/), so we just verify
        the disk artifact here. Wiring is tested at M3/M7."""
        lcm_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "plugins" / "context_engine" / "lcm"
        )
        assert lcm_dir.is_dir(), f"plugins/context_engine/lcm missing"
        manifest = lcm_dir / "plugin.yaml"
        assert manifest.is_file(), f"missing manifest: {manifest}"

    def test_list_plugins_sorted_by_name(self, fresh_plugin_manager):
        """Regression for the bug fixed during M0: ordering MUST be by
        display name, not by the registry key (which embeds path prefix)."""
        mgr = fresh_plugin_manager()
        names = [p["name"] for p in mgr.list_plugins()]
        assert names == sorted(names), (
            f"list_plugins() not sorted by name: {names}"
        )
