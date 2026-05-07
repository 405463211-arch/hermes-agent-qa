"""Per-category breakdown of context-window usage.

Powers the ``/context`` slash command: instead of just reporting a total
"X / Y tokens used", it estimates how many tokens each conceptual bucket
(system prompt, tool schemas, MCP tools, custom agents, memory files,
skills, messages, free space, autocompact buffer) is consuming.

Counting is intentionally cheap and offline — no API calls, no
provider-specific tokenizers. We use ``estimate_tokens_rough`` (chars/4),
the same heuristic that drives Hermes's preflight compression checks
(``estimate_request_tokens_rough`` in ``agent.model_metadata``). This
keeps the breakdown sums comparable to the agent's own internal accounting.

The function is duck-typed against ``AIAgent`` so it can be unit-tested
without spinning up a real model client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.model_metadata import estimate_tokens_rough


# Category keys / display order used by both the breakdown computation
# and the renderer. Keep in sync with `_DEFAULT_CATEGORY_LABELS` below.
CATEGORY_ORDER = [
    "system_prompt",
    "system_tools",
    "mcp_tools",
    "custom_agents",
    "memory_files",
    "skills",
    "messages",
    "free_space",
    "autocompact_buffer",
]


_DEFAULT_CATEGORY_LABELS: Dict[str, str] = {
    "system_prompt": "System prompt",
    "system_tools": "System tools",
    "mcp_tools": "MCP tools",
    "custom_agents": "Custom agents",
    "memory_files": "Memory files",
    "skills": "Skills",
    "messages": "Messages",
    "free_space": "Free space",
    "autocompact_buffer": "Autocompact buffer",
}


# 中文 label —— 仅用于 CLI legend 双语显示，不进入 serialize_breakdown 的 JSON 输出
_DEFAULT_CATEGORY_LABELS_ZH: Dict[str, str] = {
    "system_prompt":      "系统提示词",
    "system_tools":       "内置工具",
    "mcp_tools":          "MCP 工具",
    "custom_agents":      "子代理",
    "memory_files":       "记忆文件",
    "skills":             "技能",
    "messages":           "对话消息",
    "free_space":         "剩余空间",
    "autocompact_buffer": "压缩缓冲区",
}


# Color names compatible with rich.style.Style — kept here so renderers
# can theme each category without re-deriving the mapping.
_DEFAULT_CATEGORY_COLORS: Dict[str, str] = {
    "system_prompt": "cyan",
    "system_tools": "magenta",
    "mcp_tools": "blue",
    "custom_agents": "yellow",
    "memory_files": "green",
    "skills": "bright_yellow",
    "messages": "bright_magenta",
    "free_space": "grey50",
    "autocompact_buffer": "red",
}


@dataclass
class CategoryStat:
    """Single category in the breakdown."""

    key: str
    label: str
    tokens: int
    color: str = ""
    label_zh: str = ""   # 中文标签，CLI 双语显示用，serialize 时不输出

    def percent_of(self, total: int) -> float:
        if total <= 0:
            return 0.0
        return min(100.0, (self.tokens / total) * 100.0)


@dataclass
class ContextBreakdown:
    """Snapshot of per-category context usage at a point in time."""

    model: str
    context_length: int
    last_prompt_tokens: int
    estimated_total_tokens: int
    threshold_tokens: int
    compression_enabled: bool
    categories: List[CategoryStat] = field(default_factory=list)

    @property
    def used_tokens(self) -> int:
        """Most-accurate "current usage" available.

        Prefer the provider-reported ``last_prompt_tokens`` when we have
        one; fall back to our offline estimate before the first API call.
        """
        return self.last_prompt_tokens or self.estimated_total_tokens

    def category(self, key: str) -> Optional[CategoryStat]:
        for cat in self.categories:
            if cat.key == key:
                return cat
        return None


# ---------------------------------------------------------------------------
# Tool schema partitioning
# ---------------------------------------------------------------------------

def _partition_tool_schemas(
    tools: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Split tool schemas into system / mcp / custom_agents buckets.

    MCP tools are identified by the ``mcp_`` prefix (the convention used
    by ``tools/mcp_tool.py`` when registering dynamic MCP tools — toolset
    ``mcp-<server>`` and tool name ``mcp_<server>_<tool>``).

    Custom-agent tools come from the ``delegation`` toolset (``delegate_task``
    today, plus any future subagent-style tools). We look up the toolset
    via the registry; when the registry can't tell us, we fall back to
    name-based detection so the breakdown still works for tools loaded
    by plugins or the context engine.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "system_tools": [],
        "mcp_tools": [],
        "custom_agents": [],
    }
    if not tools:
        return buckets

    # Lazy import — keeps this module importable in tests that haven't
    # set up the full tool registry yet.
    try:
        from tools.registry import registry as _registry
    except Exception:
        _registry = None  # type: ignore[assignment]

    for tool in tools:
        name = ""
        try:
            name = tool.get("function", {}).get("name", "") or ""
        except AttributeError:
            continue
        if not name:
            continue

        toolset = ""
        if _registry is not None:
            try:
                toolset = _registry.get_toolset_for_tool(name) or ""
            except Exception:
                toolset = ""

        if toolset == "delegation" or name == "delegate_task":
            buckets["custom_agents"].append(tool)
        elif toolset.startswith("mcp-") or name.startswith("mcp_"):
            buckets["mcp_tools"].append(tool)
        else:
            buckets["system_tools"].append(tool)

    return buckets


def _count_tools_tokens(tools: List[Dict[str, Any]]) -> int:
    """Approximate tokens consumed by a list of tool schemas.

    Mirrors the shape ``estimate_request_tokens_rough`` uses for the
    ``tools=`` request parameter (``str(tools)``).
    """
    if not tools:
        return 0
    return estimate_tokens_rough(str(tools))


# ---------------------------------------------------------------------------
# System-prompt section sizing
# ---------------------------------------------------------------------------

def _safe_call(fn, *args, **kwargs) -> str:
    """Run a prompt-builder helper, returning ``""`` on any failure.

    The helpers themselves are best-effort — failure here just means
    that section gets attributed 0 tokens, never breaks ``/context``.
    """
    try:
        out = fn(*args, **kwargs)
        return out or ""
    except Exception:
        return ""


def _section_sizes(agent: Any) -> Dict[str, int]:
    """Estimate tokens for the three system-prompt subsections we surface.

    Returns a dict with keys ``memory_files``, ``skills``, and
    ``system_prompt_remainder`` — the latter is everything else in the
    cached system prompt (identity, guidance, context files, env hints,
    timestamp, etc.) so the buckets always sum to the full system prompt.
    """
    sizes = {"memory_files": 0, "skills": 0, "system_prompt_remainder": 0}

    cached = getattr(agent, "_cached_system_prompt", "") or ""
    full_tokens = estimate_tokens_rough(cached)

    # ── Memory files ──────────────────────────────────────────────────
    memory_chars = 0
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        # rules block — included even when memory/user are disabled, since
        # rules have their own enable flag and ride the same store.
        if getattr(agent, "_rules_enabled", False):
            memory_chars += len(_safe_call(store.format_for_system_prompt, "rules"))
        if getattr(agent, "_memory_enabled", False):
            memory_chars += len(_safe_call(store.format_for_system_prompt, "memory"))
        if getattr(agent, "_user_profile_enabled", False):
            memory_chars += len(_safe_call(store.format_for_system_prompt, "user"))

    manager = getattr(agent, "_memory_manager", None)
    if manager is not None:
        memory_chars += len(_safe_call(manager.build_system_prompt))

    sizes["memory_files"] = (memory_chars + 3) // 4 if memory_chars else 0

    # ── Skills (only the in-system index built by build_skills_system_prompt) ──
    valid_tool_names = getattr(agent, "valid_tool_names", set()) or set()
    has_skills_tools = any(
        n in valid_tool_names for n in ("skills_list", "skill_view", "skill_manage")
    )
    if has_skills_tools:
        try:
            from agent.prompt_builder import build_skills_system_prompt
            try:
                from model_tools import get_toolset_for_tool as _get_toolset
            except Exception:
                _get_toolset = lambda _name: None  # noqa: E731

            avail_toolsets = {
                ts for ts in (_get_toolset(n) for n in valid_tool_names) if ts
            }
            skills_text = _safe_call(
                build_skills_system_prompt,
                available_tools=valid_tool_names,
                available_toolsets=avail_toolsets,
            )
            sizes["skills"] = estimate_tokens_rough(skills_text)
        except Exception:
            sizes["skills"] = 0

    # ── Project knowledge index (folded into "skills" bucket so the /context
    # display stays manageable — both are reference indices the model uses
    # the same way).  Skipped when the PK tools aren't loaded or the
    # directory is empty.
    if "project_knowledge_search" in valid_tool_names:
        try:
            from agent.project_knowledge import build_index, render_index_block
            pk_index = build_index()
            pk_text = render_index_block(pk_index)
            sizes["skills"] += estimate_tokens_rough(pk_text)
        except Exception:
            pass

    # ── Remainder = full cached system prompt − attributed subsections ──
    attributed = sizes["memory_files"] + sizes["skills"]
    sizes["system_prompt_remainder"] = max(0, full_tokens - attributed)

    return sizes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_context_breakdown(
    agent: Any,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    *,
    labels: Optional[Dict[str, str]] = None,
    labels_zh: Optional[Dict[str, str]] = None,
    colors: Optional[Dict[str, str]] = None,
) -> ContextBreakdown:
    """Build a :class:`ContextBreakdown` for the given live agent.

    ``conversation_history`` is the CLI-side message list (what the user
    sees in ``/history``). It's accepted explicitly so the same breakdown
    can be computed from gateway state or tests without mutating
    ``agent.messages``.
    """
    labels = {**_DEFAULT_CATEGORY_LABELS, **(labels or {})}
    labels_zh = {**_DEFAULT_CATEGORY_LABELS_ZH, **(labels_zh or {})}
    colors = {**_DEFAULT_CATEGORY_COLORS, **(colors or {})}

    # ── Pull window / threshold info from the compressor ─────────────
    compressor = getattr(agent, "context_compressor", None)
    context_length = getattr(compressor, "context_length", 0) or 0
    threshold_tokens = getattr(compressor, "threshold_tokens", 0) or 0
    last_prompt_tokens = getattr(compressor, "last_prompt_tokens", 0) or 0
    compression_enabled = bool(getattr(agent, "compression_enabled", True))

    # Autocompact band: the headroom between the threshold and the full
    # window. When compression is disabled we report 0 — there's no
    # automatic boundary to visualize.
    if compression_enabled and 0 < threshold_tokens < context_length:
        autocompact_band = context_length - threshold_tokens
    else:
        autocompact_band = 0

    # ── Tool schemas ──────────────────────────────────────────────────
    tools = getattr(agent, "tools", []) or []
    buckets = _partition_tool_schemas(tools)
    system_tools_tokens = _count_tools_tokens(buckets["system_tools"])
    mcp_tools_tokens = _count_tools_tokens(buckets["mcp_tools"])
    custom_agents_tokens = _count_tools_tokens(buckets["custom_agents"])

    # ── System-prompt subsections ─────────────────────────────────────
    sizes = _section_sizes(agent)
    system_prompt_tokens = sizes["system_prompt_remainder"]
    memory_files_tokens = sizes["memory_files"]
    skills_tokens = sizes["skills"]

    # ── Messages (rough, mirrors estimate_messages_tokens_rough) ──────
    history = conversation_history if conversation_history is not None else (
        getattr(agent, "messages", []) or []
    )
    if history:
        msg_chars = sum(len(str(m)) for m in history)
        messages_tokens = (msg_chars + 3) // 4
    else:
        messages_tokens = 0

    # ── Compose categories in display order ───────────────────────────
    estimated_total = (
        system_prompt_tokens
        + system_tools_tokens
        + mcp_tools_tokens
        + custom_agents_tokens
        + memory_files_tokens
        + skills_tokens
        + messages_tokens
    )

    # Free space = everything not attributed and not reserved for autocompact.
    used_for_layout = estimated_total + autocompact_band
    free_space_tokens = max(0, context_length - used_for_layout)

    raw_values = {
        "system_prompt": system_prompt_tokens,
        "system_tools": system_tools_tokens,
        "mcp_tools": mcp_tools_tokens,
        "custom_agents": custom_agents_tokens,
        "memory_files": memory_files_tokens,
        "skills": skills_tokens,
        "messages": messages_tokens,
        "free_space": free_space_tokens,
        "autocompact_buffer": autocompact_band,
    }

    categories = [
        CategoryStat(
            key=key,
            label=labels[key],
            tokens=raw_values[key],
            color=colors[key],
            label_zh=labels_zh[key],
        )
        for key in CATEGORY_ORDER
    ]

    return ContextBreakdown(
        model=getattr(agent, "model", "") or "",
        context_length=context_length,
        last_prompt_tokens=last_prompt_tokens,
        estimated_total_tokens=estimated_total,
        threshold_tokens=threshold_tokens,
        compression_enabled=compression_enabled,
        categories=categories,
    )


# ---------------------------------------------------------------------------
# Display helpers (used by both CLI and tests — keep simple, no Rich here)
# ---------------------------------------------------------------------------

def format_token_count(n: int) -> str:
    """Compact human-friendly token count: 53300 -> '53.3k', 1_000_000 -> '1m'."""
    if n is None:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        value = n / 1_000_000
        return f"{value:.1f}m" if value < 100 else f"{int(value)}m"
    if n >= 1_000:
        value = n / 1_000
        return f"{value:.1f}k" if value < 100 else f"{int(value)}k"
    return str(n)


def format_percent(pct: float) -> str:
    """Format a percentage with sensible precision for small slices."""
    if pct >= 10:
        return f"{pct:.0f}%"
    if pct >= 1:
        return f"{pct:.1f}%"
    if pct > 0:
        return f"{pct:.2f}%"
    return "0%"


def serialize_breakdown(breakdown: ContextBreakdown) -> Dict[str, Any]:
    """JSON-friendly view of a breakdown — handy for ``/context --json``."""
    return {
        "model": breakdown.model,
        "context_length": breakdown.context_length,
        "last_prompt_tokens": breakdown.last_prompt_tokens,
        "estimated_total_tokens": breakdown.estimated_total_tokens,
        "threshold_tokens": breakdown.threshold_tokens,
        "compression_enabled": breakdown.compression_enabled,
        "categories": [
            {
                "key": c.key,
                "label": c.label,
                "tokens": c.tokens,
                "percent": c.percent_of(breakdown.context_length),
            }
            for c in breakdown.categories
        ],
    }


def to_json(breakdown: ContextBreakdown) -> str:
    return json.dumps(serialize_breakdown(breakdown), indent=2)
