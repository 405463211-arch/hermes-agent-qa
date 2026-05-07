# Frontmatter Specification

Complete reference for every YAML frontmatter field a Hermes skill can use.
Fields are grouped by tier:

1. **Anthropic-spec required** — must be present
2. **Anthropic-spec optional** — recognized by both Hermes and Claude Code
3. **Hermes extensions** — Hermes-only; ignored (silently passed through) by
   Claude Code

The frontmatter is delimited by `---` lines at the very top of `SKILL.md`.

```yaml
---
# fields go here
---

# Skill body starts here
```

---

## Tier 1 — Required

### `name` (string, ≤64 chars)

Lowercase, hyphen-separated identifier. Must match the skill directory name.

```yaml
name: my-skill
```

Used for:
- Slash command — auto-generated as `/my-skill` in CLI and gateway
- Index lookup — what `hermes skills list` and `skills_list` show
- Cross-references — what `metadata.hermes.related_skills` points to

### `description` (string, ≤1024 chars)

The single most important field. This is the **primary triggering signal**
for the agent. Read `../SKILL.md` "Write the frontmatter" section for the
philosophy; the short version:

- Start with `Use when ...` so pattern matching latches.
- Include both **what it does** and **specific trigger phrases**.
- Be slightly pushy — models undertrigger by default.

```yaml
description: |
  Use when the user wants to convert structured spreadsheet data into a
  Word document with a specific layout. Triggers on phrases like "make
  a doc from this xlsx", "format the spreadsheet as a report", or any
  mention of converting tabular data to prose. Make sure to invoke this
  skill whenever the user mentions xlsx-to-docx conversion, even if
  they don't use the word "skill".
```

---

## Tier 2 — Anthropic-spec optional

### `version` (string)

Semver-style. Used for change tracking and the `hermes skills audit`
command. Default omitted is fine for prototypes.

```yaml
version: 1.2.0
```

### `author` (string)

Free text. Useful when publishing to a hub.

```yaml
author: Your Name <you@example.com>
```

### `license` (string)

SPDX identifier (e.g. `MIT`, `Apache-2.0`, `CC0-1.0`). Defaults to MIT for
skills shipped in this repo.

```yaml
license: MIT
```

### `compatibility` (string)

Free-text human-readable note about runtime requirements. Mostly used by
external hubs for display.

```yaml
compatibility: Requires Python 3.11+ and pandoc on PATH.
```

---

## Tier 3 — Hermes extensions

All under `metadata.hermes.*` (so Claude Code happily ignores them) plus
two top-level keys (`platforms`, `required_environment_variables`) that
predate the metadata namespace.

### `platforms` (list of strings)

OS gating. Skills are silently skipped on non-matching platforms.

```yaml
platforms: [macos]                # macOS only
platforms: [macos, linux]         # not Windows
platforms: [linux, windows]       # not macOS
```

Valid values: `macos`, `linux`, `windows`. Omit the field to load on
every platform (default).

Implementation: `agent/skill_utils.py:skill_matches_platform()`.

### `required_environment_variables` (list of dicts)

Declares env vars the skill needs. Hermes' setup wizard prompts the user
during `hermes setup` and `hermes skills install`.

```yaml
required_environment_variables:
  - name: OPENROUTER_API_KEY
    prompt: OpenRouter API key
    help: Get one at https://openrouter.ai/keys
    required_for: model routing and fallback
  - name: GITHUB_TOKEN
    prompt: GitHub personal access token (classic)
    help: Needs `repo` and `workflow` scopes
    required_for: PR creation and CI status checks
```

Field meaning:
- `name` — the env var name (UPPER_SNAKE_CASE)
- `prompt` — short label shown in the setup wizard
- `help` — one-line hint on where/how to get the value
- `required_for` — what stops working if the var is unset (the wizard
  shows this so users decide whether to skip)

If `required_for` is set, the var becomes truly required (skill won't
load without it). Without `required_for`, the var is recommended but
optional — the skill loads with a `[Skill setup note: ...]` warning.

### `metadata.hermes.tags` (list of strings)

Searchable tags. Used by `hermes skills search`, `skills_list` filtering,
and the description optimizer.

```yaml
metadata:
  hermes:
    tags: [productivity, documents, conversion, office]
```

No formal taxonomy — pick what reads naturally. Lowercase preferred.

### `metadata.hermes.related_skills` (list of strings)

Cross-references. The agent sees these in `skills_list` output and may
auto-load related skills together.

```yaml
metadata:
  hermes:
    related_skills: [google-workspace, ocr-and-documents, nano-pdf]
```

Use the bare skill name (no category prefix). Typos here are silently
ignored, so verify with `hermes skills list`.

### `metadata.hermes.requires_toolsets` (list of strings)

Whitelist — skill is hidden unless **all** listed toolsets are active.

```yaml
metadata:
  hermes:
    requires_toolsets: [browser]   # only show when browser toolset is on
```

Common toolset names: `web`, `browser`, `terminal`, `code`, `image`,
`memory`, `youtube`, `apple_native`. The full list is in `toolsets.py`.

### `metadata.hermes.requires_tools` (list of strings)

Stricter version of the above — gates on individual tools, not toolsets.

```yaml
metadata:
  hermes:
    requires_tools: [web_search, web_extract]
```

Useful when one toolset is partially available (e.g., `web_search` works
but `web_extract` requires an API key the user doesn't have).

### `metadata.hermes.fallback_for_toolsets` (list of strings)

Inverse of `requires_toolsets`. Skill is **hidden** when any listed
toolset is active — typically used for "manual fallback" skills that
guide the agent through doing something by hand when the dedicated
toolset isn't available.

```yaml
metadata:
  hermes:
    fallback_for_toolsets: [browser]   # hide when browser toolset is active
```

Example: a `manual-web-scraping` skill is useless when the `browser`
toolset is on, so it declares `fallback_for_toolsets: [browser]`.

### `metadata.hermes.fallback_for_tools` (list of strings)

Same idea, gating on individual tools.

```yaml
metadata:
  hermes:
    fallback_for_tools: [browser_navigate]
```

### `metadata.hermes.config` (list of dicts)

Declares `config.yaml` settings the skill needs. The setup wizard prompts
the user during install. At skill-load time, the resolved values are
injected into the agent's context as a `[Skill config: ...]` block, so
the skill body can refer to them without the agent reading config.yaml.

```yaml
metadata:
  hermes:
    config:
      - key: arxiv.cache_dir
        description: Where to cache downloaded papers
        default: "~/.cache/arxiv"
        prompt: arXiv cache directory
      - key: arxiv.user_agent
        description: User-Agent string for arXiv API requests
        default: "hermes-agent/0.1"
```

Field meaning:
- `key` — logical key (no prefix). Storage key is automatically
  `skills.config.<key>` in `config.yaml`.
- `description` — what the setting controls (shown in `hermes setup`).
- `default` — fallback when the user hasn't set it.
- `prompt` — wizard prompt label (defaults to `description` if omitted).

**Path values** with `~` or `${VAR}` are auto-expanded at resolution time.

**Collision rule** — if two skills declare the same `key`, the first one
loaded wins; the wizard only prompts once. Namespace your keys with the
skill name (e.g. `arxiv.cache_dir`, not `cache_dir`).

Implementation: `agent/skill_utils.py:extract_skill_config_vars()` and
`resolve_skill_config_values()`.

### `prerequisites.env_vars` (list of strings) — legacy

Old-style env var declaration. Hermes auto-normalizes this into
`required_environment_variables` at load time, so it still works, but
new skills should use the typed dict form.

```yaml
prerequisites:
  env_vars: [OPENAI_API_KEY]   # legacy — equivalent to required_environment_variables
```

### `prerequisites.commands` (list of strings) — advisory

Lists CLI binaries the skill expects on `PATH`. This is **advisory** —
Hermes doesn't actually check `PATH`. It's surfaced in `hermes skills
inspect` output for the user's information.

```yaml
prerequisites:
  commands: [pandoc, ffmpeg]
```

---

## Field interaction notes

- `requires_toolsets` and `fallback_for_toolsets` are mutually exclusive
  in spirit. Don't both whitelist and blacklist the same toolset.
- A skill with `required_environment_variables` (with `required_for`)
  **and** the env var unset will load with a setup-warning banner —
  the agent is told the skill is degraded.
- `platforms: [macos]` on Linux means the skill is invisible to
  `skills_list` entirely, not just degraded.
- Disabled skills (via `skills.disabled` in config.yaml) are also
  invisible. The user disables a skill by name with `/skills disable
  <name>` or in `config.yaml`.

---

## Worked example — every Hermes feature in one frontmatter

Reference example (don't copy verbatim — adapt to your skill):

```yaml
---
name: arxiv-research
description: |
  Use when the user wants to find, read, summarize, or compare academic
  papers on arXiv. Triggers on "look up the X paper", "summarize this
  preprint", "what's the latest on Y in arXiv", or any mention of an
  arXiv ID/URL. Use this even when the user doesn't explicitly say
  "arXiv" — research-paper queries default here unless they specify
  another source.
version: 2.1.0
author: Hermes Agent
license: MIT
compatibility: Requires `arxiv` Python package and pandoc.

platforms: [macos, linux]

required_environment_variables:
  - name: SEMANTIC_SCHOLAR_API_KEY
    prompt: Semantic Scholar API key (optional but recommended)
    help: Get a free key at https://www.semanticscholar.org/product/api
    required_for: citation graph traversal

prerequisites:
  commands: [pandoc]

metadata:
  hermes:
    tags: [research, papers, academic, arxiv, science]
    related_skills: [research-paper-writing, blogwatcher]
    requires_toolsets: [web]
    fallback_for_tools: [browser_navigate]
    config:
      - key: arxiv.cache_dir
        description: Where to cache downloaded papers
        default: "~/.cache/arxiv"
        prompt: arXiv cache directory
      - key: arxiv.max_results
        description: Default page size for arXiv search
        default: 20
        prompt: Default arXiv search page size
---

# arXiv Research

...body...
```

This frontmatter would:

1. Show the skill on macOS and Linux only (Windows users see nothing).
2. Prompt the user for `SEMANTIC_SCHOLAR_API_KEY` during install — but
   not block install if they skip it.
3. Note that `pandoc` should be on `PATH` (advisory).
4. Make the skill searchable by tags `research`, `papers`, etc.
5. Link to `research-paper-writing` and `blogwatcher` for related-skill
   discovery.
6. Hide the skill when the `web` toolset is **off** (no point — needs HTTP).
7. Hide the skill when `browser_navigate` **is** available (the browser
   tool handles it directly; this skill is the fallback path).
8. Prompt the user during install for `arxiv.cache_dir` and
   `arxiv.max_results`, store under `skills.config.arxiv.*` in
   `config.yaml`, and inject the resolved values into the agent's
   context every time the skill loads.

---

## Implementation pointers (for skill authors digging in)

- Frontmatter parser: `agent/skill_utils.py:parse_frontmatter()` — uses
  `yaml.CSafeLoader` with a fallback to simple `key: value` parsing.
- Schema description: `tools/skills_tool.py` (top of file, docstring)
  enumerates the agentskills.io-compatible field set.
- Setup-wizard prompts: `hermes_cli/setup.py` reads
  `required_environment_variables` and `metadata.hermes.config`.
- Config injection at load time: `agent/skill_commands.py:_inject_skill_config()`.
- Template-variable substitution: `agent/skill_commands.py:_substitute_template_vars()`.
