# Examples — Idioms and Real Skills

Worked examples drawn from the bundled `skills/` directory plus a few
illustrative before/after rewrites. Read these alongside `frontmatter-spec.md`
to get a feel for what good looks like.

---

## Real skill: minimal single-file skill

`skills/software-development/systematic-debugging/SKILL.md` — one of the
simplest skills in the repo. No `references/`, no `scripts/`, just a
single SKILL.md.

```yaml
---
name: systematic-debugging
description: Use when encountering any bug, test failure, or unexpected behavior. 4-phase root cause investigation — NO fixes without understanding the problem first.
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
metadata:
  hermes:
    tags: [debugging, troubleshooting, problem-solving, root-cause, investigation]
    related_skills: [test-driven-development, writing-plans, subagent-driven-development]
---

# Systematic Debugging

## Overview
...
```

**What it does well:**
- Description starts with `Use when` and lists explicit triggers ("any bug,
  test failure, or unexpected behavior").
- Has a punchy ending — `4-phase root cause investigation — NO fixes
  without understanding the problem first` — that signals scope.
- Minimal frontmatter — only what's needed.
- Cross-references siblings via `related_skills` so the agent can pull
  them in together.

---

## Real skill: skill with bundled scripts

`skills/productivity/google-workspace/` ships helper scripts because every
invocation needs the same Google API plumbing.

```
google-workspace/
├── SKILL.md
├── references/
│   └── gmail-search-syntax.md
└── scripts/
    ├── google_api.py        # shared client
    ├── gws_bridge.py        # CLI wrapper
    └── setup.py             # auth flow
```

**The pattern:** SKILL.md tells the agent to call
`${HERMES_SKILL_DIR}/scripts/gws_bridge.py` rather than reinventing the
auth flow inline. Script is bundled once; every invocation reuses it.

**Why this matters:** if every test run resulted in the agent writing the
same helper script inline, that's a strong signal the skill should bundle
the script. Anthropic's `skill-creator` calls this out explicitly: read the
transcripts, and if the agent independently writes a `create_docx.py` 3
times in a row, the skill should bundle it.

---

## Real skill: skill with rich `references/`

`skills/creative/baoyu-infographic/` has a deeply nested `references/`
because the skill supports many variants:

```
baoyu-infographic/
├── SKILL.md
├── PORT_NOTES.md
└── references/
    ├── analysis-framework.md
    ├── base-prompt.md
    ├── structured-content-template.md
    ├── layouts/                       # 19 layout-specific docs
    │   ├── bento-grid.md
    │   ├── circular-flow.md
    │   ├── ...
    └── styles/
```

**The pattern:** the `SKILL.md` workflow is short and delegates to the
right `references/layouts/<name>.md` based on what the user wants. The
agent reads only the relevant layout file, keeping the active context
small.

**Use this pattern** when a skill has natural variants that don't all
need to be in context simultaneously (different cloud providers,
different output formats, different framework versions).

---

## Real skill: env vars + config injection

A hypothetical `arxiv` skill (paraphrasing what's in the repo):

```yaml
---
name: arxiv
description: |
  Use when the user wants to find, read, or summarize papers on arXiv.
  Triggers on arXiv IDs, paper URLs, "lookup the X paper", "what's the
  latest on Y in arXiv", or general research queries unless the user
  specifies another source.
version: 1.0.0
metadata:
  hermes:
    tags: [research, papers, arxiv, science]
    requires_toolsets: [web]
    config:
      - key: arxiv.cache_dir
        description: Where to cache downloaded papers
        default: "~/.cache/arxiv"
        prompt: arXiv cache directory
      - key: arxiv.max_results
        description: Default page size for arXiv search
        default: 20
required_environment_variables:
  - name: SEMANTIC_SCHOLAR_API_KEY
    prompt: Semantic Scholar API key (recommended, optional)
    help: Get a free key at https://www.semanticscholar.org/product/api
    # no required_for -> optional
---

# arXiv Research

## Workflow

1. Search arXiv for the user's query:
   ```bash
   python ${HERMES_SKILL_DIR}/scripts/arxiv_search.py \
     --max-results <max_results> \
     --cache-dir <cache_dir> \
     "<query>"
   ```
   The `<max_results>` and `<cache_dir>` placeholders are replaced
   with the values from `[Skill config: ...]` (injected at load time).
...
```

**What's happening:**
- `requires_toolsets: [web]` — skill is hidden when the user has the
  web toolset off. Saves the agent from a guaranteed-to-fail attempt.
- `metadata.hermes.config` — at load time, Hermes injects
  `[Skill config: arxiv.cache_dir = ..., arxiv.max_results = ...]` so
  the agent knows the user's chosen values without reading config.yaml.
- `required_environment_variables` with no `required_for` — the API key
  is optional. The wizard prompts for it but `hermes skills install`
  succeeds even if the user skips it. The skill loads with a
  `[Skill setup note: ...]` warning if the key is missing.
- `${HERMES_SKILL_DIR}/scripts/arxiv_search.py` — absolute path to the
  bundled helper, regardless of where the skill is installed.

---

## Before / After: weak description → strong description

### Before

```yaml
description: A skill for working with PDFs.
```

**Problems:**
- Vague — no trigger phrases.
- "for working with" — passive, doesn't tell the agent when to load.
- Missing what's actually possible (read? edit? extract? OCR?).
- Models will undertrigger this.

### After

```yaml
description: |
  Use when the user wants to read, extract text/tables from, combine,
  split, rotate, watermark, fill, encrypt, or OCR PDF files. Triggers
  on any mention of a `.pdf` file, "PDF" in conversation, or phrases
  like "extract pages from this doc", "merge these into one PDF", or
  "what does this scanned form say". Use this skill whenever a PDF is
  involved as input or output, even if the user doesn't explicitly say
  "use the PDF skill".
```

**What changed:**
- Starts with `Use when` — anchors the trigger.
- Enumerates concrete capabilities (read, extract, combine, ...).
- Lists realistic user phrases ("merge these into one PDF").
- Closes with a pushy clause to fight undertriggering.
- Adopts Anthropic's recommended language pattern from their `pdf` skill.

---

## Before / After: rigid MUST walls → "explain why"

### Before

```markdown
## Workflow

1. ALWAYS read the input file first.
2. NEVER write to the output before validating.
3. MUST use UTF-8 encoding.
4. ALWAYS run the validator before returning.
5. NEVER skip step 3.
```

**Problems:**
- All-caps imperatives don't tell the agent why they matter.
- Modern LLMs respond better to reasoning than to commands.
- Hard to remember which is which when re-reading.

### After

```markdown
## Workflow

1. **Read the input first** — early validation catches bad inputs before
   they corrupt downstream state.
2. **Validate before writing** — the output file is overwritten
   destructively, so failed runs leave the user without their original.
3. **Use UTF-8 encoding** — the downstream tool chokes on UTF-16 and
   most other tools default to UTF-16 on Windows. Force it explicitly.
4. **Run the validator before returning** — the user trusts the skill's
   final report; a silent encoding error here means downstream
   workflows blow up tomorrow.
```

**What changed:**
- Imperative voice retained (still "Read", "Validate", "Use").
- Each step explains the **why** — what failure mode the rule
  prevents.
- The agent can reason about exceptions when context warrants.
- Easier to re-read; the why's act as memory hooks.

---

## Idiom: the auto-injected skill-directory marker

You **don't** need to add a "[Skill directory: ...]" footer to your
SKILL.md body — Hermes automatically appends one at load time, like:

```
[Skill directory: /Users/me/.hermes/skills/productivity/my-skill]
Resolve any relative paths in this skill (e.g. `scripts/foo.js`,
`templates/config.yaml`) against that directory, then run them
with the terminal tool using the absolute path.
```

This means inside your body you can use **relative** paths
(`scripts/foo.py`, `templates/report.md`) and trust the agent will
resolve them against the injected directory. Or, when you want absolute
paths inline (e.g., for a one-shot command the agent should run), use
the `HERMES_SKILL_DIR` template token in dollar-curly form — the
substitution happens before the agent reads the body.

**Anti-pattern:** manually adding `[Skill directory: ...]` to your body.
You'll get a duplicate footer at load time.

---

## Idiom: the "Quick reference" block

For skills the agent will re-trigger many times, include a compact
"Quick reference" block near the top of the body:

```markdown
## Quick reference

| Action | Command |
|---|---|
| Search papers | `python ${HERMES_SKILL_DIR}/scripts/search.py "<query>"` |
| Download | `python ${HERMES_SKILL_DIR}/scripts/download.py <id>` |
| Extract refs | `python ${HERMES_SKILL_DIR}/scripts/citations.py <pdf>` |
```

The agent scans this first on re-entry. Saves it from re-reading the
full workflow section every time.

---

## Idiom: the platform-specific skill

```yaml
---
name: apple-reminders
platforms: [macos]
metadata:
  hermes:
    tags: [productivity, apple, reminders, macos-only]
---
```

If the skill calls `osascript` or any other macOS-only tool, declare
`platforms: [macos]`. Linux/Windows users won't see it — better than
discovering at run-time that nothing works.

---

## Idiom: the toolset-aware fallback skill

```yaml
---
name: manual-image-editing
description: |
  Use when the user wants to crop, resize, recolor, or compose images
  but the dedicated image toolset isn't available. Walks through doing
  it by hand with `ffmpeg`, `imagemagick`, or `sips` (macOS).
metadata:
  hermes:
    fallback_for_toolsets: [image]
    tags: [fallback, images, manual, ffmpeg, imagemagick]
---
```

Hidden when the `image` toolset is on (it would be redundant); visible
when off. Lets the agent help the user manually, without polluting the
skill list when the better tool is available.

---

## Anti-pattern: the "monster description"

Don't:

```yaml
description: |
  Use this skill for any image-related task including but not limited
  to: resizing, cropping, recoloring, compositing, applying filters,
  removing backgrounds, OCR, classification, segmentation, generation,
  inpainting, outpainting, super-resolution, depth estimation,
  video extraction, GIF creation, format conversion, EXIF stripping,
  steganography, metadata editing, NSFW detection, face detection,
  perceptual hashing, deduplication, ...
```

The description is supposed to be ≤1024 chars and skim-readable.
Overstuffing dilutes the trigger signal. If your skill really does
N unrelated things, split it.

---

## Anti-pattern: the cross-tool name-drop

Don't:

```markdown
## Workflow

1. Use `web_search` to find sources.
2. If `web_search` is unavailable, fall back to `browser_navigate`.
```

If the user has neither tool available, the agent will hallucinate
calls anyway. Either gate the skill (`requires_tools: [web_search]`)
or write tool-agnostic instructions ("Use whatever web-fetch tool is
available; if none is, ask the user for the data directly.").

The Hermes core handles cross-tool references in tool **schemas**
dynamically (see `model_tools.py:get_tool_definitions()`), but skill
bodies don't get that benefit — what you write is what the agent reads.

---

## Anti-pattern: hardcoding paths

Don't:

```markdown
Read the user's config from `/Users/me/.hermes/config.yaml`.
```

Use `${HERMES_SKILL_DIR}` for skill-bundled paths and `~/.hermes` (with
the literal `~`) for user state — Hermes resolves both correctly across
profiles. Or, if it's a skill-config value, declare it in
`metadata.hermes.config` and let the user set the actual path.

---

## Anti-pattern: relying on session state

Don't:

```markdown
You should remember the user's preferred chart style from earlier.
```

Skills load fresh each invocation. The agent's conversation context is
the only state. If you need persistence, use:

- `metadata.hermes.config` for user-set preferences (loaded every time).
- The memory tool (`memory_recall`/`memory_save`) for cross-session
  facts.
- Skill-bundled `references/` for static knowledge.

The skill body itself can't carry state between invocations.
