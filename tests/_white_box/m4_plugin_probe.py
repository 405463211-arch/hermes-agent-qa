"""M4 white-box plugin-coupling probe.

Covers:
- error_detector.classify_tool_error() — every classification branch + the
  None-on-healthy invariant
- error_detector.pattern_key_for() — stable key shape
- self_learning hooks: tally accumulation, threshold trigger, single-shot
  nudging, cross-session isolation, observation-only (no side effects)
- Hooks must NOT raise on weird input (otherwise they'd break the real
  pre/post_tool_call invocation chain in run_agent)
"""
from __future__ import annotations

import json
import threading

import pytest


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ classify_tool_error — every branch                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestClassifyToolError:
    def _classify(self, tool_name, result, args=None):
        from plugins.self_learning.error_detector import classify_tool_error
        return classify_tool_error(tool_name, args or {}, result)

    def test_healthy_json_returns_none(self):
        assert self._classify(
            "memory", json.dumps({"success": True, "data": "x"})
        ) is None

    def test_empty_result_returns_none(self):
        assert self._classify("memory", "") is None

    def test_empty_tool_name_returns_none(self):
        assert self._classify("", json.dumps({"success": False})) is None

    def test_non_json_result_returns_none(self):
        """Tool may return plain text — we don't classify (avoid false pos)."""
        assert self._classify("terminal", "raw stdout text") is None

    def test_terminal_zero_exit_returns_none(self):
        assert self._classify(
            "terminal", json.dumps({"exit_code": 0, "stdout": "ok"})
        ) is None

    def test_terminal_nonzero_exit_classified(self):
        c = self._classify(
            "terminal", json.dumps({"exit_code": 1, "stderr": "boom"})
        )
        assert c is not None
        assert c["kind"] == "exit_nonzero"
        assert c["signal"] == "exit_1"
        assert "boom" in c["summary"]

    def test_explicit_failure_classified(self):
        c = self._classify(
            "memory", json.dumps({"success": False, "error": "bad"})
        )
        assert c is not None
        assert c["kind"] == "tool_failure"

    def test_permission_denied_signal(self):
        c = self._classify(
            "terminal",
            json.dumps({"exit_code": 1, "stderr": "Permission denied: /etc"}),
        )
        assert c["signal"] == "permission_denied"
        assert c["kind"] == "permission_denied"

    def test_not_found_signal(self):
        c = self._classify(
            "terminal",
            json.dumps({"exit_code": 1, "stderr": "No such file or directory"}),
        )
        assert c["signal"] == "not_found"

    def test_timeout_signal(self):
        c = self._classify(
            "terminal",
            json.dumps({"exit_code": 1, "stderr": "Operation timed out"}),
        )
        assert c["signal"] == "timeout"

    def test_explicit_failure_with_permission_message(self):
        c = self._classify(
            "write_file",
            json.dumps({"success": False, "error": "EACCES on file"}),
        )
        assert c["signal"] == "permission_denied"


class TestPatternKeyFor:
    def test_lowercase_and_dotted(self):
        from plugins.self_learning.error_detector import pattern_key_for
        key = pattern_key_for(
            "Terminal", {"signal": "Permission_Denied"}
        )
        assert key == "tool.terminal.permission_denied"

    def test_unknown_signal_falls_back(self):
        from plugins.self_learning.error_detector import pattern_key_for
        key = pattern_key_for("foo", {})
        assert key == "tool.foo.unknown"

    def test_empty_tool_name_falls_back(self):
        from plugins.self_learning.error_detector import pattern_key_for
        key = pattern_key_for("", {"signal": "x"})
        assert key == "tool.unknown.x"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Hook behavior — accumulation + threshold + throttle + isolation         ║
# ╚══════════════════════════════════════════════════════════════════════════╝


@pytest.fixture(autouse=True)
def reset_plugin_state():
    """Each test gets fresh per-session state."""
    import plugins.self_learning as sl
    sl._pattern_state.clear()
    sl._nudged_state.clear()
    yield
    sl._pattern_state.clear()
    sl._nudged_state.clear()


class TestSelfLearningHooks:
    def _post(self, **kw):
        from plugins.self_learning import _on_post_tool_call
        return _on_post_tool_call(**kw)

    def _pre(self, **kw):
        from plugins.self_learning import _on_pre_llm_call
        return _on_pre_llm_call(**kw)

    def _failure_result(self):
        return json.dumps({"exit_code": 1, "stderr": "boom"})

    def test_post_tool_call_accumulates(self):
        for _ in range(3):
            self._post(
                tool_name="terminal",
                result=self._failure_result(),
                session_id="S1",
            )
        import plugins.self_learning as sl
        assert (
            "tool.terminal.exit_1" in sl._pattern_state["S1"]
        )
        assert len(sl._pattern_state["S1"]["tool.terminal.exit_1"]) == 3

    def test_post_tool_call_ignores_healthy_results(self):
        self._post(
            tool_name="memory",
            result=json.dumps({"success": True, "data": "ok"}),
            session_id="S1",
        )
        import plugins.self_learning as sl
        assert sl._pattern_state.get("S1") in (None, {}) or not any(
            sl._pattern_state["S1"].values()
        )

    def test_pre_llm_call_returns_none_below_threshold(self):
        # 1 occurrence < threshold (2)
        self._post(
            tool_name="terminal",
            result=self._failure_result(),
            session_id="S1",
        )
        result = self._pre(session_id="S1")
        assert result is None

    def test_pre_llm_call_nudges_at_threshold(self):
        for _ in range(2):
            self._post(
                tool_name="terminal",
                result=self._failure_result(),
                session_id="S1",
            )
        result = self._pre(session_id="S1")
        assert result is not None
        assert "context" in result
        assert "learning_record" in result["context"]
        assert "tool.terminal.exit_1" in result["context"]

    def test_nudge_only_fires_once_per_pattern(self):
        """Throttle: same pattern, repeated trigger, only one nudge."""
        for _ in range(5):
            self._post(
                tool_name="terminal",
                result=self._failure_result(),
                session_id="S1",
            )
        first = self._pre(session_id="S1")
        second = self._pre(session_id="S1")
        third = self._pre(session_id="S1")
        assert first is not None and second is None and third is None

    def test_session_isolation(self):
        """Patterns from session A must not trigger nudges in session B."""
        for _ in range(3):
            self._post(
                tool_name="terminal",
                result=self._failure_result(),
                session_id="S1",
            )
        # B has no recorded errors
        assert self._pre(session_id="S2") is None
        # And A still nudges normally
        assert self._pre(session_id="S1") is not None

    def test_pre_llm_call_with_empty_session_returns_none(self):
        for _ in range(3):
            self._post(
                tool_name="terminal",
                result=self._failure_result(),
                session_id="",  # no session
            )
        # Without a session_id, no nudge (we can't safely throttle cross-session)
        assert self._pre(session_id="") is None


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Hook robustness — must not break the LLM call chain                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestHookRobustness:
    """Per AGENTS.md, plugins must NEVER block real LLM/tool routes. Hooks
    that crash on unexpected input would do exactly that. Each branch we test
    here matches a real shape the runner could pass us."""

    def test_post_with_no_args_does_not_raise(self):
        from plugins.self_learning import _on_post_tool_call
        _on_post_tool_call()  # all defaults

    def test_post_with_none_result_does_not_raise(self):
        from plugins.self_learning import _on_post_tool_call
        _on_post_tool_call(tool_name="terminal", result=None)

    def test_post_with_garbage_json_does_not_raise(self):
        from plugins.self_learning import _on_post_tool_call
        _on_post_tool_call(tool_name="terminal", result="{not valid json")

    def test_post_with_huge_blob_does_not_raise(self):
        from plugins.self_learning import _on_post_tool_call
        big = json.dumps({"success": False, "stderr": "X" * 1_000_000})
        _on_post_tool_call(tool_name="terminal", result=big, session_id="S")

    def test_pre_with_no_state_returns_none(self):
        from plugins.self_learning import _on_pre_llm_call
        assert _on_pre_llm_call(session_id="never-seen") is None

    def test_pre_with_no_session_returns_none(self):
        from plugins.self_learning import _on_pre_llm_call
        assert _on_pre_llm_call() is None

    def test_concurrent_post_calls_thread_safe(self):
        """Lock protects the dict.  Many threads, no exception, count exact."""
        from plugins.self_learning import _on_post_tool_call
        N = 50

        def worker():
            for _ in range(20):
                _on_post_tool_call(
                    tool_name="terminal",
                    result=json.dumps({"exit_code": 1, "stderr": "boom"}),
                    session_id="STRESS",
                )

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        import plugins.self_learning as sl
        bucket = sl._pattern_state["STRESS"]["tool.terminal.exit_1"]
        assert len(bucket) == N * 20  # no lost updates


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Plugin entry-point contract                                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestPluginRegistration:
    def test_register_wires_both_hooks(self):
        from plugins.self_learning import register

        registered = []

        class Ctx:
            def register_hook(self, name, fn):
                registered.append((name, fn))

        register(Ctx())
        names = [name for name, _ in registered]
        assert "post_tool_call" in names
        assert "pre_llm_call" in names

    def test_pre_llm_call_returns_serializable_dict(self):
        """The dict gets serialized into the LLM message stream — must be
        JSON-friendly (no Python objects, no bytes)."""
        from plugins.self_learning import _on_post_tool_call, _on_pre_llm_call
        for _ in range(3):
            _on_post_tool_call(
                tool_name="terminal",
                result=json.dumps({"exit_code": 1, "stderr": "x"}),
                session_id="JSON",
            )
        result = _on_pre_llm_call(session_id="JSON")
        assert result is not None
        # round-trip through json — would raise if any non-serializable val
        json.dumps(result)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ End-to-end: hook invocation chain doesn't crash on Plugin discovery      ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestPluginManagerIntegration:
    def test_self_learning_hook_visible_to_invoke_hook(self, monkeypatch):
        """After discovery, invoke_hook('post_tool_call', ...) should not
        raise even with empty/odd inputs — the plugin's hook runs as one
        of N hooks under the manager's exception isolation."""
        import hermes_cli.plugins as plugins_mod

        # Stub config getters so discovery is deterministic
        monkeypatch.setattr(plugins_mod, "_get_enabled_plugins", lambda: set())
        monkeypatch.setattr(plugins_mod, "_get_disabled_plugins", lambda: set())

        # Fresh manager with self_learning auto-loaded
        mgr = plugins_mod.PluginManager()
        mgr.discover_and_load(force=True)

        # Replace the singleton briefly so invoke_hook routes through us
        old_mgr = plugins_mod._plugin_manager
        plugins_mod._plugin_manager = mgr
        try:
            # Should not raise
            plugins_mod.invoke_hook(
                "post_tool_call",
                tool_name="terminal",
                args={},
                result=json.dumps({"exit_code": 1, "stderr": "x"}),
                task_id="t",
                session_id="S-INTEG",
                tool_call_id="c",
            )
            results = plugins_mod.invoke_hook(
                "pre_llm_call", session_id="S-INTEG"
            )
            # Should be a list; if self_learning fired, it returns one dict
            assert isinstance(results, list)
        finally:
            plugins_mod._plugin_manager = old_mgr
