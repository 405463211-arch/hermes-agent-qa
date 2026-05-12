#!/usr/bin/env bash
# auto-stage-on-write.sh — post_tool_call hook that `git add`s any file the
# agent just wrote, so the diff is ready for review at the end of the session.
#
# Wire up in ~/.hermes/config.yaml:
#   hooks:
#     post_tool_call:
#       - matcher: "write_file|patch"
#         command: "~/.hermes/agent-hooks/auto-stage-on-write.sh"
#         timeout: 10
#
# Observer-only: the hook never blocks or transforms tool results. If you'd
# rather see type/lint errors injected back to the agent, pair this with a
# pre_llm_call hook that runs `git diff --cached | <checker>` and emits
# {"context": "..."} — Hermes only honours context injection on pre_llm_call.

set -euo pipefail

payload="$(cat -)"
path=$(printf '%s' "$payload" | jq -r '.tool_input.path // empty')

if [[ -z "$path" || ! -e "$path" ]]; then
  printf '{}\n'; exit 0
fi

# Resolve to absolute path so the `git -C` toplevel detection works no matter
# where Hermes was launched from.
abs=$(cd "$(dirname "$path")" 2>/dev/null && pwd)/$(basename "$path")
repo=$(git -C "$(dirname "$abs")" rev-parse --show-toplevel 2>/dev/null || true)

if [[ -n "$repo" ]]; then
  git -C "$repo" add -- "$abs" 2>/dev/null || true
fi

printf '{}\n'
