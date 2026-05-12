#!/usr/bin/env bash
# inject-cwd-context.sh — pre_llm_call hook that prepends `git status` to
# the next user turn when the working tree is dirty.
#
# Wire up in ~/.hermes/config.yaml:
#   hooks:
#     pre_llm_call:
#       - command: "~/.hermes/agent-hooks/inject-cwd-context.sh"
#
# Claude-Code's UserPromptSubmit is intentionally not a separate Hermes
# event — pre_llm_call fires at the same place and already supports
# context injection via {"context": "..."}.

set -euo pipefail

# Drain stdin so the pipe doesn't SIGPIPE the parent.
cat - >/dev/null

if status=$(git status --porcelain 2>/dev/null) && [[ -n "$status" ]]; then
  jq --null-input --arg s "$status" \
     '{context: ("Uncommitted changes in cwd:\n" + $s)}'
else
  printf '{}\n'
fi
