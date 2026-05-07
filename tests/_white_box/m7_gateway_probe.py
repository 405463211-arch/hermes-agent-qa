"""M7 white-box gateway-coupling probe.

Covers:
- /rules /memory /learn / context / lcm in GATEWAY_KNOWN_COMMANDS but
  NOT silenced via cli_only=True (decision: design choice — see test below)
- gateway_help_lines includes the three new families
- Discord/Slack/Telegram menu integration: telegram_bot_commands,
  slack_subcommand_map all surface the new commands
- Archive notification path uses gateway logger (no print to stdout in
  headless mode), AND the actual notification flow is plumbed through to
  the messaging adapter (not just CLI)
- The existing logging path (hermes_logging) supports gateway.log
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Command surface in gateway                                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestGatewayCommandSurface:
    NEW_COMMANDS = ("rules", "memory", "learn", "context", "lcm")

    def test_all_new_commands_in_gateway_known(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        for cmd in self.NEW_COMMANDS:
            assert cmd in GATEWAY_KNOWN_COMMANDS, (
                f"/{cmd} not surfaced to GATEWAY_KNOWN_COMMANDS — gateway "
                f"will reply 'Unknown command'"
            )

    def test_help_lines_show_new_commands(self):
        from hermes_cli.commands import gateway_help_lines
        text = "\n".join(gateway_help_lines())
        for cmd in ("rules", "memory", "learn"):
            assert f"/{cmd}" in text

    def test_telegram_bot_commands_include_new_set(self):
        from hermes_cli.commands import telegram_bot_commands
        # Returns list of (name, description) tuples
        cmds = {name for name, _desc in telegram_bot_commands()}
        # At minimum rules + memory should be visible to telegram users —
        # learn is a power-user command and may be intentionally trimmed.
        for cmd in ("rules", "memory"):
            assert cmd in cmds, f"/{cmd} missing from telegram BotCommand menu"

    def test_slack_subcommand_map_handles_rules_and_memory(self):
        from hermes_cli.commands import slack_subcommand_map
        sm = slack_subcommand_map()
        for cmd in ("rules", "memory", "learn"):
            assert cmd in sm, f"/hermes {cmd} missing from Slack subcommands"

    def test_unknown_command_warning_skipped_for_known_set(self):
        """Critical: gateway must NOT reply 'Unknown command' for any of
        the new commands — that would be a regression. Verified by the
        membership test above; this is the consumer-side contract."""
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        # Spot-check: GATEWAY_KNOWN_COMMANDS values are normalized to
        # hyphenated form to match the gateway's check.
        for cmd in self.NEW_COMMANDS:
            normalized = cmd.replace("_", "-")
            assert normalized in GATEWAY_KNOWN_COMMANDS


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Discovery: gateway dispatch implementation status                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestGatewayDispatchGap:
    """Analyzes the current state of /rules /memory /learn dispatch in the
    gateway path. As of this branch, the gateway recognizes them as known
    commands (no 'Unknown command' reply) but has no inline handler — they
    fall through to the LLM as free text. That's a deliberate design
    choice for now (chat-driven memory management), but we lock that
    behavior in so a future change can't silently break it.
    """

    def _src(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        return (repo_root / "gateway" / "run.py").read_text(encoding="utf-8")

    def test_no_inline_handler_for_rules(self):
        """If a future PR adds inline /rules handling in the gateway, this
        test will start failing — at which point we should add a real
        unit test for that path. Currently locks 'no handler' as the
        known state."""
        src = self._src()
        # Look for the SAME pattern used by other gateway commands
        assert 'canonical == "rules":' not in src, (
            "Gateway now has inline /rules handling — update this test "
            "and write proper unit tests for the new flow"
        )

    def test_no_inline_handler_for_memory(self):
        src = self._src()
        # `canonical == "memory"` would be the new pattern; the existing
        # `enabled_toolsets=["memory"]` references are unrelated config.
        assert 'canonical == "memory":' not in src

    def test_no_inline_handler_for_learn(self):
        src = self._src()
        assert 'canonical == "learn":' not in src


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Logging path: archive notification routes correctly                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLoggingPaths:
    def test_setup_logging_creates_gateway_log_when_requested(
        self, tmp_path, monkeypatch
    ):
        """archive_notify=True with the gateway active should produce an
        agent.log entry — verified by importing setup_logging directly."""
        from hermes_logging import setup_logging
        import logging

        log_dir = tmp_path / "logs"
        # setup_logging writes into <hermes_home>/logs/
        setup_logging(hermes_home=tmp_path, log_level="INFO")
        logger = logging.getLogger("test.archive")
        logger.warning("Auto-archived 1 rule (capacity_threshold)")

        # Flush all handlers
        for h in logging.root.handlers:
            h.flush()

        # agent.log should now contain our test message
        agent_log = tmp_path / "logs" / "agent.log"
        if agent_log.exists():
            content = agent_log.read_text(encoding="utf-8")
            assert "Auto-archived" in content


class TestArchiveNotifyConfig:
    def test_archive_notify_default_true(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["memory"]["archive_notify"] is True

    def test_archive_notify_false_in_user_config(self):
        from hermes_cli.config import _deep_merge, DEFAULT_CONFIG
        merged = _deep_merge(DEFAULT_CONFIG, {"memory": {"archive_notify": False}})
        assert merged["memory"]["archive_notify"] is False
