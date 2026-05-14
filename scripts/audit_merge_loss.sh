#!/usr/bin/env bash
# audit_merge_loss.sh — detect content silently dropped by an upstream merge
# ---------------------------------------------------------------------------
# Use this after every `sp_sync_upstream_release` (or any merge that pulls
# upstream into this fork) to find content the merge may have discarded
# without raising a conflict marker.
#
# Two modes:
#
#   1. SNAPSHOT MODE (original): detect LOCAL fork additions the merge
#      dropped. Diffs LOCAL_BASELINE^..LOCAL_BASELINE against
#      LOCAL_BASELINE..HEAD and reports lines added by the local snapshot
#      that are missing from HEAD.
#
#        scripts/audit_merge_loss.sh [LOCAL_BASELINE]
#
#      LOCAL_BASELINE defaults to the most recent commit whose subject
#      contains "snapshot:" — adjust if you use a different convention.
#
#   2. UPSTREAM MODE (new, post v0.13.0): detect UPSTREAM code we should
#      have inherited but lost. Per-Python-file function-level diff
#      between HEAD and an upstream tag. Catches Mode D silent losses
#      (e.g. v0.13.0's `is_truthy_value` wrap and `clear_session` wake-up
#      that vanished under "take local" conflict resolution).
#
#        scripts/audit_merge_loss.sh --upstream <REF> [paths...]
#
#      <REF> is a tag/SHA of the upstream snapshot to compare against
#      (e.g. v2026.5.7). [paths...] limits the audit to specific files
#      or directories; defaults to every .py file changed between
#      merge-base(REF, HEAD) and HEAD.
#
# Exit codes (both modes):
#   0  — no code-bearing losses found
#   1  — at least one file has code-bearing losses (review manually!)
#   2  — bad invocation (no baseline resolvable)

set -euo pipefail

# -------------------------------------------------------------------------
# Mode dispatch
# -------------------------------------------------------------------------
mode="snapshot"
upstream_ref=""
audit_paths=()

# Parse flags. Anything after --upstream <REF> is an audit path filter.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --upstream)
      mode="upstream"
      shift
      if [[ $# -eq 0 ]]; then
        echo "error: --upstream requires a ref argument" >&2
        exit 2
      fi
      upstream_ref="$1"
      shift
      ;;
    -h|--help)
      sed -n '1,40p' "$0"
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do audit_paths+=("$1"); shift; done
      ;;
    *)
      if [[ "$mode" == "snapshot" ]]; then
        # Snapshot mode takes a single positional arg (LOCAL_BASELINE).
        baseline="$1"
        shift
      else
        audit_paths+=("$1")
        shift
      fi
      ;;
  esac
done

# -------------------------------------------------------------------------
# UPSTREAM MODE — function-level diff between HEAD and an upstream ref
# -------------------------------------------------------------------------
if [[ "$mode" == "upstream" ]]; then
  if ! git cat-file -e "$upstream_ref^{commit}" 2>/dev/null; then
    echo "error: cannot resolve upstream ref '$upstream_ref'" >&2
    exit 2
  fi

  echo "▶ Function-level audit: HEAD vs $upstream_ref"
  echo "  (looking for functions/symbols that were in upstream but not in HEAD)"
  echo ""

  # Collect candidate files: every .py file that changed between
  # merge-base(upstream_ref, HEAD) and HEAD, optionally filtered by
  # caller-supplied paths.
  if (( ${#audit_paths[@]} > 0 )); then
    files=()
    while IFS= read -r line; do
      [[ -n "$line" ]] && files+=("$line")
    done < <(git ls-tree -r --name-only HEAD -- "${audit_paths[@]}" 2>/dev/null \
      | grep -E '\.py$' || true)
  else
    base=$(git merge-base "$upstream_ref" HEAD 2>/dev/null || true)
    if [[ -z "$base" ]]; then
      echo "error: cannot find merge-base between $upstream_ref and HEAD" >&2
      exit 2
    fi
    files=()
    while IFS= read -r line; do
      [[ -n "$line" ]] && files+=("$line")
    done < <(git diff --diff-filter=AM --name-only "$base..HEAD" -- '*.py' 2>/dev/null)
  fi

  if (( ${#files[@]} == 0 )); then
    echo "✓ No .py files to audit (filter matched nothing)."
    exit 0
  fi

  upstream_risky=()
  for f in "${files[@]}"; do
    [[ -z "$f" ]] && continue

    # File missing in upstream is fine (local-only file).
    if ! git cat-file -e "$upstream_ref:$f" 2>/dev/null; then
      continue
    fi
    # File missing in HEAD = removed locally; flag as risky.
    if ! git cat-file -e "HEAD:$f" 2>/dev/null; then
      echo "✗ FILE MISSING IN HEAD (present in $upstream_ref): $f"
      upstream_risky+=("$f")
      continue
    fi

    # Extract def/class signatures (name only, drop line numbers and
    # arg lists so reorderings don't cause false hits).
    git show "HEAD:$f" 2>/dev/null \
      | awk '/^[[:space:]]*(def|class|async def)[[:space:]]+[A-Za-z_]/ {
          sub(/^[[:space:]]+/, "");
          sub(/[(:].*$/, "");
          print
        }' | sort -u > /tmp/_head_syms
    git show "$upstream_ref:$f" 2>/dev/null \
      | awk '/^[[:space:]]*(def|class|async def)[[:space:]]+[A-Za-z_]/ {
          sub(/^[[:space:]]+/, "");
          sub(/[(:].*$/, "");
          print
        }' | sort -u > /tmp/_up_syms

    # Symbols in upstream not in HEAD — strong silent-loss signal.
    comm -23 /tmp/_up_syms /tmp/_head_syms > /tmp/_missing_syms
    missing_count=$(wc -l < /tmp/_missing_syms | tr -d ' ')

    # Body-level loss: lines in upstream not in HEAD, dedup, code-bearing only.
    git show "$upstream_ref:$f" 2>/dev/null | awk 'NF' | sort -u > /tmp/_up_lines
    git show "HEAD:$f" 2>/dev/null | awk 'NF' | sort -u > /tmp/_head_lines
    comm -23 /tmp/_up_lines /tmp/_head_lines > /tmp/_lost_lines
    grep -E \
      '(\bdef\b|\bclass\b|\bimport\b|\breturn\b|\bif\b|\belif\b|\belse:|\bfor\b|\bwhile\b|\btry:|\bexcept\b|\braise\b|\bwith\b|\byield\b|\bfrom\b| = |==|!=|->|@[a-z])' \
      /tmp/_lost_lines > /tmp/_lost_code 2>/dev/null || true
    lost_code_count=$(wc -l < /tmp/_lost_code | tr -d ' ')

    if (( missing_count > 0 )) || (( lost_code_count > 0 )); then
      upstream_risky+=("$f")
      printf "⚠  %s\n" "$f"
      if (( missing_count > 0 )); then
        printf "   missing symbols (in %s, not in HEAD): %d\n" "$upstream_ref" "$missing_count"
        sed 's/^/     │ /' /tmp/_missing_syms | head -20
      fi
      if (( lost_code_count > 0 )); then
        printf "   body-level code-bearing lines lost: %d\n" "$lost_code_count"
        echo "   First few:"
        head -10 /tmp/_lost_code | sed 's/^/     │ /'
      fi
      echo ""
    fi
  done

  rm -f /tmp/_head_syms /tmp/_up_syms /tmp/_missing_syms \
        /tmp/_up_lines /tmp/_head_lines /tmp/_lost_lines /tmp/_lost_code

  if (( ${#upstream_risky[@]} == 0 )); then
    echo "✓ No upstream-side losses found vs $upstream_ref"
    exit 0
  fi

  cat <<HINT

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
${#upstream_risky[@]} file(s) flagged as potential Mode D losses.

For each one, verify whether the missing symbol / line is:
  a) intentionally removed in the local fork (acceptable — note in
     v<tag>_upgrade_notes.md so future audits know),
  b) renamed but semantically present (false positive),
  c) GENUINELY missing — restore from \$upstream_ref:<file>.

The two real Mode D losses caught in v0.13.0 were:
  * tools/approval.py — \`is_truthy_value(os.getenv(...))\` YOLO wrap
  * tools/approval.py — \`clear_session()\` wake-up loop

Both produced exactly this signature: function present in HEAD, but
upstream-side body lines (calls / loops) absent.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HINT

  exit 1
fi

# -------------------------------------------------------------------------
# SNAPSHOT MODE — original line-based snapshot vs HEAD audit
# -------------------------------------------------------------------------
baseline="${baseline:-}"
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
