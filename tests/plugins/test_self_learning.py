"""Tests for plugins/self_learning — error detection and nudge throttling."""

from __future__ import annotations

import json

import pytest

from plugins.self_learning import (
    _on_post_tool_call,
    _on_pre_llm_call,
    _pattern_state,
    _nudged_state,
)
from plugins.self_learning.error_detector import (
    classify_tool_error,
    pattern_key_for,
    DEFAULT_NUDGE_THRESHOLD,
)


@pytest.fixture(autouse=True)
def reset_state():
    _pattern_state.clear()
    _nudged_state.clear()
    yield
    _pattern_state.clear()
    _nudged_state.clear()


# ---------------------------------------------------------------------------
# error_detector.classify_tool_error
# ---------------------------------------------------------------------------


class TestClassifyToolError:
    def test_terminal_zero_exit_returns_none(self):
        result = json.dumps({"exit_code": 0, "stdout": "ok"})
        assert classify_tool_error("terminal", {}, result) is None

    def test_terminal_nonzero_exit_classified(self):
        result = json.dumps({"exit_code": 1, "stderr": "boom"})
        cls = classify_tool_error("terminal", {}, result)
        assert cls is not None
        assert cls["kind"] == "exit_nonzero"
        assert cls["signal"] == "exit_1"
        assert "terminal" in cls["summary"]

    def test_permission_denied_signal(self):
        result = json.dumps({"exit_code": 1, "stderr": "permission denied: foo.txt"})
        cls = classify_tool_error("terminal", {}, result)
        assert cls["kind"] == "permission_denied"
        assert cls["signal"] == "permission_denied"

    def test_not_found_signal(self):
        result = json.dumps({"success": False, "error": "no such file or directory"})
        cls = classify_tool_error("read_file", {}, result)
        assert cls["signal"] == "not_found"

    def test_explicit_success_false_classified(self):
        result = json.dumps({"success": False, "error": "API quota exceeded"})
        cls = classify_tool_error("web_search", {}, result)
        assert cls is not None
        assert cls["kind"] == "tool_failure"

    def test_non_json_result_returns_none(self):
        assert classify_tool_error("terminal", {}, "raw stdout text") is None

    def test_empty_inputs_return_none(self):
        assert classify_tool_error("", {}, "") is None
        assert classify_tool_error("terminal", {}, "") is None

    def test_non_dict_json_returns_none(self):
        assert classify_tool_error("terminal", {}, json.dumps([1, 2, 3])) is None


class TestPatternKey:
    def test_pattern_key_form(self):
        cls = {"signal": "permission_denied"}
        assert pattern_key_for("Terminal", cls) == "tool.terminal.permission_denied"

    def test_unknown_fallbacks(self):
        assert pattern_key_for("", {}) == "tool.unknown.unknown"


# ---------------------------------------------------------------------------
# Plugin hook flow
# ---------------------------------------------------------------------------


class TestPostToolCallHook:
    def test_records_failure(self):
        result = json.dumps({"exit_code": 1, "stderr": "boom"})
        _on_post_tool_call(
            tool_name="terminal", args={}, result=result, session_id="s1"
        )
        assert "tool.terminal.exit_1" in _pattern_state["s1"]
        assert len(_pattern_state["s1"]["tool.terminal.exit_1"]) == 1

    def test_no_record_on_success(self):
        result = json.dumps({"exit_code": 0, "stdout": "ok"})
        _on_post_tool_call(
            tool_name="terminal", args={}, result=result, session_id="s1"
        )
        assert "s1" not in _pattern_state or not _pattern_state["s1"]

    def test_isolated_per_session(self):
        result = json.dumps({"exit_code": 1, "stderr": "x"})
        _on_post_tool_call(tool_name="terminal", args={}, result=result, session_id="A")
        _on_post_tool_call(tool_name="terminal", args={}, result=result, session_id="B")
        assert len(_pattern_state["A"]["tool.terminal.exit_1"]) == 1
        assert len(_pattern_state["B"]["tool.terminal.exit_1"]) == 1


class TestPreLlmCallNudge:
    def test_no_nudge_below_threshold(self):
        result = json.dumps({"exit_code": 1, "stderr": "x"})
        for _ in range(DEFAULT_NUDGE_THRESHOLD - 1):
            _on_post_tool_call(tool_name="terminal", args={}, result=result, session_id="s")
        assert _on_pre_llm_call(session_id="s") is None

    def test_nudges_at_threshold(self):
        result = json.dumps({"exit_code": 1, "stderr": "permission denied: x"})
        for _ in range(DEFAULT_NUDGE_THRESHOLD):
            _on_post_tool_call(tool_name="terminal", args={}, result=result, session_id="s")
        out = _on_pre_llm_call(session_id="s")
        assert out is not None
        assert "context" in out
        assert "learning_record" in out["context"]
        assert "tool.terminal.permission_denied" in out["context"]

    def test_nudges_only_once_per_pattern(self):
        result = json.dumps({"exit_code": 1, "stderr": "permission denied: x"})
        for _ in range(DEFAULT_NUDGE_THRESHOLD):
            _on_post_tool_call(tool_name="terminal", args={}, result=result, session_id="s")
        first = _on_pre_llm_call(session_id="s")
        second = _on_pre_llm_call(session_id="s")
        assert first is not None
        assert second is None  # nudge throttled

    def test_no_session_id_no_nudge(self):
        result = json.dumps({"exit_code": 1, "stderr": "x"})
        _on_post_tool_call(tool_name="terminal", args={}, result=result, session_id="")
        assert _on_pre_llm_call(session_id="") is None
