# Hermes Extensions — Deep Dive

Hermes implements the Anthropic Claude Skills spec faithfully but adds a
handful of runtime features that aren't part of the upstream spec. This
document explains each one in depth: what it does, when to use it, and
how to verify it's working.

For the **frontmatter** field reference, see `frontmatter-spec.md`. This
document is about runtime behavior — what happens when a skill loads.

---

## 1. Toolset and tool gating

### What it does

`metadata.hermes.requires_toolsets` / `requires_tools` /
`fallback_for_toolsets` / `fallback_for_tools` filter which skills are
visible to the agent based on the **current** toolset/tool availability.

### Why it matters

Hermes ships ~20 toolsets (`web`, `browser`, `terminal`, `code`, `image`,
`memory`, `youtube`, `apple_native`, etc.) and individual users enable
different subsets. Without gating:

- A skill that depends on `browser_navigate` would be visible even when
  the browser toolset is off, leading to broken workflows.
- A "manual fallback" skill (e.g., guide the agent through web scraping
  by hand) would clutter the skill list when the browser toolset is on
  and unnecessary.

### Common patterns

**Hard requirement (skill is useless without X):**
```yaml
metadata:
  hermes:
    requires_toolsets: [web]      # need HTTP at all
    requires_tools: [web_search]  # specifically need search
```

**Manual fallback (skill replaces a missing capability):**
```yaml
metadata:
  hermes:
    fallback_for_toolsets: [browser]   # only show when browser toolset is OFF
```

**Mutually exclusive — don't do both for the same toolset:**
```yaml
# WRONG — these contradict
metadata:
  hermes:
    requires_toolsets: [browser]
    fallback_for_toolsets: [browser]
```

### How to verify

Run `skills_list` in a session and toggle toolsets via `/toolsets` —
verify the skill appears/disappears as expected. Or inspect at the CLI:

```bash
hermes -p test-profile skills inspect <name>
```

If you're authoring a skill that gates on a non-existent toolset name,
the skill will simply never show. Typos here fail silently. Cross-check
toolset names against `toolsets.py` in this repo.

---

## 2. Config injection

### What it does

When a skill loads, Hermes reads `metadata.hermes.config` from the
frontmatter, resolves each key against `config.yaml`, and appends a
block like this to the skill message:

```
[Skill config (from ~/.hermes/config.yaml):
  arxiv.cache_dir = /Users/me/.cache/arxiv
  arxiv.max_results = 20
]
```

The agent sees the resolved values without ever reading `config.yaml`
itself — keeps the prompt cache valid and the skill body
deployment-portable.

### Why it matters

Skills that need user-specific paths/thresholds/preferences shouldn't
hardcode them. By declaring them in frontmatter, the user is prompted
once during setup, and every future skill invocation gets the live value.

### Storage key convention

Logical key in frontmatter is `arxiv.cache_dir`. Storage path in
`config.yaml` is `skills.config.arxiv.cache_dir`. The `skills.config.`
prefix is added automatically.

```yaml
# config.yaml
skills:
  config:
    arxiv:
      cache_dir: /Users/me/.cache/arxiv
      max_results: 20
```

### Path expansion

String values containing `~` or `${VAR}` are expanded with
`os.path.expanduser` + `os.path.expandvars` at resolution time. So:

```yaml
default: "~/work/.cache/${USER}_papers"
```

resolves to `/Users/me/work/.cache/me_papers` at load.

### Collision rule

If two skills declare the same logical key, the first one parsed wins
the `prompt` text shown to the user. Both skills receive the same value
at load. **Namespace your keys with the skill name** to avoid this:

```yaml
# Good — namespaced
config:
  - key: arxiv.cache_dir

# Risky — too generic, will collide
config:
  - key: cache_dir
```

### Implementation

`agent/skill_utils.py:extract_skill_config_vars()` parses frontmatter.
`resolve_skill_config_values()` reads `config.yaml`. Injection happens
in `agent/skill_commands.py:_inject_skill_config()`.

---

## 3. Required environment variables

### What it does

Top-level `required_environment_variables` declares env vars (API keys,
tokens) the skill needs. The setup wizard prompts the user during
`hermes setup` and `hermes skills install`. Stored in `~/.hermes/.env`.

### Field semantics

```yaml
required_environment_variables:
  - name: GITHUB_TOKEN
    prompt: GitHub personal access token (classic)
    help: Needs `repo` scope; create at github.com/settings/tokens
    required_for: PR creation and CI status checks
```

- `name` — env var name (UPPER_SNAKE_CASE). Hermes loads from
  `~/.hermes/.env` and the process environment, in that order.
- `prompt` — short label in the wizard.
- `help` — one-line hint with the get-it URL.
- `required_for` — what stops working without it. **Presence of this
  field marks the env var as required**: skill won't load without it.
  Omit to make the var optional (skill loads with a warning banner).

### Optional vs required

```yaml
# Required — skill blocks load without it
- name: STRIPE_API_KEY
  prompt: Stripe secret key
  help: Get from dashboard.stripe.com/apikeys
  required_for: payment operations

# Optional — skill loads, warns the user
- name: SLACK_WEBHOOK_URL
  prompt: Slack webhook URL for notifications
  help: Create at api.slack.com/apps
  # no `required_for` -> optional
```

### Legacy form

`prerequisites.env_vars: [...]` is the old form. Hermes auto-normalizes
it into `required_environment_variables` at load time, but new skills
should use the typed dict form.

### Implementation

`hermes_cli/setup.py` (wizard prompts), `tools/skills_tool.py` (load-time
checking), `agent/skill_commands.py` (`[Skill setup note: ...]` banner
injection when an optional var is missing).

---

## 4. Template variables

### What it does

Two tokens in the SKILL.md body are replaced at load time:

| Token | Resolves to |
|---|---|
| `${HERMES_SKILL_DIR}` | Absolute path to the skill's directory |
| `${HERMES_SESSION_ID}` | Current session ID (or left as-is if no session) |

Useful for referring to bundled assets without round-tripping through
`skill_view`:

```markdown
Run the helper:

```bash
python ${HERMES_SKILL_DIR}/scripts/extract.py input.pdf
```

Save output under `~/.hermes/sessions/${HERMES_SESSION_ID}/output/`.
```

becomes (at load):

```markdown
Run the helper:

```bash
python /Users/me/.hermes/skills/my-skill/scripts/extract.py input.pdf
```

Save output under `~/.hermes/sessions/abc123/output/`.
```

### Why it matters

Skills installed in different places (user dir vs bundled vs external
hub) have different absolute paths. Template variables let one skill
work everywhere.

### Disabling

Substitution is on by default. Disable globally with:

```yaml
# config.yaml
skills:
  template_vars: false
```

(Rare — usually you want this on.)

### Implementation

`agent/skill_commands.py:_substitute_template_vars()`. Unresolved tokens
(e.g. `${HERMES_SESSION_ID}` with no active session) are **left
in place** so the author can spot them.

### Trailing convention

By convention, end your SKILL.md with:

```markdown
[Skill directory: ${HERMES_SKILL_DIR}]
```

so the agent has a definitive marker for the skill's install location
even when it's reading the message far down a long conversation.

---

## 5. Inline shell expansion

### What it does

Snippets like `` !`date +%Y-%m-%d` `` in the SKILL.md body are replaced
with the command's stdout at load time:

```markdown
Today is !`date +%Y-%m-%d`. Use this as the default date in reports.
```

becomes:

```markdown
Today is 2026-05-06. Use this as the default date in reports.
```

### When to use

Rare. Typical cases:

- Inject the current date/time so the skill doesn't have to re-query it
- Pull a versioning number from a file (`!`cat VERSION``)
- Stamp the user's hostname

### Why it's opt-in

Inline shell is a **prompt-injection vector** if the skill comes from
an untrusted source. Disabled by default; users opt in via:

```yaml
# config.yaml
skills:
  inline_shell: true
  inline_shell_timeout: 10   # seconds, capped per snippet
```

### Constraints

- Single-line only — no newlines inside the backticks.
- Stdout is captured. Stderr is fallback if stdout is empty.
- Output capped at 4000 chars (truncated with `…[truncated]`).
- Failures return `[inline-shell error: ...]` markers, not exceptions.
- CWD is the skill directory, so relative paths in commands resolve
  the way the author expects.

### Implementation

`agent/skill_commands.py:_run_inline_shell()` and
`_expand_inline_shell()`.

---

## 6. Slash command auto-generation

### What it does

Every skill is automatically exposed as a slash command:

- In the **CLI**: `/my-skill [free-text args]`
- In **every gateway platform** (Telegram, Slack, Discord, WhatsApp, ...):
  same syntax

The slash command loads the SKILL.md content (with all the substitutions
above) as a **user message** (not a system prompt — this preserves the
prompt cache) and the agent picks up from there. Free-text args after
the command name are appended as the user's instruction.

### Behavior across surfaces

| Surface | Notes |
|---|---|
| CLI | Tab-completion populated from skill index. `/skill-name` works. |
| Telegram | Auto-registered via `BotCommands` API at gateway start. |
| Slack | Routed through `/hermes <skill-name>` subcommand mapping. |
| Discord | Same as Slack pattern. |
| Other (WhatsApp, Signal, Matrix, ...) | Plain `/skill-name` text. |

### Conflict with built-in slash commands

If a skill name collides with a built-in (`/help`, `/quit`, `/clear`,
`/resume`, etc.), the built-in wins. Avoid these names — see
`hermes_cli/commands.py:COMMAND_REGISTRY` for the canonical list.

### How to disable

Per-skill: rename the skill so it doesn't conflict, or
`/skills disable <name>`.

Globally: not exposed as a knob — slash commands are an integral part
of the skill mechanism.

### Implementation

`agent/skill_commands.py` builds the message; `cli.py` and
`gateway/run.py` route the slash command to that builder.

---

## 7. Cache-aware activation

### What it does

When a user installs/edits/disables a skill mid-session, the change does
**not** take effect immediately by default. It applies to the **next**
session.

### Why

The system prompt enumerates every available skill's metadata. Changing
that mid-session **invalidates the prompt cache** and dramatically
increases token cost on every subsequent turn. The cost spike is large
enough (often 5–10×) that opt-in is the right default.

### Opting in

Slash commands and CLI subcommands that mutate skill state accept
`--now` to invalidate the cache immediately:

```bash
# CLI
hermes skills install owner/repo/skill --now
hermes skills disable my-skill --now

# Slash command
/skills install owner/repo/skill --now
/skills uninstall my-skill --now
```

### Authoring implications

Don't write a skill that **expects** to be active immediately after
install — assume the user resets or `--now`'s. The skill body should
work regardless of session age.

### Implementation pointers

`hermes_cli/skills_hub.py` (the `--now` flag plumbing) and
`agent/prompt_builder.py` (cache invalidation).

---

## 8. Two-tier loading

### What it does

Skills are discovered from multiple directories, in this order:

1. `${HERMES_HOME}/skills/` (user dir, default `~/.hermes/skills/`)
2. `skills.external_dirs` from `config.yaml` (any number of paths)
3. The bundled `skills/` directory in this repo

Earlier entries override later entries on name collision. So a user can
override a bundled skill by dropping a same-named directory into
`~/.hermes/skills/`.

### Why

- Lets users customize bundled skills without forking the repo.
- Lets teams share a curated skill set via a shared external dir.
- Keeps `optional-skills/` opt-in (they're not in any of the loaded
  paths until `hermes skills install` copies them).

### Authoring implications

If you write a skill that wants to be the canonical version regardless
of where the user installs Hermes, ship it in `skills/` and **don't**
recommend users duplicate it in `~/.hermes/skills/`. Conversely, for a
personal skill, always put it in `~/.hermes/skills/`.

### `optional-skills/` specifics

Heavier skills (large deps, niche audiences) live in `optional-skills/`
in this repo and are **not** auto-loaded. They become available after:

```bash
hermes skills install official/<category>/<skill-name>
```

which copies them into `~/.hermes/skills/`. Use this directory for
anything heavy or specialized.

### Implementation

`agent/skill_utils.py:get_all_skills_dirs()` and
`get_external_skills_dirs()`.

---

## 9. Skill management CLI

### Commands

```bash
hermes skills browse              # paginated list of available skills
hermes skills search <query>      # full-text search
hermes skills install <id>        # install from a hub or local path
hermes skills inspect <id>        # show frontmatter + first lines of body
hermes skills list                # list installed skills
hermes skills check               # verify required env vars / config
hermes skills update [name]       # re-fetch skill from source
hermes skills audit               # find broken / stale / unused skills
hermes skills uninstall <name>    # remove from ~/.hermes/skills/
hermes skills reset <name>        # restore bundled version (or remove user override)
hermes skills publish <path>      # PR a skill to a configured hub
hermes skills snapshot export     # bundle all installed skills into one file
hermes skills snapshot import     # restore from a snapshot
hermes skills tap [list|add|rm]   # manage GitHub repos used as skill sources
```

### Slash command equivalents

All of the above also work as `/skills <subcommand>` in the chat
interface, with the same flags.

### Implementation

`hermes_cli/skills_hub.py` is the router; per-action functions are
named `do_<action>`.

---

## 10. Disabling skills

### Per-user disable

In `config.yaml`:

```yaml
skills:
  disabled:
    - never-load-this-skill
```

Or per-platform:

```yaml
skills:
  platform_disabled:
    telegram: [creative-skill, dev-only-skill]
    slack: [media-heavy-skill]
```

Or via slash command (writes the same key): `/skills disable <name>`.

### What "disabled" means

The skill is **invisible** to `skills_list`, doesn't generate a slash
command, doesn't get config-var-prompted in setup. It's effectively
deleted from the user's perspective until re-enabled. The files stay
where they are — just filtered at load.

### Implementation

`agent/skill_utils.py:get_disabled_skill_names()` reads the config; all
discovery paths funnel through this filter.

---

## 11. Skill loading as a user message (not system prompt)

### What it does

When a skill triggers (via slash command or auto-load), its content is
injected as a `user` role message, **not** added to the system prompt.

### Why

Adding to the system prompt would invalidate the prompt cache (see
"Cache-aware activation" above). Injecting as a user message is
cache-friendly — the system prompt stays stable, and only the
conversation tail changes.

### Authoring implication

The agent reads your SKILL.md as if the user just said it. So:

- Imperative voice still works ("Run X. Read Y.").
- Don't write the body as if it were system rules ("You are an arXiv
  expert.") — frame it as task instructions ("Help the user with arXiv
  research using this workflow:").
- Don't expect the agent to remember the skill across many turns —
  re-injection happens only when the slash command fires again or the
  agent calls `skill_view`.

---

## 12. Namespaced skills (plugin-provided)

### What it does

Plugins can ship skills under their own namespace, written as
`<namespace>:<skill-name>`. The slash command becomes
`/<namespace>:<skill-name>`. This avoids collisions when multiple
plugins ship same-named skills.

```yaml
# In a plugin's skill
name: github:pr-review
```

### When to use

Only if you're authoring a Hermes plugin. Standalone skills (in
`skills/`, `~/.hermes/skills/`) don't need a namespace.

### Implementation

`agent/skill_utils.py:parse_qualified_name()` and `is_valid_namespace()`.

---

## Quick verification checklist

For any skill you author, run these to confirm the runtime features
work:

```bash
# 1. Frontmatter parses cleanly
python -c "
from agent.skill_utils import parse_frontmatter
from pathlib import Path
fm, body = parse_frontmatter(Path('skills/<cat>/<name>/SKILL.md').read_text())
print(fm)
"

# 2. Skill is visible in skills_list
hermes skills list | grep <name>

# 3. Frontmatter inspector shows everything
hermes skills inspect <name>

# 4. Required env vars / config keys are honored
hermes skills check <name>

# 5. (If applicable) Toolset gating works
#    Toggle the toolset via /toolsets in CLI and re-run /skills
```
