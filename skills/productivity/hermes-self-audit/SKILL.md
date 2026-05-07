---
name: hermes-self-audit
description: Use when the user asks to "audit", "check", "review my Hermes usage", investigate why memory/learning isn't working as expected, look at recent errors, find quality issues in past sessions, or get a usage health report. Inspects logs, session DB, memory state, and learning store to surface real-world quality issues.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [audit, observability, memory, learning, quality, troubleshooting, hermes-internals]
    related_skills: [hermes-memory-guide, systematic-debugging]
---

# Hermes Self-Audit

## Overview

Hermes ships rich observability. This skill is the **operator's playbook** for actually using it: what to look at, in what order, and how to translate raw output into actionable findings.

**Core principle:** every dimension below has a "healthy looks like" + "trouble looks like" pair. Compare and report — don't dump raw data on the user.

## When to Use

- "How's my Hermes doing?" / "Audit my usage"
- "Why isn't [feature X] working as expected?"
- "What did Hermes do in session XYZ?"
- "Are my rules being respected?"
- "Is the self-learning loop firing?"
- After noticing degraded quality (slower responses, repeated mistakes, ignored preferences)
- Proactively before changing memory configuration, to establish a baseline

## Data Sources at a Glance

| Source | Path / Command | What it tells you |
|---|---|---|
| Agent log | `~/.hermes/logs/agent.log` (`hermes logs`) | INFO+ runtime — tool calls, decisions, archive notices |
| Errors log | `~/.hermes/logs/errors.log` (`hermes logs errors`) | WARNING+ — failures, retries, stack traces |
| Gateway log | `~/.hermes/logs/gateway.log` (`hermes logs gateway`) | Messaging-platform events |
| Session DB | `~/.hermes/sessions.db` (SQLite + FTS5) | Every message + tool_calls; FTS5 search |
| Memory files | `~/.hermes/memories/{RULES,MEMORY,USER}.md` + `RULES.archive.md` | Current memory state |
| Learning store | `~/.hermes/learning_store.db` | Pending + promoted patterns |
| Config | `~/.hermes/config.yaml` | What's enabled, what's tuned |

For profiles, replace `~/.hermes/` with `~/.hermes/profiles/<name>/`. Use `hermes profile list` to enumerate.

## The 6 Audit Dimensions

Always run them in this order — earlier dimensions surface issues that explain later ones.

### Dimension 1 — Memory Bucket Health

**Goal:** are the three buckets within budget and well-curated?

```bash
# Bucket sizes vs limits
hermes -c "memory show"

# Or directly:
python3 - <<'EOF'
from pathlib import Path
import os
home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
for name in ("RULES", "MEMORY", "USER"):
    p = home / "memories" / f"{name}.md"
    if p.exists():
        size = len(p.read_text())
        print(f"{name:8} {size:6,} chars  {p}")
EOF
```

**Healthy looks like:**
- All three buckets < 80% of limit
- RULES.md has both manual + LRN-* entries (diverse origin = self-learning works)
- MEMORY.md entries have hermes-meta blocks (modern format)

**Trouble looks like:**
- Any bucket > 90% — auto-archive should kick in next session; if not, check `auto_archive_rules` config
- RULES.md is 100% manual entries — self-learning loop is silent (see Dimension 4)
- RULES.md is 100% LRN-* entries — user never adds rules manually; might indicate the user doesn't know about `/rules add`
- MEMORY.md filled with no metadata — legacy entries; review and migrate

**Then check:**
```bash
# Find dormant entries
hermes -c "memory review"

# See what's been auto-archived
hermes -c "rules archive list"
```

### Dimension 2 — Configuration Consistency

**Goal:** does config.yaml reflect actual runtime behavior?

```bash
# Show effective memory config
python3 - <<'EOF'
from hermes_cli.config import load_config
cfg = load_config()
mem = cfg.get("memory", {})
plugins = cfg.get("plugins", {})
print("memory:")
for k in (
    "rules_enabled","memory_enabled","user_profile_enabled",
    "rules_char_limit","memory_char_limit","user_char_limit",
    "auto_archive_rules","auto_archive_capacity_threshold",
    "auto_archive_age_days","trial_new_marker_days","archive_notify",
):
    print(f"  {k}: {mem.get(k)!r}")
print("plugins.disabled:", plugins.get("disabled", []))
print("plugins.enabled:", plugins.get("enabled", []))
EOF
```

**Healthy looks like:**
- All `*_enabled` flags reflect what the user actually wants
- Char limits make sense for the user's typical session length
- `auto_archive_rules: true` if user has any LRN-* entries
- `self_learning` not in `plugins.disabled` (unless user opted out explicitly)

**Trouble looks like:**
- `memory_enabled: false` but MEMORY.md has many entries → silent waste
- `auto_archive_age_days: 0` but the user complains rules are stale → archiving is disabled
- `trial_new_marker_days: 0` → newly promoted rules silently locked in; user can't catch bad inferences

### Dimension 3 — Recent Error Patterns

**Goal:** are there recurring errors that should have been learned?

```bash
# Last 24h of WARNING+ errors
hermes logs errors --since 24h | tail -100

# Cluster by logger name + message stem
hermes logs errors --since 7d 2>/dev/null | \
  awk '{ for(i=4;i<=NF;i++) printf "%s ", $i; print "" }' | \
  sort | uniq -c | sort -rn | head -20
```

**Healthy looks like:**
- Errors are diverse (ad-hoc network blips, transient API issues)
- Errors taper or disappear over time
- After repeated similar errors, a `[self-learning]` nudge appears in `agent.log` and an `LRN-*` rule appears in RULES.md within a few sessions

**Trouble looks like:**
- Same `tool.X.signal` shows up 10+ times across days **without** corresponding `[self-learning]` nudge → plugin disabled, threshold too high, or session_id missing
- Same nudge fires repeatedly **without** a `learning_record` call after → agent ignoring nudges; check `MEMORY_GUIDANCE` injection
- Errors with `Operation not permitted` → permission/sandbox issues, not real bugs

### Dimension 4 — Self-Learning Loop Effectiveness

**Goal:** is the learning loop actually closing?

```bash
hermes -c "learn stats"      # if available
hermes -c "learn list"

# Or directly query the store:
sqlite3 ~/.hermes/learning_store.db <<'EOF'
.headers on
.mode column
SELECT
    state,
    COUNT(*) AS n,
    AVG(recurrence_count) AS avg_recurrence,
    MAX(recurrence_count) AS max_recurrence
FROM learnings
GROUP BY state;
EOF

# Top non-promoted patterns by recurrence
sqlite3 ~/.hermes/learning_store.db <<'EOF'
.headers on
.mode column
SELECT
    id, pattern_key, recurrence_count, distinct_tasks,
    state, summary
FROM learnings
WHERE state = 'pending'
ORDER BY recurrence_count DESC
LIMIT 10;
EOF
```

**Healthy looks like:**
- Pending entries with `recurrence_count >= 3` AND `distinct_tasks >= 2` should have promoted (state=`promoted`); if pending, either window expired or thresholds were tuned higher
- Promoted entries appear in RULES.md with matching `LRN-*` source IDs
- Resolution rate (`state=resolved`) > 0 — user actively `/learn resolve`-ing

**Trouble looks like:**
- High recurrence but state=pending → check `is_eligible_for_promotion` thresholds (config.yaml `learning.promotion.*`)
- Promoted entries NOT in RULES.md → promotion ran but write to RULES.md failed; check errors.log for the same timestamp
- Empty learning store after weeks of use → plugin disabled OR agent never calls `learning_record` (system prompt MEMORY_GUIDANCE missing the trigger language?)

### Dimension 5 — Session Quality Signals

**Goal:** are individual sessions healthy (no thrashing, no infinite loops, reasonable tool density)?

```bash
sqlite3 ~/.hermes/sessions.db <<'EOF'
.headers on
.mode column

-- Session length distribution (last 30 days)
SELECT
    CASE
        WHEN message_count < 5 THEN 'short (<5)'
        WHEN message_count < 20 THEN 'medium (5-19)'
        WHEN message_count < 50 THEN 'long (20-49)'
        ELSE 'very long (50+)'
    END AS bucket,
    COUNT(*) AS sessions,
    AVG(tool_call_count * 1.0 / NULLIF(message_count, 0)) AS avg_tools_per_msg
FROM sessions
WHERE started_at > strftime('%s', 'now', '-30 days')
GROUP BY bucket
ORDER BY MIN(message_count);

-- Top 10 tools by usage (last 7 days)
SELECT
    json_extract(value, '$.function.name') AS tool,
    COUNT(*) AS calls
FROM messages, json_each(messages.tool_calls)
WHERE messages.tool_calls IS NOT NULL
  AND messages.created_at > strftime('%s', 'now', '-7 days')
GROUP BY tool
ORDER BY calls DESC
LIMIT 10;
EOF
```

**Healthy looks like:**
- Most sessions in short/medium buckets
- avg_tools_per_msg around 1-3 (each message does some work but doesn't loop)
- Top tools: terminal, read_file, write_file dominate (real work)

**Trouble looks like:**
- "very long (50+)" is a meaningful fraction → context compression isn't running, or the user runs marathon sessions without `/new`
- avg_tools_per_msg > 5 → tool-call thrashing; check errors.log for retries
- A single tool dominates by 10x → likely a loop bug

### Dimension 6 — Memory Effect on Behavior

**Goal:** are the rules and preferences actually steering behavior?

This is the hardest dimension — needs cross-referencing memory state against actual session content.

```bash
# Pick a recent rule
RULE_TEXT="Always run tests before pushing"

# Search session messages for evidence the agent followed it
sqlite3 ~/.hermes/sessions.db <<EOF
.mode list
SELECT s.session_id, m.role,
       substr(m.content, 1, 200) AS snippet
FROM messages m
JOIN sessions s ON m.session_id = s.session_id
WHERE m.content LIKE '%test%push%'
   OR m.content LIKE '%pytest%push%'
ORDER BY m.created_at DESC
LIMIT 10;
EOF
```

For agent introspection, the model can also call `session_search` directly:

```python
session_search(query="test before push", role_filter="assistant", limit=10)
```

**Healthy looks like:**
- Searching for keywords from a rule turns up assistant messages explicitly invoking the rule's spirit (e.g. "running pytest first as you've requested")
- For user preferences in USER.md, search for situations where they'd apply

**Trouble looks like:**
- A rule exists but every session contradicts it → either the rule wasn't injected (check rules_enabled, restart), or the model is overriding it (rule should perhaps be pinned)
- A frequently-edited rule in RULES.md → the user keeps tweaking it because the agent isn't getting it right; consider rewording or pinning

## Putting It Together — Audit Workflow

```
1. Read config.yaml first (Dimension 2) — establishes what should happen
2. Read memory bucket sizes (Dimension 1) — establishes current state
3. Skim errors.log for the last 7-30 days (Dimension 3) — find pain
4. Dump learning store stats (Dimension 4) — is loop closing?
5. Sample session metrics (Dimension 5) — is shape healthy?
6. For 1-3 specific rules, verify behavior (Dimension 6) — sanity check
```

Don't run all 6 in parallel and dump output. Walk through them, flag findings inline, then summarize at the end.

## Audit Report Template

```markdown
# Hermes Self-Audit — <date>

## TL;DR
- Memory: <healthy / borderline / trouble>
- Config: <consistent / drift detected>
- Errors: <quiet / N recurring patterns>
- Learning: <closing / N pending should-have-promoted>
- Sessions: <healthy / N degraded>
- Behavior: <rules respected / N unfollowed>

## Findings

### 🔴 High — must address
- ...

### 🟡 Medium — worth tuning
- ...

### 🟢 Low — informational
- ...

## Recommendations
1. <specific config change or command>
2. ...

## Raw data appendix
<terse summaries; not raw dumps>
```

## Useful One-Liners

```bash
# Watch self-learning nudges in real time
hermes logs -f --component self_learning

# Find a specific session's full log trail
hermes logs --session <session-id-substr> --since 7d

# Tail with WARNING+ filter
hermes logs --level WARNING -f

# Count tool calls per session (last 7 days)
sqlite3 ~/.hermes/sessions.db \
  "SELECT session_id, message_count, tool_call_count
   FROM sessions
   WHERE started_at > strftime('%s', 'now', '-7 days')
   ORDER BY tool_call_count DESC LIMIT 20;"

# Find rules that have never recurred (potential dormant)
sqlite3 ~/.hermes/learning_store.db \
  "SELECT id, pattern_key, summary, recurrence_count, last_seen
   FROM learnings
   WHERE state = 'promoted' AND recurrence_count <= 1
   ORDER BY first_seen ASC;"

# Profile-aware variants — replace ~/.hermes with the profile path
hermes -p <name> logs ...
```

## Hermes Agent Integration

When the user invokes this skill, the agent should:

1. **Acknowledge scope** — clarify which profile to audit (default current) and what time window (default last 7 days)
2. **Run dimensions in order** using `terminal` + `execute_code` for the queries above
3. **Synthesize, don't dump** — translate raw output into the report template's findings
4. **Suggest, don't act** — recommendations should be specific commands or config edits, not auto-applied (memory and learning state are the user's data)
5. **Cross-reference** — when a finding in one dimension suggests another, follow it (e.g. learning loop dormant → check plugin enabled in config → check session prompts)

### With delegate_task

For a long-running deep audit, dispatch a subagent:

```python
delegate_task(
    goal="Run a full Hermes self-audit per the hermes-self-audit skill",
    context="""
    Walk all 6 dimensions: memory health, config consistency, error
    patterns, learning loop, session metrics, behavior verification.
    Read ~/.hermes/ files directly; query session DB and learning store
    via sqlite3.

    Output the full report with findings tagged High/Medium/Low and
    concrete recommendations. Do NOT modify anything — read-only audit.
    """,
    toolsets=["terminal", "file"]
)
```

## Privacy Note

The session DB contains every message you and the agent have exchanged. The audit reads from it but should never **export** it. Keep the report's appendix to terse summaries (timestamps + token counts), not message bodies.

## See Also

- `hermes-memory-guide` skill — for *managing* (vs. auditing) the memory system
- `whitebox-qa-review` skill — for testing implementation-level invariants
- `systematic-debugging` skill — for root-causing specific bugs the audit surfaces
- `docs/memory-and-learning-design.md` — design intent (the "should-be" the audit measures against)
