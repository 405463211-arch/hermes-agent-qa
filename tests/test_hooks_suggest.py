"""Unit tests for :mod:`hermes_cli.hooks_suggest`.

Two pieces in the module need hard guardrails:

1. ``fingerprint_tool_call`` — the verb extraction for terminal commands
   and the suffix extraction for write_file/patch/edit. Both feed every
   downstream decision (in-session hints and the ``suggest`` CLI), so a
   regression here silently breaks the whole feature.

2. ``best_starter_template`` — the hand-curated fingerprint → template
   mapping. New entries are easy to add wrong (e.g. mapping ``.py`` to
   the wrong template), and a wrong mapping makes the agent suggest a
   command that does the wrong thing.

LLM-rationale path (``_annotate_with_llm``) is not exercised here:
it's the network-bound part, has its own threading timeout, and is
opt-in via ``--with-llm``. The pure-Python core gets full coverage.
"""

from __future__ import annotations

import pytest

from hermes_cli.hooks_suggest import (
    CAT_HOOKABLE,
    CAT_NAVIGATION,
    CAT_OBSERVATION,
    Fingerprint,
    best_starter_template,
    fingerprint_tool_call,
)


# ── fingerprint_tool_call ─────────────────────────────────────────────────


@pytest.mark.parametrize("command,expected_detail", [
    # Single-verb tools: keep just the verb so `black a.py` and `black b.py`
    # collide into one fingerprint.
    ("black src/foo.py",                 "black"),
    ("ruff check .",                     "ruff"),
    ("rm -rf /tmp/junk",                 "rm"),
    ("grep -rn foo .",                   "grep"),
    # Multi-verb tools (git/npm/pnpm/...): keep verb + subverb so
    # `git push` and `git status` don't collide.
    ("git status --porcelain",           "git status"),
    ("git push origin main --force",     "git push"),
    ("pnpm install",                     "pnpm install"),
    ("npm run build",                    "npm run"),
    ("docker compose up",                "docker compose"),
    # Path-prefixed binaries are normalized down to basename.
    ("/usr/local/bin/black foo.py",      "black"),
])
def test_fingerprint_terminal_captures_verb(command, expected_detail):
    fp = fingerprint_tool_call("terminal", {"command": command})
    assert fp.tool == "terminal"
    assert fp.detail == expected_detail


def test_fingerprint_terminal_empty_command():
    fp = fingerprint_tool_call("terminal", {})
    assert fp.tool == "terminal"
    # No verb → not hookable
    assert fp.category != CAT_HOOKABLE


@pytest.mark.parametrize("path,expected_suffix", [
    ("foo.py",         ".py"),
    ("foo.yaml",       ".yaml"),
    ("foo.yml",        ".yml"),
    ("dir/sub/x.ts",   ".ts"),
    ("Makefile",       "(no-ext)"),
    ("",               "(no-ext)"),
])
def test_fingerprint_write_uses_suffix(path, expected_suffix):
    fp = fingerprint_tool_call("write_file", {"path": path})
    assert fp.tool == "write_file"
    assert fp.detail == expected_suffix
    assert fp.suggested_event == "post_tool_call"


def test_fingerprint_patch_and_edit_same_dispatch_as_write():
    a = fingerprint_tool_call("patch", {"path": "a.py"})
    b = fingerprint_tool_call("edit",  {"path": "a.py"})
    assert a.detail == ".py" and b.detail == ".py"
    assert a.suggested_event == "post_tool_call"
    assert b.suggested_event == "post_tool_call"


def test_fingerprint_unknown_tool_falls_back():
    fp = fingerprint_tool_call("totally_made_up_tool", {})
    assert fp.tool == "totally_made_up_tool"
    assert fp.suggested_event == "pre_tool_call"


def test_fingerprint_observation_tool_categorized():
    fp = fingerprint_tool_call("read_file", {"path": "x.py"})
    # read_file is in the OBSERVATION list — should reflect that
    assert fp.category == CAT_OBSERVATION


# ── best_starter_template ─────────────────────────────────────────────────


@pytest.mark.parametrize("tool,detail,expected", [
    # write_file family → auto-format only for python / yaml
    ("write_file", ".py",      "auto-format"),
    ("write_file", ".yaml",    "auto-format"),
    ("write_file", ".yml",     "auto-format"),
    ("write_file", ".ts",      None),
    ("write_file", ".md",      None),
    ("write_file", "(no-ext)", None),
    ("patch",      ".py",      "auto-format"),
    ("edit",       ".py",      "auto-format"),

    # terminal family → distinct templates by verb
    ("terminal", "black",      "auto-format"),
    ("terminal", "ruff",       "auto-format"),
    ("terminal", "rm",         "block-rm-rf"),
    ("terminal", "git push",   "block-force-push-main"),
    ("terminal", "git status", None),
    ("terminal", "grep",       None),
    ("terminal", "pnpm",       None),
    ("terminal", "",           None),  # no verb

    # Anything else
    ("read_file",         "",   None),
    ("delegate_task",     "",   None),
])
def test_best_starter_template(tool, detail, expected):
    fp = Fingerprint(
        tool=tool, detail=detail,
        suggested_event="pre_tool_call", suggested_matcher=tool,
        category=CAT_HOOKABLE,
    )
    assert best_starter_template(fp) == expected
