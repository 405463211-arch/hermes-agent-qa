---
name: create-skill
description: Use when the user wants to create a new Hermes skill, author a SKILL.md, scaffold a skill directory, convert an ad-hoc workflow into a reusable skill, or update/improve an existing skill's frontmatter or body. Adapts Anthropic's skill-creator guidance to Hermes specifics — frontmatter extensions (toolset gating, config injection, env var setup), template variables, slash command generation, gateway behavior, and skill placement decisions. Trigger this skill whenever the user mentions "write a skill", "make a skill", "create a SKILL.md", "turn this into a skill", "improve my skill", "skill file", or asks how to package a workflow for reuse — even if they don't say the word "skill" explicitly.
version: 1.0.0
author: Hermes Agent (combines Anthropic skill-creator guidance with Hermes extensions)
license: MIT
metadata:
  hermes:
    tags: [skills, authoring, meta, scaffolding, hermes-internals, documentation]
    related_skills: [hermes-self-audit, hermes-memory-guide, writing-plans]
---

# Create Skill

A Hermes-native guide for authoring high-quality skills. Combines Anthropic's
public `skill-creator` philosophy with Hermes-specific extensions (toolset
gating, config injection, env-var setup, template variables, slash command
auto-generation, gateway behavior).

## Why this skill exists

Hermes' SKILL.md format is `agentskills.io`-compatible — it deliberately
mirrors Anthropic's Claude Skills spec so skills can move between agents.
But Hermes layers on a half-dozen optional features (`metadata.hermes.*`
extensions, `HERMES_SKILL_DIR` / `HERMES_SESSION_ID` template tokens,
`required_environment_variables`, ...) that an agent improvising from
training data alone won't know about. This skill codifies both layers in
one place so authored skills are correct on the first try.

**Note on token syntax in this document:** the body you're reading is
itself loaded through Hermes' template substitution. To avoid recursive
replacement, this SKILL.md refers to the runtime tokens by **name only**
(e.g., `HERMES_SKILL_DIR`). The literal dollar-curly form
(`$` + `{HERMES_SKILL_DIR}` + `}`) is what you actually write **inside
your authored skill's body** — it will be replaced with the absolute
skill directory path at load time. The full literal-form examples live
in `references/examples.md` (where substitution doesn't run).

## Core principle

**Skills are progressive disclosure.** Three loading tiers:

1. **Frontmatter (name + description)** — always in context, ~100 words
2. **SKILL.md body** — loaded when skill triggers, aim for <500 lines
3. **`references/`, `scripts/`, `assets/`** — loaded on demand

Optimize the description for **triggering** (it's the only signal Hermes
sees by default). Push body content into references when it gets long.

## When to use this skill

- "Write me a skill that does X"
- "Turn this conversation/workflow into a skill"
- "Improve my skill's description so it triggers more reliably"
- "How do I make this skill only load when toolset Y is active?"
- "How do I let users configure a path for my skill?"
- The user already has a draft `SKILL.md` and wants a review

## The workflow

### 1. Capture intent (don't skip this)

Before touching the keyboard, get explicit answers to:

- **What does this skill enable Hermes to do?** Concrete capability, not vague theme.
- **When should it trigger?** Phrases the user would actually type.
- **What's the output / artifact?** A file? A structured response? A side-effect?
- **What does it depend on?** A specific toolset (`web`, `browser`, `terminal`),
  a tool (`web_search`, `browser_navigate`), an env var, a config setting,
  an OS, a CLI binary?
- **Are there test cases?** Skills with objectively verifiable outputs (file
  transforms, data extraction, deterministic workflows) benefit from tests;
  subjective skills (writing tone, design taste) usually don't.

If the conversation already contains the workflow (the user said "turn
this into a skill"), mine the history first — extract the tools used,
the order of steps, the corrections the user made, the input/output
shape — and confirm with the user before drafting.

### 2. Decide WHERE the skill lives

Hermes has multiple skill locations. Pick deliberately.

| Location | Use for |
|---|---|
| `~/.hermes/skills/<name>/` | User-authored skills, personal workflows. Loaded automatically on every session. |
| `skills/<category>/<name>/` (this repo) | Skills shipped with Hermes, broadly useful, lightweight deps. |
| `optional-skills/<category>/<name>/` (this repo) | Skills with heavy deps or niche audiences. Users install explicitly via `hermes skills install`. |
| External hubs (GitHub repos, Anthropic skills) | Installed via `hermes skills install owner/repo/path`. |

**Category folders** in `skills/` are flat, single-level (`research/`,
`productivity/`, `software-development/`, `mlops/`, etc.). Pick the
closest match — don't invent new categories without checking what already
exists.

**Naming convention:** lowercase, hyphen-separated, descriptive
(`arxiv`, `systematic-debugging`, `hermes-self-audit`). The `hermes-`
prefix is reserved for skills that operate on Hermes itself (audit,
memory inspection, etc.) — avoid it for general-purpose skills.

### 3. Choose the directory shape

The minimum viable skill is a single file:

```
my-skill/
└── SKILL.md
```

Add subdirectories only when you need them:

```
my-skill/
├── SKILL.md           # Main instructions (required)
├── references/        # Docs loaded on demand (>300 lines → add a TOC)
│   ├── api-spec.md
│   └── examples.md
├── scripts/           # Executable helpers (Python/bash/JS)
│   └── do_thing.py
├── templates/         # Output templates (jinja, plain text, YAML)
│   └── report.md
└── assets/            # Images, fonts, fixtures
    └── logo.png
```

**Domain organization** — when one skill supports multiple variants
(e.g., AWS/GCP/Azure deploy), keep the workflow logic in `SKILL.md` and
split per-variant docs into `references/aws.md`, `references/gcp.md`, etc.
The agent reads only the relevant variant.

### 4. Write the frontmatter

The frontmatter is a YAML block at the top of `SKILL.md`. Only `name` and
`description` are required by Anthropic spec; everything else is optional.
Hermes adds extensions under `metadata.hermes`.

**Bare minimum (works in both Hermes and Claude Code):**

```yaml
---
name: my-skill
description: Use when the user wants to do X. Triggers Y by following Z workflow.
---
```

**Recommended baseline for Hermes:**

```yaml
---
name: my-skill
description: |
  Use when the user wants to do X. Concrete trigger phrases:
  "do X", "Y the data", "convert Z". Triggers a deterministic
  N-step workflow that produces an .xlsx file. Make sure to use
  this skill whenever the user mentions X, Y, or Z — even if they
  don't explicitly ask for "X".
version: 1.0.0
author: Your Name
license: MIT
metadata:
  hermes:
    tags: [primary-tag, domain, action-verb]
    related_skills: [other-skill, sibling-skill]
---
```

**Description rules — the single most important field:**

- Start with `Use when ...` so the agent's pattern-matching latches on.
- Include both **what it does** AND **specific trigger contexts**.
- Models tend to **undertrigger** skills. Be slightly pushy: list synonyms,
  edge phrasings, and add a "even if they don't explicitly say X" clause.
- All "when to use" info goes here, **not** in the body. The body is loaded
  only after the skill triggers.
- Soft cap: ≤1024 chars (Hermes truncates at this for indexing).
- `name` cap: ≤64 chars.

For the full set of optional Hermes extensions (`requires_toolsets`,
`fallback_for_tools`, `config`, `required_environment_variables`,
`platforms`), see `references/frontmatter-spec.md`.

### 5. Write the body

The body is Markdown. Hermes injects it into the conversation as a **user
message** (not a system prompt) when the skill triggers, so prompt-cache
stays intact. There's no rigid template, but high-signal skills usually
have:

```markdown
# Skill Title

## Overview
1-2 sentence summary of what the skill enables.

## When to use
Concrete trigger conditions. Repeats some of the description, but with room
for nuance ("use this rather than X when ...").

## Prerequisites
What needs to be true for the skill to work — env vars, CLI tools, files
the user provides, toolsets that must be active.

## Workflow / Procedure
The actual instructions. Step-numbered, imperative voice ("Run X. Then
read Y. Verify Z."). Explain *why* steps matter, not just what.

## Quick reference
A table of common commands / API calls / output schemas — the bit the
agent will scan first when re-triggered later.

## Pitfalls
Failure modes the author already discovered. "If X fails with Y, do Z."

## Verification
How the agent confirms the skill worked.
```

**Writing style notes:**

- **Imperative voice.** "Run `script.py`" beats "you should run `script.py`".
- **Explain the why.** Modern LLMs go further with reasoning than rote
  rules. `MUST NOT do X — it triggers Y` is more useful than
  `NEVER do X`.
- **Prefer guidance over hard rules.** Heavy-handed `MUST` / `NEVER` /
  `ALWAYS` walls are a yellow flag. If you find yourself writing a wall
  of all-caps, reframe as "X causes Y problem, so do Z instead."
- **Keep it under ~500 lines.** Push deep dives into `references/<topic>.md`
  with a clear pointer ("For the full API schema, read
  `references/api-spec.md`.").
- **No cross-tool name-dropping in schemas.** Don't reference tools the
  user might not have available (e.g. "use `browser_navigate` instead
  of `web_search`") — it leads to hallucinated tool calls when the
  toolset is disabled.

### 6. Wire up Hermes power features (only what you need)

This is where Hermes diverges from vanilla Anthropic skills. Read the
relevant subsection of `references/hermes-extensions.md` for the full
detail; here's the cheat sheet:

| Need | Use |
|---|---|
| Skill should only show when a specific toolset is active | `metadata.hermes.requires_toolsets: [web]` |
| Skill should hide when a better tool is available | `metadata.hermes.fallback_for_tools: [browser_navigate]` |
| Skill needs an env var (API key, token) | Top-level `required_environment_variables: [{name, prompt, help, required_for}]` |
| Skill needs a config setting (path, threshold) | `metadata.hermes.config: [{key, description, default, prompt}]` |
| Skill restricted to one OS | `platforms: [macos]` (or `[linux, windows]`, etc.) |
| Skill needs to know its own absolute install path | Use the `HERMES_SKILL_DIR` template token in the body (dollar-curly form) |
| Skill needs the current session ID | Use the `HERMES_SESSION_ID` template token in the body (dollar-curly form) |
| Skill should run a shell snippet at load time | Inline `` !`date +%Y-%m-%d` `` (opt-in via `skills.inline_shell` config) |
| Skill bundles helper scripts | Put them in `scripts/`, reference as `HERMES_SKILL_DIR/scripts/foo.py` (with the dollar-curly wrapper) |

**Slash command auto-generation** — every skill is automatically
exposed as `/<skill-name>` in CLI **and** in every gateway platform
(Telegram, Slack, Discord, etc.). No extra wiring. The slash command
loads the SKILL.md content as a user message and the agent takes over.

**Cache-aware activation** — newly installed/edited skills don't take
effect mid-session by default (would invalidate the prompt cache and
spike costs). Users opt in to immediate activation with `--now`. Author
the skill assuming "next session" is the default activation timing.

### 7. Test the skill

After drafting, run **2–3 realistic test prompts**. Realistic means:

- The user wouldn't say "Format this data" — they'd say "ok so my boss
  sent me this xlsx file (it's in Downloads, called 'Q4 sales final
  FINAL v2.xlsx') and she wants a column with profit margin %, revenue
  is column C, costs are column D".
- Mix length, formality, and explicit-vs-implicit triggers.
- Include a near-miss negative — a query that shares keywords but
  shouldn't trigger.

If the skill is for objectively verifiable output (file transforms, data
extraction), save test cases to `evals/evals.json` and run them with
and without the skill, then compare. For subjective output, qualitative
review with the user is enough.

For the full eval/iteration loop (subagents, grading, benchmark viewer),
defer to Anthropic's official `skill-creator` (install with
`hermes skills install anthropics/skills/skills/skill-creator`) — this
skill intentionally doesn't reimplement that pipeline.

### 8. Iterate

**Generalize from feedback** — if a single test case fails, ask whether
the fix is local or whether the skill is wrong on a class of inputs.
Don't add narrow special-case overrides; reframe the underlying pattern.

**Keep the prompt lean** — read the agent's transcript, not just the final
output. If the agent wasted three turns on something the skill made
ambiguous, prune that part of the skill rather than adding more rules.

**Look for repeated work** — if every test run resulted in the agent
writing the same helper script inline, bundle that script into
`scripts/` and tell the skill to use it.

### 9. Pre-publish checklist

Before declaring the skill done, walk through `references/checklist.md`.
Quick version:

- `name` and `description` present, sensible, and pushy enough to trigger
- Body fits in ≤500 lines (or has clear `references/` pointers)
- All bundled-asset paths use the `HERMES_SKILL_DIR` token (not stale absolute paths) and resolve to existing files
- `requires_toolsets` / `requires_tools` actually exist (typos here
  silently disable the skill)
- `required_environment_variables` set up correctly (`prompt`, `help`,
  `required_for`)
- `metadata.hermes.config` keys don't collide with another skill's keys
- Skill loads cleanly (`skill_view <category>/<name>` returns
  `success: true`)
- Test prompts trigger the skill reliably
- License + author present (defaults are fine: `license: MIT`,
  `author: <Your Name>`)

## Reference files

- `references/frontmatter-spec.md` — Complete frontmatter reference
  (Anthropic-spec required + Hermes-extension optional fields), with
  worked examples for every field.
- `references/hermes-extensions.md` — Deep dive into the Hermes-only
  features: toolset/tool gating, config injection, template variables,
  inline shell, env-var setup, slash command behavior across CLI and
  gateway, cache-aware activation.
- `references/examples.md` — Annotated real-world skills from the repo,
  before/after rewrites of weak descriptions, common idioms.
- `references/checklist.md` — Full pre-publish checklist with
  verification commands.

## Pointers to other tooling

- **Want the full Anthropic eval loop** (subagents, blind comparison,
  description-optimizer with auto-tuning) — install the upstream skill:
  `hermes skills install anthropics/skills/skills/skill-creator`. This
  skill complements that one; they're not competitors.
- **Want to publish a skill to a hub** — `hermes skills publish <path>`
  opens a PR to a configured GitHub repo.
- **Want to share via snapshot** — `hermes skills snapshot export <path>`
  bundles all installed skills into one file; users restore with
  `hermes skills snapshot import`.
- **Want to inspect existing skills as templates** — `hermes skills list`
  shows installed skills; `hermes skills inspect <name>` dumps the
  frontmatter.
