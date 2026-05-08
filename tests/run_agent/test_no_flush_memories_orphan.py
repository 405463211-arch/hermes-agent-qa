"""Anchor test: ``flush_memories`` must NOT be reintroduced as an orphan.

History
-------
- baseline ``6198fe35f`` had ``AIAgent.flush_memories`` + 3 call sites
  (``_compress_context`` / ``cli.new_session`` / CLI exit cleanup).
- Upstream PR #15696 (commit ``ea01bdceb``, 2026-04-25) deleted both the
  function body and all 3 call sites in a single architectural change,
  citing prompt-cache invalidation, blocking inference, and redundancy
  with the background memory review (``memory.nudge_interval``).
- v0.12.0 sync merge (``4f7c71c3c``, 2026-05-07) erroneously applied a
  "take local" decision in §3.6.6 of the upgrade notes — it kept the
  248-line function body but the 3 call sites were silently absorbed
  into the upstream-deletion side of the merge.  Result: orphan dead
  code that violated AGENTS.md prompt-caching policy on paper.
- Cleanup commit (2026-05-07) removed the orphan function + the
  ``memory.flush_min_turns`` config slot.

This test exists to keep that cleanup decision durable.  It guards
against three regressions:

  1. Someone reads §10 of the upgrade notes and decides to "restore"
     ``flush_memories`` without re-reading PR #15696's reasoning.
  2. A future upstream merge re-introduces the function (if upstream
     ever changes its mind, we want a deliberate decision, not a quiet
     re-merge).
  3. Anyone wires ``flush_memories`` into ``_compress_context`` /
     ``cli.py`` again, replicating the cache-breaking behaviour the
     upstream removed.

The replacement mechanism (``memory.nudge_interval``) is asserted
positively to prove there's still an in-band memory-save trigger.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. Function body must not exist (def or method)
# ---------------------------------------------------------------------------

class TestFlushMemoriesIsGone:
    def test_aiagent_has_no_flush_memories_method(self):
        """``AIAgent`` must not define a ``flush_memories`` method.

        Used to be at ``run_agent.py:9211`` between ``_should_sanitize_tool_calls``
        and ``_compact_with_progress``.  Killed in v0.12.0 cleanup.
        """
        from run_agent import AIAgent
        assert not hasattr(AIAgent, "flush_memories"), (
            "AIAgent.flush_memories was reintroduced.  This violates the "
            "AGENTS.md prompt-caching rule (it temporarily swaps the toolset "
            "to a memory-only single-tool list, breaking prefix cache).  "
            "If you genuinely need it back, read .cursor/prompts/v0.12.0_upgrade_notes.md §10 first, "
            "then update this test with the new rationale.  Replacement "
            "mechanism: memory.nudge_interval in cli-config.yaml."
        )

    def test_no_flush_memories_call_sites_in_run_agent(self):
        """``run_agent.py`` must not call ``flush_memories`` anywhere.

        baseline 6198fe35f had ``self.flush_memories(messages, min_turns=0)``
        in ``_compress_context``.  Upstream PR #15696 deleted it.
        """
        text = (REPO_ROOT / "run_agent.py").read_text(encoding="utf-8")
        # Permit zero string mentions — easier to grep, no false positives
        # since the symbol no longer exists at all.
        assert "flush_memories" not in text, (
            "run_agent.py mentions 'flush_memories'.  The function and all "
            "call sites were removed in v0.12.0 cleanup (see upgrade notes §10). "
            "If you're restoring it, this test must be updated with the new rationale."
        )

    def test_no_flush_memories_call_sites_in_cli(self):
        """``cli.py`` must not call ``flush_memories``.

        baseline 6198fe35f had two sites: ``new_session`` and CLI exit cleanup.
        Both removed by upstream PR #15696 — never reapplied.
        """
        text = (REPO_ROOT / "cli.py").read_text(encoding="utf-8")
        assert "flush_memories" not in text, (
            "cli.py mentions 'flush_memories'.  This was removed in v0.12.0 "
            "cleanup; see upgrade notes §10 for why it should stay removed."
        )

    def test_no_memory_flush_min_turns_field(self):
        """``_memory_flush_min_turns`` instance field must not exist.

        Was set in ``__init__`` from ``memory.flush_min_turns`` config.
        """
        from run_agent import AIAgent
        # Construct a minimal instance to confirm the attribute genuinely
        # isn't initialized anywhere — checking the class isn't enough
        # because attributes are set in __init__.
        sig = inspect.signature(AIAgent.__init__)
        # Just confirm the symbol doesn't appear in the source — that's
        # the contract.  Instantiating AIAgent without secrets/network is
        # fragile and not worth the test setup cost here.
        text = (REPO_ROOT / "run_agent.py").read_text(encoding="utf-8")
        assert "_memory_flush_min_turns" not in text, (
            "_memory_flush_min_turns field was reintroduced.  It paired with "
            "flush_memories — if flush_memories is gone, the field has no "
            "consumers.  See upgrade notes §10."
        )


# ---------------------------------------------------------------------------
# 2. Config slot must not exist
# ---------------------------------------------------------------------------

class TestFlushMinTurnsConfigIsGone:
    def test_cli_config_example_has_no_flush_min_turns(self):
        """``cli-config.yaml.example`` must not advertise ``memory.flush_min_turns``.

        Leaving the config slot documented while the implementation is gone
        creates a documentation-vs-reality drift bug.  Users would set the
        value, observe nothing happens, and file confused issues.
        """
        text = (REPO_ROOT / "cli-config.yaml.example").read_text(encoding="utf-8")
        assert "flush_min_turns" not in text, (
            "cli-config.yaml.example still advertises memory.flush_min_turns. "
            "The corresponding code is gone (v0.12.0 cleanup, see upgrade notes "
            "§10) — remove the config slot or wire the implementation back."
        )


# ---------------------------------------------------------------------------
# 3. Replacement mechanism must still be in place (positive assertion)
# ---------------------------------------------------------------------------

class TestReplacementMechanismIsAlive:
    def test_memory_nudge_interval_field_initialized(self):
        """``_memory_nudge_interval`` must still be initialized in __init__.

        This is the in-band per-N-turn memory-save trigger that replaced
        flush_memories' once-per-compression behaviour.  If this is gone too,
        we'd be losing memory-save coverage entirely.
        """
        text = (REPO_ROOT / "run_agent.py").read_text(encoding="utf-8")
        assert "self._memory_nudge_interval = " in text, (
            "_memory_nudge_interval is no longer initialized in __init__. "
            "This is the replacement for flush_memories — losing it means "
            "the agent has no in-band trigger to consider saving memories. "
            "Check memory.nudge_interval in cli-config.yaml.example and "
            "the init block in run_agent.py:AIAgent.__init__."
        )

    def test_nudge_interval_check_in_run_conversation(self):
        """The per-turn nudge-interval check must still fire in run_conversation.

        Baseline lives around run_agent.py:10928-10934:
            if (self._memory_nudge_interval > 0
                    and "memory" in self.valid_tool_names
                    and self._memory_store):
                self._turns_since_memory += 1
                if self._turns_since_memory >= self._memory_nudge_interval:
                    _should_review_memory = True
        """
        text = (REPO_ROOT / "run_agent.py").read_text(encoding="utf-8")
        assert "self._turns_since_memory >= self._memory_nudge_interval" in text, (
            "The per-turn nudge-interval check is gone from run_conversation. "
            "Without it the agent loses its only in-band memory-save trigger "
            "(flush_memories already removed in v0.12.0). Restore the check or "
            "explain in upgrade notes why memory saves no longer need a trigger."
        )

    def test_cli_config_example_documents_nudge_interval(self):
        """``cli-config.yaml.example`` must document ``memory.nudge_interval``."""
        text = (REPO_ROOT / "cli-config.yaml.example").read_text(encoding="utf-8")
        assert "nudge_interval:" in text, (
            "memory.nudge_interval is no longer documented in cli-config.yaml.example. "
            "Users have no way to discover/tune the replacement for flush_memories."
        )
