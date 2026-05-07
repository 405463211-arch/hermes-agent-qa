"""Tests for agent/context_breakdown.py — per-category context usage."""

from types import SimpleNamespace

import pytest

from agent.context_breakdown import (
    CATEGORY_ORDER,
    ContextBreakdown,
    _DEFAULT_CATEGORY_LABELS_ZH,
    _partition_tool_schemas,
    compute_context_breakdown,
    format_percent,
    format_token_count,
    serialize_breakdown,
    to_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "x" * 200}}


def _make_compressor(*, context_length=200_000, last_prompt_tokens=0,
                     threshold_pct=0.50):
    return SimpleNamespace(
        context_length=context_length,
        last_prompt_tokens=last_prompt_tokens,
        threshold_tokens=int(context_length * threshold_pct),
    )


def _make_agent(
    *,
    model="test/model",
    cached_system_prompt="",
    tools=None,
    messages=None,
    context_length=200_000,
    last_prompt_tokens=0,
    threshold_pct=0.50,
    compression_enabled=True,
    valid_tool_names=None,
    memory_store=None,
    memory_enabled=False,
    user_profile_enabled=False,
    memory_manager=None,
):
    return SimpleNamespace(
        model=model,
        _cached_system_prompt=cached_system_prompt,
        tools=tools or [],
        messages=messages or [],
        context_compressor=_make_compressor(
            context_length=context_length,
            last_prompt_tokens=last_prompt_tokens,
            threshold_pct=threshold_pct,
        ),
        compression_enabled=compression_enabled,
        valid_tool_names=valid_tool_names or set(),
        _memory_store=memory_store,
        _memory_enabled=memory_enabled,
        _user_profile_enabled=user_profile_enabled,
        _memory_manager=memory_manager,
    )


# ---------------------------------------------------------------------------
# format_token_count / format_percent
# ---------------------------------------------------------------------------

class TestFormatTokenCount:
    def test_zero(self):
        assert format_token_count(0) == "0"

    def test_under_thousand(self):
        assert format_token_count(239) == "239"
        assert format_token_count(999) == "999"

    def test_thousands(self):
        assert format_token_count(1_500) == "1.5k"
        assert format_token_count(53_300) == "53.3k"

    def test_millions(self):
        assert format_token_count(1_000_000) == "1.0m"
        assert format_token_count(2_500_000) == "2.5m"


class TestFormatPercent:
    def test_zero(self):
        assert format_percent(0) == "0%"

    def test_small(self):
        assert format_percent(0.05) == "0.05%"
        assert format_percent(0.7) == "0.70%"

    def test_under_ten(self):
        assert format_percent(2.5) == "2.5%"
        assert format_percent(5.0) == "5.0%"

    def test_above_ten(self):
        assert format_percent(91.2) == "91%"


# ---------------------------------------------------------------------------
# Tool partitioning
# ---------------------------------------------------------------------------

class TestPartitionTools:
    def test_empty(self):
        buckets = _partition_tool_schemas([])
        assert buckets == {"system_tools": [], "mcp_tools": [], "custom_agents": []}

    def test_mcp_detected_by_name(self):
        # MCP tools always start with mcp_ even when registry isn't loaded.
        tools = [_tool("mcp_github_create_issue"), _tool("file_write")]
        buckets = _partition_tool_schemas(tools)
        assert len(buckets["mcp_tools"]) == 1
        assert buckets["mcp_tools"][0]["function"]["name"] == "mcp_github_create_issue"

    def test_delegate_task_is_custom_agent(self):
        tools = [_tool("delegate_task"), _tool("file_write")]
        buckets = _partition_tool_schemas(tools)
        assert len(buckets["custom_agents"]) == 1
        assert buckets["custom_agents"][0]["function"]["name"] == "delegate_task"

    def test_unknown_tools_default_to_system(self):
        tools = [_tool("file_write"), _tool("terminal"), _tool("web_search")]
        buckets = _partition_tool_schemas(tools)
        assert len(buckets["system_tools"]) == 3
        assert buckets["mcp_tools"] == []
        assert buckets["custom_agents"] == []


# ---------------------------------------------------------------------------
# compute_context_breakdown
# ---------------------------------------------------------------------------

class TestComputeBreakdown:
    def test_basic_shape(self):
        agent = _make_agent(cached_system_prompt="hello world" * 50)
        bd = compute_context_breakdown(agent, conversation_history=[])

        assert isinstance(bd, ContextBreakdown)
        assert bd.model == "test/model"
        assert bd.context_length == 200_000
        assert {c.key for c in bd.categories} == set(CATEGORY_ORDER)
        # Categories are returned in canonical display order.
        assert [c.key for c in bd.categories] == CATEGORY_ORDER

    def test_system_prompt_attribution(self):
        # Build a system prompt of known length: 4000 chars ≈ 1000 tokens.
        text = "x" * 4000
        agent = _make_agent(cached_system_prompt=text)
        bd = compute_context_breakdown(agent, conversation_history=[])

        cat = bd.category("system_prompt")
        assert cat is not None
        # ceil-divide(4000, 4) = 1000 tokens.
        assert cat.tokens == 1000

    def test_messages_attribution(self):
        history = [
            {"role": "user", "content": "x" * 400},
            {"role": "assistant", "content": "y" * 400},
        ]
        agent = _make_agent()
        bd = compute_context_breakdown(agent, conversation_history=history)

        # Each str(msg) wraps content in dict repr — count must be > 200
        # (the bare content tokens) and well-defined.
        cat = bd.category("messages")
        assert cat is not None
        assert cat.tokens > 200

    def test_tool_attribution(self):
        agent = _make_agent(tools=[
            _tool("file_write"),
            _tool("mcp_github_get_issue"),
            _tool("delegate_task"),
        ])
        bd = compute_context_breakdown(agent, conversation_history=[])

        assert bd.category("system_tools").tokens > 0
        assert bd.category("mcp_tools").tokens > 0
        assert bd.category("custom_agents").tokens > 0

    def test_free_space_fills_to_context_length(self):
        agent = _make_agent(cached_system_prompt="x" * 4000, context_length=100_000)
        bd = compute_context_breakdown(agent, conversation_history=[])

        # Free space + autocompact + everything else should approximately
        # equal context_length. Allow ±1 token for rounding.
        total = sum(c.tokens for c in bd.categories)
        assert abs(total - bd.context_length) <= 1

    def test_autocompact_band_when_compression_enabled(self):
        # threshold 50% of 100k → autocompact band = 50k.
        agent = _make_agent(context_length=100_000, threshold_pct=0.50)
        bd = compute_context_breakdown(agent, conversation_history=[])

        assert bd.category("autocompact_buffer").tokens == 50_000

    def test_autocompact_band_zero_when_compression_disabled(self):
        agent = _make_agent(context_length=100_000, compression_enabled=False)
        bd = compute_context_breakdown(agent, conversation_history=[])

        assert bd.category("autocompact_buffer").tokens == 0

    def test_used_tokens_prefers_provider_count(self):
        agent = _make_agent(
            cached_system_prompt="x" * 1000,
            last_prompt_tokens=12_345,
        )
        bd = compute_context_breakdown(agent, conversation_history=[])
        assert bd.used_tokens == 12_345

    def test_used_tokens_falls_back_to_estimate(self):
        agent = _make_agent(cached_system_prompt="x" * 4000)
        bd = compute_context_breakdown(agent, conversation_history=[])
        # No API call made yet → fall back to local estimate (≥1000 from
        # the system prompt alone).
        assert bd.used_tokens >= 1000

    def test_handles_missing_compressor(self):
        agent = SimpleNamespace(
            model="m",
            _cached_system_prompt="",
            tools=[],
            messages=[],
            context_compressor=None,
            compression_enabled=False,
            valid_tool_names=set(),
            _memory_store=None,
            _memory_enabled=False,
            _user_profile_enabled=False,
            _memory_manager=None,
        )
        bd = compute_context_breakdown(agent, conversation_history=[])
        assert bd.context_length == 0
        assert bd.used_tokens == 0


# ---------------------------------------------------------------------------
# Memory store integration
# ---------------------------------------------------------------------------

class _FakeMemoryStore:
    """Stand-in MemoryStore that returns canned blocks."""

    def __init__(self, memory_block: str = "", user_block: str = ""):
        self._memory = memory_block
        self._user = user_block

    def format_for_system_prompt(self, kind: str) -> str:
        if kind == "memory":
            return self._memory
        if kind == "user":
            return self._user
        return ""


class TestMemoryAttribution:
    def test_memory_block_counted(self):
        store = _FakeMemoryStore(memory_block="m" * 800)  # ~200 tokens
        agent = _make_agent(
            cached_system_prompt="m" * 800,  # mirror — same content
            memory_store=store,
            memory_enabled=True,
        )
        bd = compute_context_breakdown(agent, conversation_history=[])
        mem = bd.category("memory_files")
        assert mem is not None
        assert mem.tokens == 200

    def test_user_block_only_counted_when_enabled(self):
        store = _FakeMemoryStore(memory_block="", user_block="u" * 400)
        agent = _make_agent(
            cached_system_prompt="u" * 400,
            memory_store=store,
            memory_enabled=False,
            user_profile_enabled=False,
        )
        bd = compute_context_breakdown(agent, conversation_history=[])
        assert bd.category("memory_files").tokens == 0


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class TestSerialize:
    def test_round_trip_shape(self):
        agent = _make_agent(cached_system_prompt="x" * 100)
        bd = compute_context_breakdown(agent, conversation_history=[])
        data = serialize_breakdown(bd)

        assert data["model"] == "test/model"
        assert data["context_length"] == 200_000
        assert "categories" in data
        assert {c["key"] for c in data["categories"]} == set(CATEGORY_ORDER)
        for cat in data["categories"]:
            assert {"key", "label", "tokens", "percent"} <= cat.keys()


# ---------------------------------------------------------------------------
# Registry-aware partitioning
# ---------------------------------------------------------------------------

class TestRegistryPartitioning:
    """Verify that toolset lookup wins over name-based fallback when present."""

    def test_registry_classifies_correctly(self, monkeypatch):
        # Pretend the registry says tool 'foo' belongs to toolset 'mcp-svc'
        # even though its name doesn't start with mcp_.
        from tools import registry as registry_mod

        monkeypatch.setattr(
            registry_mod.registry,
            "get_toolset_for_tool",
            lambda name: {"foo": "mcp-svc", "bar": "delegation"}.get(name, "filesystem"),
        )

        tools = [_tool("foo"), _tool("bar"), _tool("baz")]
        buckets = _partition_tool_schemas(tools)
        assert [t["function"]["name"] for t in buckets["mcp_tools"]] == ["foo"]
        assert [t["function"]["name"] for t in buckets["custom_agents"]] == ["bar"]
        assert [t["function"]["name"] for t in buckets["system_tools"]] == ["baz"]


# ---------------------------------------------------------------------------
# Bilingual labels —— /context CLI 双语显示层
# ---------------------------------------------------------------------------

class TestBilingualLabels:
    """`/context` 显示层的中文双语支持。

    JSON 输出保持纯英文，只在 CategoryStat.label_zh 中暴露中文，
    供 cli.py::_show_context 渲染时拼接显示。
    """

    def test_chinese_labels_present_for_every_category(self):
        """每个 CATEGORY_ORDER 都必须配套非空中文 label。"""
        for key in CATEGORY_ORDER:
            assert key in _DEFAULT_CATEGORY_LABELS_ZH, \
                f"缺失中文 label: {key}"
            zh = _DEFAULT_CATEGORY_LABELS_ZH[key]
            assert zh and zh.strip(), \
                f"中文 label 为空: {key} -> {zh!r}"

    def test_compute_breakdown_fills_label_zh(self):
        """compute_context_breakdown 应自动填充 CategoryStat.label_zh。"""
        agent = _make_agent()
        breakdown = compute_context_breakdown(agent)
        for cat in breakdown.categories:
            assert cat.label_zh, f"{cat.key} 未填充 label_zh"
            assert cat.label_zh == _DEFAULT_CATEGORY_LABELS_ZH[cat.key]

    def test_serialize_breakdown_keeps_english_only(self):
        """to_json 输出必须不含中文字符（脚本/测试稳定性）。"""
        agent = _make_agent()
        breakdown = compute_context_breakdown(agent)
        json_str = to_json(breakdown)
        chinese_chars = [ch for ch in json_str if "\u4e00" <= ch <= "\u9fff"]
        assert not chinese_chars, \
            f"JSON 输出意外包含中文: {chinese_chars[:5]}"
