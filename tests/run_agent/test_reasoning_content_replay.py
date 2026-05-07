"""Round-trip behaviour for ``reasoning_content`` on assistant messages.

Some providers operate in a "thinking mode" where the API requires that
EVERY replayed assistant turn carries a ``reasoning_content`` field —
even when that turn produced no reasoning text.  When the field is
missing on a follow-up call the provider rejects the request with::

    400 invalid_request_error: The `reasoning_content` in the thinking
    mode must be passed back to the API.

Originally only Moonshot/Kimi had this behaviour.  DeepSeek's official
API enforces the same rule for ``deepseek-reasoner`` and the new V4
family (``deepseek-v4-flash``, ``deepseek-v4-thinking``, ...) — using
those models for subagents made every multi-turn delegated task fail
on the second API call.

These tests pin the matching policy in
``AIAgent._copy_reasoning_content_for_api`` so the regression doesn't
come back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# Bypass the heavy ``AIAgent.__init__`` — the method under test only
# touches ``self.provider``, ``self.base_url``, and ``self.model`` plus
# its own helpers, so a hand-built stand-in is enough and far faster
# than constructing a real agent.
from run_agent import AIAgent


def _make_stub(provider: str, base_url: str, model: str) -> AIAgent:
    stub = AIAgent.__new__(AIAgent)
    stub.provider = provider
    stub.base_url = base_url
    stub.model = model
    return stub


# ---------------------------------------------------------------------
# DeepSeek thinking-mode detector
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "provider, base_url, model, expected",
    [
        # Thinking-mode models on DeepSeek's official endpoint
        ("custom", "https://api.deepseek.com", "deepseek-v4-flash", True),
        ("custom", "https://api.deepseek.com", "deepseek-v4-thinking", True),
        ("custom", "https://api.deepseek.com", "deepseek-reasoner", True),
        ("custom", "https://api.deepseek.com/v1", "deepseek-r1", True),
        ("deepseek", "", "deepseek-reasoner", True),
        # Same family via case-insensitive match
        ("custom", "https://api.deepseek.com", "DeepSeek-V4-Flash", True),
        # Non-thinking DeepSeek model — must NOT be flagged
        ("custom", "https://api.deepseek.com", "deepseek-chat", False),
        ("deepseek", "", "deepseek-chat", False),
        ("deepseek", "", "deepseek-coder", False),
        # Different provider — must NOT be flagged even if the model
        # name happens to contain "reasoner" / "v4" / etc.
        ("openai", "https://api.openai.com/v1", "gpt-4-reasoner", False),
        ("custom", "https://api.openai.com/v1", "deepseek-v4-flash", False),
        # Empty / unknown
        ("custom", "", "", False),
        ("custom", "https://api.deepseek.com", "", False),
    ],
)
def test_deepseek_thinking_protocol_detector(provider, base_url, model, expected):
    stub = _make_stub(provider, base_url, model)
    assert stub._deepseek_thinking_protocol_required() is expected


# ---------------------------------------------------------------------
# _copy_reasoning_content_for_api: explicit reasoning is always copied
# ---------------------------------------------------------------------

def test_explicit_reasoning_content_propagates_for_any_provider():
    stub = _make_stub("openai", "https://api.openai.com/v1", "gpt-4o")
    src = {
        "role": "assistant",
        "content": "ok",
        "reasoning_content": "thinking out loud",
    }
    api = src.copy()
    stub._copy_reasoning_content_for_api(src, api)
    assert api["reasoning_content"] == "thinking out loud"


def test_normalized_reasoning_field_falls_through_when_explicit_missing():
    stub = _make_stub("openai", "https://api.openai.com/v1", "gpt-4o")
    src = {
        "role": "assistant",
        "content": "ok",
        "reasoning": "the thinking",
    }
    api = src.copy()
    stub._copy_reasoning_content_for_api(src, api)
    assert api["reasoning_content"] == "the thinking"


# ---------------------------------------------------------------------
# Backfill: when the streamer captured no reasoning, thinking-mode
# providers MUST still see the field on tool-call / content messages.
# ---------------------------------------------------------------------

def test_deepseek_thinking_model_backfills_empty_reasoning_on_tool_call():
    stub = _make_stub("custom", "https://api.deepseek.com", "deepseek-v4-flash")
    src = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "type": "function",
                        "function": {"name": "ls", "arguments": "{}"}}],
    }
    api = src.copy()
    stub._copy_reasoning_content_for_api(src, api)
    assert "reasoning_content" in api
    assert api["reasoning_content"] == ""


def test_deepseek_thinking_model_backfills_empty_reasoning_on_text_reply():
    stub = _make_stub("custom", "https://api.deepseek.com", "deepseek-reasoner")
    src = {"role": "assistant", "content": "hello"}
    api = src.copy()
    stub._copy_reasoning_content_for_api(src, api)
    assert api.get("reasoning_content") == ""


def test_deepseek_chat_model_does_not_backfill_reasoning():
    """Regression guard: deepseek-chat is V3, no thinking, no protocol."""
    stub = _make_stub("custom", "https://api.deepseek.com", "deepseek-chat")
    src = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "type": "function",
                        "function": {"name": "ls", "arguments": "{}"}}],
    }
    api = src.copy()
    stub._copy_reasoning_content_for_api(src, api)
    assert "reasoning_content" not in api


def test_kimi_still_backfills_after_refactor():
    """The original Kimi/Moonshot behaviour must keep working."""
    for provider, base_url in [
        ("kimi-coding", ""),
        ("kimi-coding-cn", ""),
        ("custom", "https://api.kimi.com/v1"),
        ("custom", "https://api.moonshot.ai"),
        ("custom", "https://api.moonshot.cn"),
    ]:
        stub = _make_stub(provider, base_url, "moonshot-v1-32k")
        src = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "x", "type": "function",
                            "function": {"name": "ls", "arguments": "{}"}}],
        }
        api = src.copy()
        stub._copy_reasoning_content_for_api(src, api)
        assert api.get("reasoning_content") == "", (
            f"Kimi/Moonshot backfill regressed for "
            f"provider={provider!r} base_url={base_url!r}"
        )


# ---------------------------------------------------------------------
# Non-assistant messages must never grow a reasoning_content field
# ---------------------------------------------------------------------

@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_non_assistant_messages_never_get_reasoning_content(role):
    stub = _make_stub("custom", "https://api.deepseek.com", "deepseek-v4-flash")
    src = {"role": role, "content": "hi"}
    api = src.copy()
    stub._copy_reasoning_content_for_api(src, api)
    assert "reasoning_content" not in api
