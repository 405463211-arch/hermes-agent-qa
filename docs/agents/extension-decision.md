# Choosing the Right Extension Point

Hermes has six places where new behavior can live. Picking the wrong one
either bloats every request, breaks prompt caching, or leaks state across
profiles. This page is the **decision entry point**. Implementation details
live in the sibling docs (linked at the end).

The matrix is the load-bearing artifact — when in doubt, scan it first.

## Two-/Three-times rule (signal you need an extension)

| Signal | What to add |
|---|---|
| Same fact corrected twice across sessions ("we use uv, not pip") | `AGENTS.md` line |
| Same multi-step procedure pasted a third time | a **skill** |
| Tool/data source needed from outside the agent (DB, Slack, GitHub) | an **MCP server** |
| Auxiliary research/refactor floods the main transcript | **delegate_task** subagent |
| Deterministic action that must happen on every event ("never touch `.env`") | a **hook** (shell or plugin) |
| Cross-session, mutable user/project state | a **memory provider** |

Anything below twice is premature; remove it next session if the pain didn't recur.

**Detection helpers for hook candidates** — you don't have to spot them by
hand:

- **Within a session:** `HookHinter` (`agent/hook_hinter.py`) watches your
  tool calls and appends a one-shot `[hermes hook hint]` reminder to a
  tool result the moment the same fingerprint crosses 3 occurrences.
  When a bundled starter template fits (`.py`/`.yaml` writes, `black`/
  `ruff`/`rm`/`git push --force` terminals), the hint includes a
  concrete `hermes hooks new --from-template <name>` command; otherwise
  it falls back to a generic `--event/--matcher` scaffold. Surface it
  to the user **once** and continue. Off switch:
  `display.hook_suggestions: off`.
- **Across sessions:** `hermes hooks suggest --lookback-hours 168` mines
  `~/.hermes/sessions/session_*.json` for repeated fingerprints. Add
  `--with-llm` for matcher refinement + rationale (90s wall timeout;
  falls back to frequency-only on timeout).
- **To scaffold once chosen:** `hermes hooks new --from-template <name>`
  — see `skills/productivity/create-hook/SKILL.md` for the full workflow.

## Speed sheet

```
┌─ persistent fact the agent must ALWAYS see?
│   └─ ≤200 lines total ──────────────► AGENTS.md / .hermes.md
│   └─ longer ─────────────────────────► split per docs/agents/, link from AGENTS.md
│
├─ procedure / domain knowledge invoked ON DEMAND?
│   └─ light deps, ships with repo ────► skills/ (built-in skill)
│   └─ heavy deps / niche audience ────► optional-skills/ (opt-in install)
│
├─ external API / data source?
│   └─ already speaks MCP ─────────────► mcp_servers: in config.yaml
│   └─ Hermes-specific tool needed ────► tools/<name>.py + register
│
├─ side-effect every time event X fires?
│   └─ no Python deps, simple guard ───► shell hook  (hooks: in config.yaml)
│   └─ rich Python state / typing ─────► plugin hook (plugins/<name>/)
│
├─ isolated parallel work, only summary back?
│   └─ ───────────────────────────────► delegate_task (subagent)
│
└─ cross-session user/project state?
    └─ ──────────────────────────────► memory provider plugin
```

## Sibling-feature confusion table

These are the pairs people actually mix up. Resolve by the right-hand column.

| Confused pair | Pick A when | Pick B when |
|---|---|---|
| `AGENTS.md` **vs** skill | The fact must condition **every** turn (architecture, build cmd, profile rules) | Knowledge needed **occasionally**, can be discovered on demand. Anchor: AGENTS.md should be ≤200 lines (`docs/agents/instruction-files.md`). |
| skill **vs** subagent (delegate_task) | You want **reusable knowledge** the model loads into its own context | You want **isolated labor** — heavy reading or batch work whose intermediate steps must not pollute the parent transcript |
| skill **vs** hook | Decision needs **model reasoning** (e.g. "draft a PR description") | Decision is **deterministic** (block `rm -rf /`, run `black` after writes) — no LLM thought required |
| plugin hook **vs** shell hook | You need typed Python state, in-process speed, or to register tools/commands | You want a drop-in script in any language with subprocess isolation and first-use consent |
| MCP **vs** native tool | Server already exists or your tool is generic enough to be reused outside Hermes | Tool needs Hermes-internal state (`task_id`, agent ref, approval callback) — register via `tools/registry.py` |
| subagent **vs** skill that calls `delegate_task` | Single isolated child for one focused subgoal | A skill **wraps** the orchestration pattern (e.g. `subagent-driven-development` skill) so it's repeatably invokable |

## Context-cost ranking (per request, when active)

`/context` is the source of truth — these are the buckets it surfaces.

| Bucket | Cost shape | Mitigation |
|---|---|---|
| `system_prompt` (AGENTS.md, SOUL.md, env hints) | **Loaded every request, hot path** | Keep ≤200 lines, split into `docs/agents/` |
| `system_tools` + `mcp_tools` schemas | Every request, scales with toolset count | Disable toolsets you don't use (`/tools`) |
| `skills` index | Every request, but only metadata (≤1024 char descriptions) | Progressive disclosure — full content only via `skill_view()` |
| `memory_files` (MEMORY.md, USER.md) | Every request | Bounded by curator; review with `/memory` |
| `messages` | Grows per turn | Compression at `compressor.threshold_tokens` |
| Hooks (shell + plugin) | **Zero context cost** | Run in subprocess / native callback |
| Subagent (`delegate_task`) | **Zero parent cost** for intermediate steps | Only the summary re-enters parent context |

Rule of thumb: if a capability would land in the top three rows of this
table, ask whether it could be expressed as a hook or subagent instead.

## Layering / priority when extensions overlap

Profile / user → project → plugin → subdirectory, with **more-specific
wins**. Two important non-obvious cases:

- **Project context files** (`.hermes.md` / `AGENTS.md` / `CLAUDE.md` /
  `.cursorrules`) are loaded by `build_context_files_prompt()` as
  **first-found-wins**, not layered merge. `SubdirectoryHintTracker` then
  **appends** subdirectory hints to tool results — it does not replace
  the root context. See `docs/agents/policies.md`.
- **Hooks** never overwrite each other. Every matching `(event, matcher)`
  pair fires. Plugin hooks register first, so their block decisions win
  ties when both vote "block". See `agent/shell_hooks.py:14`.

## Hard rules

- **Hard rules belong in AGENTS.md, not skills.** Skills load on demand
  and may be missed. Anything that causes a bug when violated must live
  in `AGENTS.md` (L4) **and** in source via `typing.Literal` /
  `__post_init__` (L1/L2). See `docs/agents/instruction-files.md`
  §"Four-tier defense".
- **Don't reach for a plugin to do a one-line guard.** A 6-line shell
  hook is cheaper to author and audit than a Python plugin package.
- **Don't reach for a subagent to ask one question.** Subagents inherit
  no parent history (`tools/delegate_tool.py` warning). Use them when
  the cost of polluting the parent context exceeds the cost of restating
  the goal.
- **MCP server-side ≠ MCP client-side.** Hermes can be either
  (`tools/mcp_tool.py` is the client, `mcp_serve.py` exposes Hermes as a
  server). Mixing those up in design discussions is the most common
  confusion.

## Where to read next

| You want to | Read |
|---|---|
| Add an entry to `AGENTS.md` / split a long context file | `docs/agents/instruction-files.md` |
| Add a built-in skill | `docs/agents/skills.md` |
| Author a plugin (general / memory / context-engine) | `docs/agents/plugins.md` |
| Write a shell hook (scaffold + propose) | `skills/productivity/create-hook/SKILL.md` |
| Hook protocol / security model | `website/docs/user-guide/features/hooks.md` |
| Hook starter recipes | `scripts/agent-hooks-examples/README.md` |
| Wire an MCP server / OAuth | `website/docs/user-guide/features/mcp.md` |
| Spawn subagents from code | `tools/delegate_tool.py` docstring + `website/docs/user-guide/features/delegation.md` |
| Understand priority / first-found-wins | `docs/agents/policies.md` |
