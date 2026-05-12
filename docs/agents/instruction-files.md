# Writing AGENTS.md and SKILL.md (layered instructions)

`AGENTS.md` ships in the system prompt every turn. A bundled `SKILL.md`
is injected every time its skill triggers. Both are **token taxes on
every request** if they get long, and both fight U-shaped attention
when they exceed ~400 lines (mid-document hard rules get silently
ignored).

This page is the methodology for keeping them small without losing
information: a main file with the load-bearing parts, a sibling
directory with the rest, and a redundancy strategy so hard rules
survive even when the model skips the sibling files.

## When this applies

Split into main + sibling files only when **all** of these hold:

| Condition | Threshold |
|---|---|
| File size | > 400 lines / > 4k tokens |
| Load frequency | Always-applied (`AGENTS.md`) or high-trigger skill |
| Task-irrelevant share | > 60% of content unrelated to a typical single task |
| Section independence | Sections can be read in isolation |

Otherwise just compress — splitting smaller files costs more in
navigation than it saves in tokens.

## What stays in the main file, what moves out

Main file (target: ≤200 lines / ≤2k tokens):

| Keep | Why |
|---|---|
| Hard Rules (violation → bug) | Every request needs to see them; can't be skipped |
| Path Speed Sheet | High-frequency lookup; avoids re-discovering structure |
| Common Enums (closed sets) | Model invents new values otherwise — see §4 |
| Task → docs Index | The ONLY signal that sibling files exist |
| One-line mechanism sketches | Lets the model work without reading siblings for the happy path |

Move to siblings (`docs/agents/<topic>.md` for `AGENTS.md`,
`references/<topic>.md` for a `SKILL.md`):

| Move | Why |
|---|---|
| Full API signatures, field tables | One-line invocation in main is enough |
| Worked examples | Linkable; rarely needed by every reader |
| Design rationale / "why we picked X" | Doesn't affect "how to do it" |
| Changelogs, phase notes | Historical, not procedural |
| Setup / environment / dependencies | One-time configuration |

Naming: kebab-case topic names, single-purpose per file, no
cross-references between siblings (each must be readable standalone).

## Four-tier defense for hard constraints

Documentation alone (L4) is insufficient — the model **will** violate
rules it has read if a stronger signal is available elsewhere (e.g.
its prior on "Utility is a common command category" overriding a
docs-only enum list). Promote each hard rule to the strongest tier
practical:

| Tier | Form | Strength | Example |
|---|---|---|---|
| **L1** | `typing.Literal[...]` / `Enum` | ★★★★★ | `category: Literal["Session", "Configuration", ...]` |
| **L2** | `__post_init__` raise / module-load assert | ★★★★★ | `if cmd.category not in {...}: raise ValueError(...)` |
| **L3** | Source comment + automated test | ★★★★ | `# one of: A / B / C` + `test_all_categories_valid` |
| **L4** | `AGENTS.md` Hard Rules / `docs/agents/<file>.md` | ★★ | "Common Enums" table |

Rule of thumb:

- Closed-set enums → **always L1 + L2**, never L4 only
- Path conventions / shape contracts → L3 + L4 minimum
- Editorial / stylistic / "prefer X" rules → L4 is fine

## Promotion ladder (when a rule keeps getting violated)

| Symptom | Promote to |
|---|---|
| Rule in a sibling file gets missed | Add a copy in main-file Hard Rules |
| Main-file Hard Rule still violated | Add a source comment (L3) |
| L3 comment still violated (esp. enum cases) | `typing.Literal[...]` (L1) |
| L1 bypassed via dynamic construction / `type: ignore` | `__post_init__` runtime assert (L2) |
| L2 reached by an already-released path | Add a CI test that exercises the path |

`AGENTS.md` is the natural starting point for new rules, but anything
that has been violated **twice** should be moved up at least one tier.

## Anti-patterns

| Anti-pattern | Why it fails |
|---|---|
| Put Hard Rules in a sibling, link from main | Model may skip the sibling; "violation → bug" rules must be visible every turn |
| Split 700 lines into 30 sibling files | Model can't tell which to read; navigation cost exceeds token savings (target 8–15 siblings) |
| Main file with no Task Index | Model doesn't know the siblings exist, behaves as if they don't |
| Use relative paths like `./refs/foo.md` | Model gets them wrong; always use repo-root-relative or `~/.hermes/...` |
| Closed-set enum only in L4 docs | Model invents "Utility" / "Other" — see §3, must be L1/L2 |
| Sibling files reference each other for context | Breaks standalone readability; the model can't follow a chain |
| Skip verification after splitting | Hard rules drift; at minimum grep each rule's keywords across main + intended sibling |
| Put war stories in the main file | "Why we used to do X" is sibling material; main file is "what to do now" |

## Main-file workflow when adding new content

1. Decide tier first (L1–L4). If the rule needs L1/L2, write the
   source change before the docs change.
2. If the addition is a **hard rule**: append to the appropriate
   section of `AGENTS.md` Hard Rules. Keep it ≤4 lines plus an
   arrow link to a sibling for details.
3. If the addition is a **task-area detail** (e.g. how to add a new
   tool): write the sibling under `docs/agents/<topic>.md`, then add
   one row to the Task Index in `AGENTS.md`.
4. If the main file crossed 200 lines: revisit §2 and move
   non-load-bearing sections out.
5. After every edit run a grep check that every hard rule's keyword
   still appears in both the main file and its intended sibling
   (rephrasing during edits silently breaks this).

## Sibling file shape

Open with an H1 matching the topic and one or two sentences telling the
model **when to read this file**. Close (when relevant) with a back-link
to the main-file Hard Rules section that gates this content.

Do not depend on having read a sibling neighbor. The model may arrive
at any single sibling without context — make sure that's enough.

## Self-application

This repository is its own example. `AGENTS.md` is ~140 lines; the 13
files under `docs/agents/` are siblings; the Task Index in `AGENTS.md`
is the only directed link from main to siblings. When tempted to add a
section to `AGENTS.md`, run through §2 first — most additions belong in
a new or existing `docs/agents/<topic>.md`.
