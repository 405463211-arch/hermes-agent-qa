#!/usr/bin/env bash
# block-force-push-main.sh — pre_tool_call hook that refuses `git push --force`
# (and `--force-with-lease`) against main/master/release/* branches.
#
# Wire up in ~/.hermes/config.yaml:
#   hooks:
#     pre_tool_call:
#       - matcher: "terminal"
#         command: "~/.hermes/agent-hooks/block-force-push-main.sh"
#         timeout: 5
#
# Heuristic — does not parse argv; matches the most common shapes. Tune for
# your remote names (origin/upstream/etc).

set -euo pipefail

payload="$(cat -)"
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty')

if ! printf '%s' "$cmd" | grep -qE '\bgit[[:space:]]+push\b'; then
  printf '{}\n'; exit 0
fi

# Has --force or --force-with-lease (covers -f short form too)?
if ! printf '%s' "$cmd" | grep -qE '(--force(-with-lease)?|[[:space:]]-f([[:space:]]|$))'; then
  printf '{}\n'; exit 0
fi

# Targeting a protected branch? Match either `push <remote> main` or `push <remote> HEAD:main`.
protected='(main|master|release/[A-Za-z0-9._-]+|prod|production)'
if printf '%s' "$cmd" | grep -qE "[[:space:]:](${protected})([[:space:]]|$)"; then
  reason="blocked by block-force-push-main.sh: refusing force-push to a protected branch ($protected)"
  jq --null-input --arg r "$reason" '{decision: "block", reason: $r}'
else
  printf '{}\n'
fi
