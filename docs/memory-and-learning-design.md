# Memory & Learning — Design Notes

> Maintainer reference. For end-user usage, see the `hermes-memory-guide` skill.
> For testing methodology, see the `whitebox-qa-review` skill.

This document records the **why** behind hermes-agent's memory + learning architecture. Read this before changing the data model, the auto-archive thresholds, or the promotion logic — the constraints below are not arbitrary.

---

## Hard Constraints (Non-Negotiable)

These are red lines from `AGENTS.md`. Any change must preserve all of them:

1. **Prefix cache stability** — the system prompt MUST NOT change mid-session except during context compression. Memory entries injected into the prompt are taken from `_system_prompt_snapshot`, frozen at session start.
2. **No mid-session toolset changes** — adding a tool requires a new session.
3. **Per-turn cost is multiplicative** — every char in RULES/MEMORY/USER is paid on every turn × every session. Cap aggressively.
4. **Best-effort indexing must not block main flow** — LCM, external memory providers, hooks may all fail; archive bookkeeping must complete regardless.

---

## The Three-Bucket Model

### Why three buckets, not one?

A single "memory" bucket would force the model to triage every entry's importance on every turn. By tiering at write time, we make that decision once, not 1000 times:

| Bucket | Mental model | Imperative? | Auto-managed? |
|---|---|---|---|
| `rules` | Mandatory laws (red lines) | YES — model treats as directives | YES — auto-archive |
| `memory` | Working notes (declarative facts) | NO — declarative phrasing only | NO direct, but LCM overflow |
| `user` | Who the user is (identity layer) | NO | NO |

### Why is `rules` the one with auto-archive?

Because `rules` is the only bucket the **agent itself** writes to (via `learning_record` → auto-promote). Memory and user are written by the human — humans curate manually; the agent's writes need housekeeping or the bucket would fill in days.

### Injection order

In `run_agent.py:_build_system_prompt()`:

```
SOUL.md (identity)
  ↓
PINNED rules     ← max-salience tier, never auto-archived
  ↓
REGULAR rules    ← normal tier, [NEW] tags appear here
  ↓
... tool guidance, skill guidance, ...
  ↓
MEMORY.md        ← working notes, lower priority
  ↓
USER.md          ← user identity layer
```

**Why pinned before regular before memory/user?** Salience decay — entries closer to the "task" portion of the prompt have stronger pull. Pinned rules are red lines; they go first. Regular rules next. Then context (memory + user).

This order is locked by static analysis in `tests/_white_box/m3_agent_probe.py::TestSystemPromptOrderInvariants` — any future refactor that swaps the order fails CI.

---

## Auto-Archiving — Two Triggers, Defense in Depth

### Trigger A — Capacity (`capacity_threshold = 0.80` of `rules_char_limit`)

When serialized RULES.md exceeds 80% of its budget, evict oldest non-pinned rules until under the line.

**Why 80%, not 100%?** Headroom for the next learning_record promotion. If we hit 100% then archived, the next add would fail or trigger immediately again. 80% gives one breath of slack.

**Why oldest-first, not LRU?** Last-recurrence and last-edit are protection windows; we don't want recently-active rules to get archived just because they're old. Oldest-first + the protection windows = the equivalent of LRU + LFU + age, simpler to reason about.

### Trigger B — Age (`age_days = 90`, only LRN-* rules)

A rule auto-promoted by `learning_record` (source = `LRN-*`) older than 90 days, with no recurrence and no edit in the last 30 days, is archived.

**Why ONLY LRN-* rules?** Manual rules (added via `/rules add`) reflect explicit user intent. We don't presume to delete those. The agent's own auto-promoted rules are subject to lifecycle; the user's are sacred.

**Why 90/30/7?** These came from a back-of-envelope balance:
- 90 days = "if I haven't seen this pattern in a quarter, it probably stopped happening"
- 30-day recurrence/edit window = "any usage in the past month resets the clock"
- 7-day NEW marker = "give the user one work week to verify and reject before locking in"

### Invariant: `trial_new_marker_days <= auto_archive_age_days`

This is enforced by `tests/_white_box/m6_config_probe.py::TestDefaultConfig::test_age_window_sensible`. If trial > age, a rule could be archived **while still wearing [NEW]** — i.e. while still on probation. That's a design contradiction.

---

## The `[NEW]` Trial Period

Newly auto-promoted rules carry `[NEW — verify before applying]` for `trial_new_marker_days` (default 7) days in the system prompt.

### Why a trial period at all?

`learning_record` promotions are **inferred**, not explicit. The recurrence threshold means we've seen the pattern N times — but inference can be wrong. The user might catch a bad promotion within their first week with the new rule active. The trial:

1. **Signals** to the model that the rule is unverified (`verify before applying`)
2. **Protects** the rule from age-based archive (a rule can't simultaneously be on probation and stale)
3. **Gives** the user a window to remove it without dispute

After 7 days, the rule earns its place. The marker disappears, and lifecycle treats it like any other LRN entry.

### Why mark only the regular tier, not pinned?

Pinned rules go through manual user action. They aren't on probation — the user already verified them. Adding `[NEW]` to a pinned rule would dilute the "this is a red line" signal.

Locked by `m3_agent_probe.py::TestFormatRulesByTier::test_new_marker_only_in_regular_tier`.

---

## Self-Learning Plugin — Observation Only

`plugins/self_learning/` registers two hooks:

- `post_tool_call` — observes tool errors, classifies them via `error_detector.classify_tool_error`
- `pre_llm_call` — when a recurring pattern crosses `DEFAULT_NUDGE_THRESHOLD` (default 2 per session), inject a one-line nudge suggesting the agent call `learning_record`

### Why is the plugin observation-only?

If the plugin auto-recorded entries on the agent's behalf, two failure modes appear:

1. **Stealth writes** — entries appear in the learning store the agent never decided to write. Future `learning_record` dedupes against unfamiliar entries.
2. **Authority confusion** — the agent is the only writer to memory by design (`AGENTS.md` says the agent is the writer; plugins are observers). Auto-record breaks that contract.

So the plugin **only nudges**. The agent decides whether to call `learning_record`. The agent stays the single writer.

### Why throttle nudges?

Without throttling, the plugin would spam every turn after threshold is crossed. The throttle (one nudge per `pattern_key` per session) keeps the loop visible without becoming noise.

### Why per-session, not global?

State that persists across sessions belongs in the `LearningStore` SQLite. The plugin's per-session dict is an in-memory tally — keeping it scoped means a fresh session gets a fresh chance to nudge if the pattern reappears (the `LearningStore` will dedupe by pattern_key on the actual record, so we don't double-record).

---

## `learning_record` → Promotion Pipeline

```
agent observes recurring pattern
        ↓
agent calls learning_record(category, pattern_key, summary, ...)
        ↓
LearningStore.record():
    pattern_key exists?
        YES → UPDATE recurrence_count++, last_seen, distinct_tasks
        NO  → INSERT new row (id = LRN-YYYYMMDD-XXXXXX)
        ↓
is_eligible_for_promotion() check:
    recurrence_count >= 3 AND
    distinct_tasks >= 2 AND
    first_seen within recurrence_window AND
    state == "pending"
        ↓
if eligible:
    add_rule_with_lifecycle(text, source=LRN-id, promoted_at=today)
    → RULES.md gets the rule with hermes-meta block
    → state = "promoted"
    → carries [NEW] tag for 7 days
```

### Why the three thresholds (count + distinct_tasks + window)?

Each threshold guards a different failure mode:

- **count without distinct_tasks** → same task repeating (one bug, ten retries) auto-promotes. Multiple distinct tasks proves the pattern transcends a single failure.
- **count without window** → a 3-month-old pattern that never came back auto-promotes today. Window forces recency.
- **distinct_tasks without count** → a coincidence (two unrelated tasks both touched the same surface) auto-promotes. Count proves it's a pattern.

All three together = pattern + cross-task evidence + recency.

### Why `state == "pending"`?

Once promoted, an entry can't auto-promote again. Otherwise the same pattern would cycle: archive (90d) → re-record on next occurrence → re-promote → archive ... ad nauseam. The "promoted" state is sticky; if archived, the entry can be unarchived (`/rules unarchive`) but won't auto-promote again.

---

## ID Format & Collision Math

### `LRN-YYYYMMDD-XXXXXX` (6 hex chars)

Original implementation used **3 hex chars** (4096 combinations). Birthday-paradox collision probability for N same-day inserts:

- N=50 → 26%
- N=100 → 70%
- N=500 → ~100%

We hit collisions in M9 stability tests with 500 distinct pattern_keys (BUG-M9-1).

Bumped to 6 hex chars (16M combinations):

- N=1000 → 0.003%
- N=10000 → 3%

Sufficient headroom for any realistic write rate. Older 3-char IDs on disk continue to work — the column is TEXT, no migration needed.

---

## LCM Indexing — Best-Effort, Defense in Depth

`run_auto_archive` calls `_index_archive_to_lcm` to push archived rules into LCM (long-context memory) for retrieval via `lcm_search`. Two layers of try/except (BUG-M8-1):

1. **Inner** — `_index_archive_to_lcm` catches errors per-entry, so one bad row doesn't cancel the rest
2. **Outer** — `run_auto_archive` catches errors from the call itself (e.g. attribute access on a None LCM engine, provider raised before its wrappers)

The archive **file itself** is the durable record. LCM is a search optimization — losing it doesn't lose the rule.

---

## File Locking & Concurrency

`MemoryStore` uses an in-process lock (`threading.Lock`) plus atomic file writes (`tmp file + rename`).

### Why both?

- **Threading.Lock** — guards in-memory `rules_entries` / `memory_entries` / `user_entries` against race conditions in the same process (concurrent slash commands, plugin hooks, the agent's tool calls).
- **Atomic rename** — guards readers (other processes, external tools) from seeing a partially-written file. SQLite-style: open-old-or-new, never half.

### Cross-process?

`MemoryStore` does NOT take an OS-level file lock. Multiple processes (e.g. CLI + gateway running same profile) could both write and the latter wins. This is intentional — the alternative (fcntl lock) blocks legitimately concurrent reads, and the rename atomicity is enough for the actual scenarios we've seen.

If two profiles both want to write, that's what profiles are for. They have separate `HERMES_HOME`s.

### LearningStore is per-thread

`sqlite3.Connection` is not thread-safe; `LearningStore` caches one connection per instance. Production model: each thread (subagent) constructs its own LearningStore pointing at the shared DB file. SQLite's file lock + `timeout=5.0` handles the cross-store coordination.

---

## Configuration Strategy

### `config.yaml` vs `.env`

Per `AGENTS.md`: **`.env` is for SECRETS only** (API keys, tokens). Settings (timeouts, thresholds, feature flags) belong in `config.yaml`.

Memory config lives in `config.yaml` because:
- Char limits are budgets, not secrets
- Trigger thresholds are tunables
- Feature flags are tunables

If internal code needs an env-var mirror for backward compat, bridge from config.yaml to env in code (NOT in user-facing docs).

### Deep-merge invariant

User config overrides one key in `memory:` → all other defaults preserved. This is enforced by `_deep_merge` in `hermes_cli/config.py` and locked by `tests/_white_box/m6_config_probe.py::TestUserOverride::test_user_partial_override_keeps_other_keys`.

If a user has only:
```yaml
memory:
  memory_char_limit: 1000
```
they still get all 9 new memory keys at their defaults — the merge fills in.

---

## Where to Edit, Where Not To

### To change the model (what entries look like)

`agent/rules_lifecycle.py` — `RuleEntry` dataclass + parse/serialize. Adding a field requires:
1. Update dataclass
2. Update `parse_rule_entry` (read from hermes-meta block)
3. Update `serialize_rule_entry` (write to hermes-meta block)
4. Update tests in `tests/agent/test_rules_lifecycle.py` and `tests/_white_box/m1_storage_probe.py`

### To change auto-archive policy

`agent/rules_lifecycle.py:auto_archive_rules()` — the function. Tests in `m1_storage_probe.py` + integration in `m3_agent_probe.py::TestRunAutoArchive`.

### To change promotion thresholds

`agent/learning_store.py:PromotionRule` dataclass + `is_eligible_for_promotion`. Tests in `m1_storage_probe.py::TestLearningStorePromotionEligibility`.

### Do NOT touch

- `format_for_system_prompt('rules')` returning the snapshot — this is the prefix-cache contract
- `_invoke_tool` / worker-loop dispatch passing `store=self._memory_store` — losing it silently breaks auto-promote in one path while the other works
- Injection order in `_build_system_prompt` — pinned > regular > memory > user

All three are locked by static-analysis tests; CI catches regressions.

---

## Open Design Questions (Not Yet Decided)

1. **Cross-process file locking** — current model is "last writer wins"; might want fcntl when multiple agents (CLI + gateway) share a profile
2. **Gateway dispatch for /rules /memory /learn** — currently CLI-only; messaging users go through chat-driven memory management. Worth wiring inline handlers? Trade-off: more code paths to maintain vs. snappier UX in messaging
3. **LearningStore thread sharing** — per-instance connections work; if we ever want one process-wide store with multi-thread access, would need `threading.local` or a connection pool
4. **External memory provider integration during auto-archive** — currently bridged on add/replace, not on archive. If you have honcho/mem0 plugged in, archives are file-only. Add a hook?

These are intentional non-decisions; document the trade-off when re-opening.

---

## See Also

- `AGENTS.md` — the project-wide red lines this design serves
- `tests/_white_box/m{0..10}_*.py` — the invariant tests that lock the design
- `TEST_REPORT.md` — the QA report from the May 2026 white-box review
- Source files: `agent/rules_lifecycle.py`, `agent/learning_store.py`, `tools/memory_tool.py`, `tools/learning_tool.py`, `plugins/self_learning/`
