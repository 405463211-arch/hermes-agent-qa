---
name: hermes-memory-guide
description: Use when the user asks about Hermes' memory system, /rules, /memory, /learn commands, RULES.md/MEMORY.md/USER.md, the three-bucket model, auto-archiving, the [NEW] trial period, or how to teach Hermes durable preferences. Operational manual for managing Hermes' persistent memory.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [memory, configuration, rules, learning, hermes-internals]
    related_skills: [hermes-self-audit]
---

# Hermes Memory & Learning — User Manual

## Overview

Hermes has **three-bucket persistent memory** plus a **self-learning loop** that auto-promotes recurring patterns. This skill is the manual for managing that system.

**Core principle:** every memory entry is paid for on every future turn. Choose the right bucket; remove what no longer matters.

## The Three Buckets

| Bucket | File | Purpose | Examples |
|---|---|---|---|
| `rules` | `~/.hermes/memories/RULES.md` | Mandatory protocols / red lines | "Always run tests before pushing", "Never rebase main", "必须用中文回答" |
| `memory` | `~/.hermes/memories/MEMORY.md` | Working notes, declarative facts | "This project uses pytest with xdist", "Build command is `make build`" |
| `user` | `~/.hermes/memories/USER.md` | Who the user is | "Prefers concise responses", "Works in PST timezone", "Senior backend engineer" |

**Routing rule of thumb:**

| User said... | Bucket |
|---|---|
| "Always X" / "Never Y" / "must" / "必须" / "红线" | rules |
| "I am..." / "I prefer..." / "Don't bother me with..." | user |
| "This project uses X" / "Build with Y" | memory |

When in doubt: **"would this matter for everything I ask Hermes from now on?"** → rules. **"Is this about me as a person?"** → user. Else → memory.

## Slash Commands Cheatsheet

### `/rules` — view & manage red lines

```
/rules                          # alias for /rules show
/rules show                     # list all rules with %used and entry count
/rules add <text>               # add a rule
/rules remove <substring>       # remove rule containing substring
/rules edit                     # open RULES.md in $EDITOR
/rules pin <id-or-substring>    # mark rule as 'pinned' — highest priority,
                                #   never auto-archived
/rules unpin <id-or-substring>
/rules archive list             # show RULES.archive.md
/rules unarchive <id-or-sub>    # restore an archived rule
```

### `/memory` — overview of all three buckets

```
/memory                         # show all three buckets at a glance with %used
/memory show                    # same
/memory edit-rules              # $EDITOR on RULES.md
/memory edit-memory             # $EDITOR on MEMORY.md
/memory edit-user               # $EDITOR on USER.md
/memory review                  # surface MEMORY.md entries >60 days old
                                #   (legacy entries without timestamps are skipped)
```

### `/learn` — self-learning store

```
/learn                          # alias for /learn list
/learn list                     # pending error/correction patterns
/learn show <id>                # detail view of one entry
/learn stats                    # recurrence histogram, promotion eligibility
/learn resolve <id>             # mark a pattern as fixed
```

There is **no** manual `/learn promote` — promotion is driven by the threshold logic in `learning_record` (the agent calls it; recurring patterns auto-promote to RULES.md once they prove durable).

## How Auto-Archiving Works

When `RULES.md` grows, two triggers prune it (controlled by `~/.hermes/config.yaml` → `memory.*`):

### Trigger A — Capacity protection
Default: when serialized RULES.md exceeds **80%** of `rules_char_limit` (4000 chars), evict oldest non-pinned rules until back under the threshold.

### Trigger B — Age-based eviction
Default: an auto-promoted (`LRN-*`) rule **older than 90 days**, with no recurrence and no edit in 30 days, gets archived. **Manual rules never participate in B.**

### Protection layers
- **Pinned rules** — never archived under either trigger
- **Recently edited / recurred** within 30 days — protected
- **`[NEW — verify before applying]` window** — within 7 days of promotion, the rule is still on probation and protected

Archived rules go to `RULES.archive.md` with metadata, **not** silently deleted. Restore with `/rules unarchive <id>`.

## The `[NEW]` Trial Period

When `learning_record` auto-promotes a recurring pattern to RULES.md, it gets tagged `[NEW — verify before applying]` for **7 days** in the system prompt. This is a probation:

- Hermes will follow the rule but flag it as unverified
- You can `/rules remove <sub>` if it's wrong before it sticks
- After 7 days the marker disappears and the rule becomes a permanent law

Tune the window via `memory.trial_new_marker_days` in config.yaml (set to 0 to disable).

## Configuration Reference (config.yaml → memory)

```yaml
memory:
  memory_enabled: true              # enable MEMORY.md injection
  user_profile_enabled: true        # enable USER.md injection
  rules_enabled: true               # enable RULES.md injection
  memory_char_limit: 2200           # ~800 tokens
  user_char_limit: 1375             # ~500 tokens
  rules_char_limit: 4000            # ~1450 tokens
  lcm_archive_on_overflow: true     # MEMORY.md overflow → push to LCM
  auto_archive_rules: true          # enable rules archiving (master switch)
  auto_archive_capacity_threshold: 0.80   # Trigger A; 0 disables
  auto_archive_age_days: 90         # Trigger B; 0 disables
  auto_archive_recurrence_window: 30      # protection window
  archive_notify: true              # show notice after each archive
  trial_new_marker_days: 7          # [NEW] tag duration; 0 disables
```

To disable a trigger, set its value to **0**, not a high number. (`capacity_threshold = 1.01` does **not** disable — it's a multiplier; only 0 short-circuits the check.)

## Common Tasks

### "Make Hermes always do X from now on"

```
/rules add Always X.
```

Take effect on **next session start** — current session keeps the cached snapshot for prefix-cache stability. Restart with `/new` if you want it active immediately.

### "Stop Hermes from doing Y"

```
/rules add Never Y.
```

### "Hermes keeps reminding me about Z, I get it already"

That's a `user` preference, not a rule:

```
/memory edit-user
# add: "User is aware of Z; do not re-explain."
```

### "Pin the most important rule so it survives any cleanup"

```
/rules pin <unique substring>
```

### "Review what Hermes has been auto-learning"

```
/learn list                       # see pending patterns
/learn stats                      # see promotion eligibility
/rules show                       # see what's already promoted (look for LRN-* IDs and [NEW] tags)
```

### "Reduce Hermes' memory token cost"

1. `/memory show` — check %used per bucket
2. Edit each bucket and remove stale entries
3. `/memory review` — find dormant entries you forgot
4. Lower the char limits in config.yaml if you want hard caps

### "Hermes lost my preferences after upgrading"

Memories are stored in `~/.hermes/memories/`. They're **not** versioned with the codebase. Check:

```bash
ls ~/.hermes/memories/                    # files exist?
hermes -p <profile_name> memory show      # if using profiles
```

If you're using a profile, memories live at `~/.hermes/profiles/<name>/memories/`.

## Anti-Patterns

| Don't | Why | Do this instead |
|---|---|---|
| Save procedural workflows to memory | Workflows belong in skills (loaded on demand) | `skill_manage` to save as a skill |
| Save task progress to memory | Bloats the prompt with stale state | Use the `todo` tool; recall from session_search |
| Dump all preferences into rules | Inflates rules cost; rules are red lines, not preferences | Use `user` for preferences, `rules` only for protocols |
| Imperative phrasing in MEMORY.md | "Always do X" gets re-read as a directive every turn | Declarative facts in MEMORY.md, imperatives in RULES.md |
| Edit RULES.md mid-session expecting effect | Frozen-snapshot pattern keeps prefix cache stable | Changes apply next session; `/new` for immediate |
| Hand-promote learning entries | Bypasses threshold logic, fills RULES with one-offs | Let `learning_record` auto-promote on durability |
| Stuff documentation into RULES.md | Per-turn cost; not what RULES.md is for | Save as a skill or doc |

## How `learning_record` Decides to Promote

The self-learning loop tracks transient errors / corrections / feature requests. A pattern auto-promotes to RULES.md when **all three** thresholds clear:

1. **Recurrence count** — same pattern_key seen N times (default 3)
2. **Distinct task IDs** — across at least M different tasks (default 2; not the same prompt repeated)
3. **Window** — recurrences within a recent time window (default 30 days)

Promoted rules show up in `/rules show` with source `LRN-YYYYMMDD-XXXXXX` and the `[NEW]` tag for 7 days. If a pattern is wrong, remove it during the trial window and the loop won't re-promote (it tracks last_promoted_at).

## Profiles

Each profile has its own memory directory. To work with multiple profiles:

```bash
hermes -p coder /memory show          # coder profile's memory
hermes -p personal /rules show        # personal profile's rules
hermes profile list                    # list all profiles
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/rules add` says "memory store unavailable" | Permissions on `~/.hermes/memories/` | `chmod -R u+w ~/.hermes/memories/` |
| Rules from yesterday are gone | Auto-archived (Trigger B) | `/rules archive list` then `/rules unarchive <id>` |
| `[NEW]` tag stuck after 7 days | `trial_new_marker_days` was raised | Check config.yaml or `promoted_at` in metadata |
| Hermes ignores a new rule | Cached snapshot from before edit | Restart session with `/new` |
| RULES.md keeps filling up | LRN auto-promotion working as designed | Either accept it (trigger A handles overflow) or `/rules unpin` rules you don't need |

## Working with Obsidian (optional sixth layer)

If the user runs `hermes obsidian setup` and points hermes at an Obsidian
vault, a sixth read/write layer comes online. It does not replace any of
the three buckets above — it **mirrors** them into a folder the user can
edit in their note app, plus **searches** their pre-existing notes.

### How the layers map

| Hermes layer | Obsidian mirror file (read-only for the user) |
|---|---|
| RULES.md | `vault/hermes/rules.md` |
| MEMORY.md | `vault/hermes/memory.md` |
| USER.md | `vault/hermes/user.md` |
| `learning_store.db` | `vault/hermes/learnings/LRN-*.md` (one per id) |

### Inbound paths (Obsidian → hermes)

| User does | What happens |
|---|---|
| Adds bullets to `vault/hermes/rules-staging.md` | Auto-imported to RULES.md on next session start (`source: obsidian-import`) |
| Drops markdown into `vault/hermes/ingest/` | Visible to `obsidian_search` (when `search_scope: ingest` or wider) |
| Runs `hermes obsidian import-notes` | Slices ingest files into LCM (queryable via `lcm_search`) |
| Runs `hermes pk import-from-vault <project> <folder>` | Copies notes into `project-knowledge/<project>/` |

### Outbound (hermes → Obsidian)

Triggered automatically on session end (when `auto_export_on_session_end: true`)
or manually via `hermes obsidian export`.

### When to recommend the bridge

- User keeps asking hermes to remember something it already wrote down in their notes
- User wants to manage rules from their phone / iPad (Obsidian Sync)
- User has a large reference doc they want hermes to index
- User wants to inspect / back up hermes' state alongside their notes

### When NOT to mention it

- One-off rule additions where `/rules add` is enough
- Sessions where the user has explicitly disabled the bridge
- When the cost-conscious user is on a token-strict provider — the
  bridge schemas add ~250 tokens/session; usually negligible but worth
  mentioning if they're optimizing aggressively

For the full bridge manual, see the `hermes-obsidian-bridge` skill.

## See Also

- `hermes-self-audit` skill — analyze actual usage logs and session DB to spot quality issues
- `hermes-obsidian-bridge` skill — operational manual for the Obsidian integration
- `whitebox-qa-review` skill — used to validate the memory/learning system's implementation
- Source: `agent/rules_lifecycle.py`, `tools/memory_tool.py`, `agent/learning_store.py`, `agent/obsidian.py`
- Design notes: `docs/memory-and-learning-design.md`
