#!/usr/bin/env bash
# block-rm-rf.sh — pre_tool_call hook that blocks `rm -rf /` invocations.
#
# Wire up in ~/.hermes/config.yaml:
#   hooks:
#     pre_tool_call:
#       - matcher: "terminal"
#         command: "~/.hermes/agent-hooks/block-rm-rf.sh"
#         timeout: 5
#
# Returns a Claude-Code-compatible block decision. Hermes also accepts
# the canonical {"action":"block","message":"..."} shape.

set -euo pipefail

payload="$(cat -)"
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty')

# Block literal `rm -rf /` and `rm -rf /<critical>` (etc, usr, var, bin,
# sbin, lib, boot, opt, root, srv, sys, proc, dev). Leaves
# `rm -rf /tmp/...`, `rm -rf ./build`, etc. untouched — the agent often
# legitimately needs those.  Tune the pattern below for your environment.
danger='rm[[:space:]]+-[rf]+[[:space:]]+/([[:space:]]*$|(etc|usr|var|bin|sbin|lib|boot|opt|root|srv|sys|proc|dev|home)([[:space:]/]|$))'
if printf '%s' "$cmd" | grep -qE "$danger"; then
  printf '{"decision": "block", "reason": "blocked by block-rm-rf.sh: refusing rm -rf on a critical root path"}\n'
else
  printf '{}\n'
fi
