#!/usr/bin/env bash
# block-env-write.sh — pre_tool_call hook that blocks writes to credentials/secret
# files. Triggers on write_file / patch / terminal (shell redirections to
# .env-like targets).
#
# Wire up in ~/.hermes/config.yaml:
#   hooks:
#     pre_tool_call:
#       - matcher: "write_file|patch|terminal"
#         command: "~/.hermes/agent-hooks/block-env-write.sh"
#         timeout: 5
#
# Adjust the pattern below for project-specific secret stores.

set -euo pipefail

payload="$(cat -)"
tool=$(printf '%s' "$payload" | jq -r '.tool_name // empty')

# Single regex used for path/command matching. Covers:
#   .env / .env.local / .env.production
#   ~/.aws/credentials, ~/.ssh/id_*, ~/.netrc
#   *.pem / *.key files in any path
#   GitHub Actions secrets file
secret_re='(\.env(\.[A-Za-z0-9_-]+)?(\s|$)|\.aws/credentials|\.ssh/(id_|authorized_keys)|\.netrc(\s|$)|\.(pem|key|p12|pfx)(\s|$))'

target=""
case "$tool" in
  write_file|patch)
    target=$(printf '%s' "$payload" | jq -r '.tool_input.path // empty')
    ;;
  terminal)
    target=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty')
    ;;
  *)
    printf '{}\n'; exit 0
    ;;
esac

if [[ -n "$target" ]] && printf '%s' "$target" | grep -qE "$secret_re"; then
  reason="blocked by block-env-write.sh: refusing to touch a likely-secret file ($tool target matched secrets pattern)"
  jq --null-input --arg r "$reason" '{decision: "block", reason: $r}'
else
  printf '{}\n'
fi
