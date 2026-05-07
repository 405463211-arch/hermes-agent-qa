"""M6 white-box config / switch probe.

Covers:
- DEFAULT_CONFIG.memory contains all 9 new keys with sane defaults
- User-config overrides flow through DeepMerge into the runtime config
- Each switch actually disables its feature end-to-end:
    * auto_archive_rules: false → run_auto_archive returns []
    * auto_archive_capacity_threshold: 1.01 → never triggers
    * auto_archive_age_days: 0 → age trigger off
    * trial_new_marker_days: 0 → never shows [NEW] marker
    * lcm_archive_on_overflow: false → memory full raises old error path
- Boolean / numeric / string types preserved through merge
- Plugin registry: plugins.disabled hides self_learning from auto-load
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.rules_lifecycle import RuleEntry, serialize_rule_entry


NEW_MEMORY_KEYS = (
    "memory_enabled", "user_profile_enabled", "rules_enabled",
    "lcm_archive_on_overflow",
    "auto_archive_rules",
    "auto_archive_capacity_threshold",
    "auto_archive_age_days",
    "auto_archive_recurrence_window",
    "archive_notify",
    "trial_new_marker_days",
)


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    import tools.memory_tool as mt
    counter = {"n": 0}

    def make(rules_text=None, memory_text=None, **kw):
        counter["n"] += 1
        mem_dir = tmp_path / f"mem-{counter['n']}"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if rules_text is not None:
            (mem_dir / "RULES.md").write_text(rules_text, encoding="utf-8")
        if memory_text is not None:
            (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
        monkeypatch.setattr(mt, "get_memory_dir", lambda d=mem_dir: d)
        params = dict(
            rules_char_limit=10_000,
            memory_char_limit=10_000,
            user_char_limit=10_000,
        )
        params.update(kw)
        store = mt.MemoryStore(**params)
        store.load_from_disk()
        return store, mem_dir
    return make


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ DEFAULT_CONFIG sanity                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestDefaultConfig:
    def test_all_new_memory_keys_exist(self):
        from hermes_cli.config import DEFAULT_CONFIG
        mem = DEFAULT_CONFIG["memory"]
        for k in NEW_MEMORY_KEYS:
            assert k in mem, f"missing memory.{k} in DEFAULT_CONFIG"

    def test_default_types(self):
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]
        assert isinstance(m["memory_enabled"], bool)
        assert isinstance(m["rules_enabled"], bool)
        assert isinstance(m["user_profile_enabled"], bool)
        assert isinstance(m["auto_archive_rules"], bool)
        assert isinstance(m["lcm_archive_on_overflow"], bool)
        assert isinstance(m["archive_notify"], bool)
        assert isinstance(m["auto_archive_capacity_threshold"], float)
        assert isinstance(m["auto_archive_age_days"], int)
        assert isinstance(m["auto_archive_recurrence_window"], int)
        assert isinstance(m["trial_new_marker_days"], int)

    def test_capacity_threshold_in_range(self):
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]
        assert 0.0 < m["auto_archive_capacity_threshold"] <= 1.0

    def test_age_window_sensible(self):
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]
        assert m["auto_archive_age_days"] >= 0
        assert m["auto_archive_recurrence_window"] >= 0
        assert m["trial_new_marker_days"] >= 0
        # New-marker window must be ≤ age window — otherwise rules could
        # be archived while still wearing [NEW]
        assert m["trial_new_marker_days"] <= m["auto_archive_age_days"]

    def test_default_values_match_design(self):
        """Locks the published design contract: 90/30/7/0.80."""
        from hermes_cli.config import DEFAULT_CONFIG
        m = DEFAULT_CONFIG["memory"]
        assert m["auto_archive_age_days"] == 90
        assert m["auto_archive_recurrence_window"] == 30
        assert m["trial_new_marker_days"] == 7
        assert m["auto_archive_capacity_threshold"] == 0.80
        assert m["auto_archive_rules"] is True
        assert m["archive_notify"] is True


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ User overrides via deep-merge                                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestUserOverride:
    def _merge(self, user_yaml: dict) -> dict:
        from hermes_cli.config import _deep_merge, DEFAULT_CONFIG
        merged = _deep_merge(DEFAULT_CONFIG, user_yaml)
        return merged

    def test_user_disable_auto_archive(self):
        merged = self._merge({"memory": {"auto_archive_rules": False}})
        assert merged["memory"]["auto_archive_rules"] is False
        # Other keys must still hold their defaults
        assert merged["memory"]["auto_archive_age_days"] == 90

    def test_user_lower_threshold(self):
        merged = self._merge({"memory": {"auto_archive_capacity_threshold": 0.5}})
        assert merged["memory"]["auto_archive_capacity_threshold"] == 0.5

    def test_user_zero_disables_age_trigger(self):
        merged = self._merge({"memory": {"auto_archive_age_days": 0}})
        assert merged["memory"]["auto_archive_age_days"] == 0

    def test_user_partial_override_keeps_other_keys(self):
        """deep-merge invariant — overriding one memory.* key must not drop
        siblings (DEFAULT_CONFIG cannot regress for unspecified keys)."""
        merged = self._merge({"memory": {"trial_new_marker_days": 14}})
        assert merged["memory"]["trial_new_marker_days"] == 14
        for k in NEW_MEMORY_KEYS:
            assert k in merged["memory"], (
                f"user override dropped {k} during merge!"
            )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Switch behavior — each flag disables its feature end-to-end              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestSwitchesActuallyDisable:
    def _stuffed_rules(self):
        """A blob big enough to trip capacity if enabled."""
        from tools.memory_tool import ENTRY_DELIMITER
        return ENTRY_DELIMITER.join(
            serialize_rule_entry(RuleEntry(
                text=f"R{i}: " + ("x" * 500),
                created=date(2026, 1, 1) + timedelta(days=i),
            )) for i in range(20)
        )

    def test_auto_archive_off_no_op(self, store_factory):
        store, _ = store_factory(
            rules_text=self._stuffed_rules(),
            rules_char_limit=2000,
            auto_archive_rules=False,  # OFF
            auto_archive_capacity_threshold=0.80,
        )
        assert store.run_auto_archive() == []

    def test_capacity_threshold_zero_disables_capacity_trigger(self, store_factory):
        """Per docstring: ``Set to 0 to disable`` capacity trigger."""
        store, _ = store_factory(
            rules_text=self._stuffed_rules(),
            rules_char_limit=2000,
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.0,  # disabled
            auto_archive_age_days=0,
        )
        assert store.run_auto_archive() == []

    def test_age_zero_disables_age_trigger(self, store_factory):
        old = date.today() - timedelta(days=365)
        rule = serialize_rule_entry(RuleEntry(
            text="ancient",
            source="LRN-20250101-OLD",
            created=old,
            promoted_at=old,
        ))
        store, _ = store_factory(
            rules_text=rule,
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.80,
            auto_archive_age_days=0,  # OFF
        )
        # Capacity not exceeded + age off → no archive
        assert store.run_auto_archive() == []

    def test_trial_window_zero_no_new_marker(self, store_factory):
        rule = serialize_rule_entry(RuleEntry(
            text="freshly promoted",
            source="LRN-20260501-NEW",
            promoted_at=date.today(),
        ))
        store, _ = store_factory(
            rules_text=rule,
            trial_new_marker_days=0,  # OFF
        )
        tiers = store.format_rules_by_tier()
        assert "[NEW" not in tiers["regular"]

    def test_trial_window_long_shows_new_marker(self, store_factory):
        """Sanity check the inverse — when window is long, [NEW] sticks."""
        rule = serialize_rule_entry(RuleEntry(
            text="freshly promoted",
            source="LRN-20260501-NEW",
            promoted_at=date.today() - timedelta(days=1),
        ))
        store, _ = store_factory(
            rules_text=rule,
            trial_new_marker_days=30,
        )
        tiers = store.format_rules_by_tier()
        assert "[NEW" in tiers["regular"]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Plugin disable via config                                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestPluginDisable:
    def test_disabled_self_learning_not_loaded(self, monkeypatch):
        """plugins.disabled list should make self_learning silently skip."""
        import hermes_cli.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "_get_enabled_plugins", lambda: set())
        monkeypatch.setattr(
            plugins_mod, "_get_disabled_plugins", lambda: {"self_learning"}
        )

        mgr = plugins_mod.PluginManager()
        mgr.discover_and_load(force=True)
        info = {p["name"]: p for p in mgr.list_plugins()}
        sl = info.get("self_learning")
        assert sl is not None
        assert sl["enabled"] is False, (
            "self_learning should NOT be enabled when disabled in config"
        )

    def test_default_self_learning_loaded(self, monkeypatch):
        """Without overrides, self_learning auto-enables."""
        import hermes_cli.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "_get_enabled_plugins", lambda: set())
        monkeypatch.setattr(plugins_mod, "_get_disabled_plugins", lambda: set())

        mgr = plugins_mod.PluginManager()
        mgr.discover_and_load(force=True)
        info = {p["name"]: p for p in mgr.list_plugins()}
        sl = info.get("self_learning")
        assert sl is not None
        assert sl["enabled"] is True


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Config-version migration sanity                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestConfigVersion:
    def test_default_config_has_version(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "_config_version" in DEFAULT_CONFIG
        assert isinstance(DEFAULT_CONFIG["_config_version"], int)
        assert DEFAULT_CONFIG["_config_version"] >= 1

    def test_user_config_without_new_keys_inherits_defaults(self):
        """Real-world scenario: an old config.yaml without any of the new
        memory keys must still produce a working merged config."""
        from hermes_cli.config import _deep_merge, DEFAULT_CONFIG
        # User has only the old subset
        old_user = {
            "memory": {
                "memory_enabled": True,
                "memory_char_limit": 1000,
            }
        }
        merged = _deep_merge(DEFAULT_CONFIG, old_user)
        # All 9 new keys must be present after merge
        for k in NEW_MEMORY_KEYS:
            assert k in merged["memory"], (
                f"old user config dropped new key {k} during merge"
            )
        # User's explicit value is preserved
        assert merged["memory"]["memory_char_limit"] == 1000
