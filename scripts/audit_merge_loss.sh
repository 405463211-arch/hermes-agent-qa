#!/usr/bin/env bash
# audit_merge_loss.sh — detect content silently dropped by an upstream merge
# ---------------------------------------------------------------------------
# Use this after every `sp_sync_upstream_release` (or any merge that pulls
# upstream into this fork) to find local additions the merge may have
# discarded without raising a conflict marker.
#
# How it works:
#   For each file the LOCAL_BASELINE commit modified (vs its parent), we
#   compute:
#     * snapshot_adds  = lines added by LOCAL_BASELINE   (your fork's deltas)
#     * merge_dels     = lines removed between LOCAL_BASELINE..HEAD
#     * lost           = snapshot_adds ∩ merge_dels   (content present in
#                        the snapshot but absent in HEAD)
#   Then we split `lost` into:
#     * code-bearing lines (contain Python keywords / operators / decorators)
#     * docstring/comment-only lines (likely false positives — upstream
#       rewrote the prose)
#
# The 0-deletion case is silent. Files with code-bearing losses get listed
# with a heads-up so you can grep the symbols in HEAD to confirm they're
# still there in some form.
#
# Usage:
#   scripts/audit_merge_loss.sh [LOCAL_BASELINE]
#
#   LOCAL_BASELINE defaults to the most recent commit whose subject
#   contains "snapshot:" — adjust if you use a different convention.
#
# Exit codes:
#   0  — no code-bearing losses found
#   1  — at least one file has code-bearing losses (review manually!)
#   2  — bad invocation (no baseline resolvable)

set -euo pipefail

baseline="${1:-}"
if [[ -z "$baseline" ]]; then
  baseline=$(git log --grep='snapshot:' --format='%H' -1 || true)
fi
if [[ -z "$baseline" ]] || ! git cat-file -e "$baseline^{commit}" 2>/dev/null; then
  echo "error: cannot resolve LOCAL_BASELINE; pass a commit-ish as \$1" >&2
  exit 2
fi

echo "▶ Auditing $(git rev-parse --short "$baseline") .. $(git rev-parse --short HEAD)"
echo "  for content silently dropped between baseline and current HEAD"
echo ""

# Collect all files the baseline changed (added or modified) in code/test/config.
# Use a while-read loop so this works on macOS bash 3.2 (no mapfile).
files=()
while IFS= read -r line; do
  [[ -n "$line" ]] && files+=("$line")
done < <(git diff --diff-filter=AM --name-only "${baseline}^..${baseline}" -- \
  '*.py' '*.yml' '*.yaml' '*.sh' '*.json' '*.toml' '*.cfg' 2>/dev/null)

risky=()
for f in "${files[@]}"; do
  [[ -z "$f" ]] && continue

  # File missing entirely in HEAD?
  if ! git cat-file -e "HEAD:$f" 2>/dev/null; then
    echo "✗ FILE MISSING IN HEAD: $f"
    risky+=("$f")
    continue
  fi

  # snapshot_adds and merge_dels (non-empty content lines, deduplicated)
  git diff "${baseline}^..${baseline}" -- "$f" 2>/dev/null \
    | awk '/^\+[^+]/ {print substr($0,2)}' | awk 'NF' | sort -u > /tmp/_snap_adds
  git diff "${baseline}..HEAD" -- "$f" 2>/dev/null \
    | awk '/^-[^-]/ {print substr($0,2)}' | awk 'NF' | sort -u > /tmp/_merge_dels

  comm -12 /tmp/_snap_adds /tmp/_merge_dels > /tmp/_lost

  # Split lost lines: code-bearing vs docstring/comment-only
  grep -E \
    '(\bdef\b|\bclass\b|\bimport\b|\breturn\b|\bif\b|\belif\b|\belse:|\bfor\b|\bwhile\b|\btry:|\bexcept\b|\braise\b|\bwith\b|\byield\b|\bfrom\b| = |==|!=|->|@[a-z])' \
    /tmp/_lost > /tmp/_lost_code 2>/dev/null || true

  lost_code=$(wc -l < /tmp/_lost_code | tr -d ' ')
  lost_total=$(wc -l < /tmp/_lost | tr -d ' ')
  lost_docs=$((lost_total - lost_code))

  if (( lost_code > 0 )); then
    risky+=("$f")
    printf "⚠  %s\n" "$f"
    printf "   code-bearing lines lost: %d (review!)  docstring/comment lines lost: %d (likely OK)\n" \
      "$lost_code" "$lost_docs"
    echo "   First few code-bearing losses:"
    head -10 /tmp/_lost_code | sed 's/^/     │ /'
    echo ""
  fi
done

rm -f /tmp/_snap_adds /tmp/_merge_dels /tmp/_lost /tmp/_lost_code

if (( ${#risky[@]} == 0 )); then
  echo "✓ No code-bearing content lost between $(git rev-parse --short "$baseline") and HEAD"
  exit 0
fi

cat <<HINT

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
${#risky[@]} file(s) flagged. For each one, manually verify by grepping
the symbols in HEAD. Common false-positive patterns:

  * Bare structural lines (else:/try:/except:) shifted position only
  * Docstring rewritten in better style by upstream
  * Helper function renamed (e.g. _foo_v1 → _foo_v2) — same semantics
  * Test fixture sanitised for public repo (real path → /tmp/sample-*)

If after grepping you find a symbol genuinely missing, that's a true
silent-merge-loss bug — restore from \$LOCAL_BASELINE.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HINT

exit 1
