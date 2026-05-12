#!/usr/bin/env bash
# auto-format.sh — post_tool_call hook that reformats files the agent
# just wrote, in-place. Currently handles:
#
#   *.py            → `black --quiet`     (if installed)
#   *.yaml / *.yml  → `yamlfmt`           (if installed)
#
# Other extensions are silently passed through; add new branches as your
# tooling grows. The hook always exits 0 — formatter failures must not
# block the agent (the file is still written, just not reformatted).
#
# Wire up in ~/.hermes/config.yaml:
#   hooks:
#     post_tool_call:
#       - matcher: "write_file|patch"
#         command: "~/.hermes/agent-hooks/auto-format.sh"
#
# Note: the agent's in-context view of the file is NOT re-read
# automatically — only the on-disk content changes. Subsequent
# read_file calls will see the formatted version.

set -euo pipefail

payload="$(cat -)"
path=$(printf '%s' "$payload" | jq -r '.tool_input.path // empty')

case "$path" in
  *.py)
    if command -v black >/dev/null 2>&1; then
      black --quiet "$path" 2>/dev/null || true
    fi
    ;;
  *.yaml|*.yml)
    if command -v yamlfmt >/dev/null 2>&1; then
      yamlfmt "$path" 2>/dev/null || true
    fi
    ;;
esac

printf '{}\n'
