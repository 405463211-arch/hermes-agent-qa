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
    per_tool_pattern_key_for,
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
    def test_pattern_key_falls_back_to_per_tool_when_no_category(self):
        """Hand-built classifications without ``category`` use the legacy
        per-tool key so external API callers don't break."""
        cls = {"signal": "permission_denied"}
        assert pattern_key_for("Terminal", cls) == "tool.terminal.permission_denied"

    def test_unknown_fallbacks(self):
        assert pattern_key_for("", {}) == "tool.unknown.unknown"

    def test_pattern_key_uses_category_when_present(self):
        """Cross-tool bucketing key takes precedence over per-tool."""
        cls = {"signal": "not_found", "category": "file_io"}
        assert pattern_key_for("read_file", cls) == "cat.file_io.not_found"

    def test_per_tool_key_helper_always_returns_tool_form(self):
        """``per_tool_pattern_key_for`` is for human-facing summaries —
        it must always return the legacy ``tool.<tool>.<signal>`` form
        even when ``category`` is set."""
        cls = {"signal": "not_found", "category": "file_io"}
        assert (
            per_tool_pattern_key_for("read_file", cls)
            == "tool.read_file.not_found"
        )


# ---------------------------------------------------------------------------
# Plugin hook flow
# ---------------------------------------------------------------------------


class TestPostToolCallHook:
    def test_records_failure(self):
        result = json.dumps({"exit_code": 1, "stderr": "boom"})
        _on_post_tool_call(
            tool_name="terminal", args={}, result=result, session_id="s1"
        )
        # exit_1 isn't in the file-io-promoting signal list, so the
        # bucket stays under the generic shell category.
        assert "cat.shell.exit_1" in _pattern_state["s1"]
        assert len(_pattern_state["s1"]["cat.shell.exit_1"]) == 1

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
        assert len(_pattern_state["A"]["cat.shell.exit_1"]) == 1
        assert len(_pattern_state["B"]["cat.shell.exit_1"]) == 1


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
        # permission_denied promotes terminal → file_io category.
        assert "cat.file_io.permission_denied" in out["context"]
        # The nudge must still surface the concrete tool name so the
        # user/agent can see *which* tool triggered it.
        assert "terminal" in out["context"]

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


# ---------------------------------------------------------------------------
# Cross-tool semantic bucketing (the actual fix this commit lands)
# ---------------------------------------------------------------------------


class TestCrossToolBucketing:
    """The bug being fixed: an LLM hitting ``not_found`` once via
    ``read_file`` and once via ``terminal`` ``cat`` used to register two
    separate per-tool buckets at count=1 — neither crossed the nudge
    threshold, so the self-learning loop stayed silent on a pattern that
    is semantically the same. After the fix both bucket under
    ``cat.file_io.not_found`` and cross threshold on the second call.
    """

    def test_classify_includes_category_field(self):
        """Sanity: classifications must surface the new ``category`` key."""
        result = json.dumps({"exit_code": 1, "stderr": "no such file or directory"})
        cls = classify_tool_error("terminal", {}, result)
        assert cls is not None
        assert cls["category"] == "file_io"
        assert cls["signal"] == "not_found"

    def test_terminal_exit_nonzero_without_file_signal_stays_shell(self):
        """Generic terminal failures (e.g. compile error, unrelated exit
        code) must NOT bucket as file_io — they belong to the shell
        category so they don't pollute file-I/O recurrence counters."""
        result = json.dumps({"exit_code": 2, "stderr": "syntax error near unexpected token"})
        cls = classify_tool_error("terminal", {}, result)
        assert cls is not None
        assert cls["category"] == "shell"

    def test_read_file_and_terminal_cat_share_bucket_on_not_found(self):
        """The end-to-end contract: hitting not_found once via
        ``read_file`` then once via ``terminal cat`` accumulates into a
        SINGLE bucket at count=2, crossing the nudge threshold. Before
        the fix these were ``tool.read_file.not_found`` (count=1) +
        ``tool.terminal.not_found`` (count=1) — silent."""
        read_result = json.dumps(
            {"success": False, "error": "no such file or directory"}
        )
        cat_result = json.dumps(
            {"exit_code": 1, "stderr": "cat: /tmp/missing.txt: No such file or directory"}
        )

        _on_post_tool_call(
            tool_name="read_file", args={}, result=read_result, session_id="s"
        )
        _on_post_tool_call(
            tool_name="terminal", args={}, result=cat_result, session_id="s"
        )

        # Single shared bucket, count == 2 (was 2 separate buckets at 1).
        assert "cat.file_io.not_found" in _pattern_state["s"]
        assert len(_pattern_state["s"]["cat.file_io.not_found"]) == 2

        # And the nudge fires on this 2nd call (was: silent before fix).
        out = _on_pre_llm_call(session_id="s")
        assert out is not None
        assert "cat.file_io.not_found" in out["context"]

    def test_obsidian_view_buckets_as_file_io(self):
        """Vault-level not_found should also bucket as file_io so a
        not_found mix of read_file + obsidian_view still counts together."""
        result = json.dumps(
            {"success": False, "error": "no such file or directory"}
        )
        _on_post_tool_call(
            tool_name="read_file", args={}, result=result, session_id="s"
        )
        _on_post_tool_call(
            tool_name="obsidian_view", args={}, result=result, session_id="s"
        )
        assert len(_pattern_state["s"]["cat.file_io.not_found"]) == 2

    def test_net_category_isolated_from_file_io(self):
        """Don't over-merge: web_search timeouts shouldn't accumulate
        into the file-I/O bucket."""
        web_result = json.dumps({"success": False, "error": "operation timed out"})
        read_result = json.dumps(
            {"success": False, "error": "no such file or directory"}
        )
        _on_post_tool_call(
            tool_name="web_search", args={}, result=web_result, session_id="s"
        )
        _on_post_tool_call(
            tool_name="read_file", args={}, result=read_result, session_id="s"
        )
        # Different categories → still 2 separate buckets, each at 1.
        assert len(_pattern_state["s"]["cat.net.timeout"]) == 1
        assert len(_pattern_state["s"]["cat.file_io.not_found"]) == 1
