# Pre-Publish Checklist

Walk through this before declaring a skill done. Each item has a
verification step you can actually run, not just a vague "make sure".

---

## 1. Frontmatter is well-formed

- [ ] File starts with `---` on the very first line (no BOM, no leading
      whitespace).
- [ ] YAML frontmatter ends with a `---` line followed by a blank line.
- [ ] `name` is lowercase, hyphen-separated, ≤64 chars, matches the
      skill directory name.
- [ ] `description` is present and ≤1024 chars.

**Verify:**

```bash
python -c "
from agent.skill_utils import parse_frontmatter
from pathlib import Path
p = Path('skills/<category>/<name>/SKILL.md')
fm, _ = parse_frontmatter(p.read_text())
assert fm.get('name'), 'missing name'
assert fm.get('description'), 'missing description'
assert len(fm['name']) <= 64, f'name too long: {len(fm[\"name\"])}'
assert len(fm['description']) <= 1024, f'description too long: {len(fm[\"description\"])}'
print('OK:', fm['name'])
"
```

---

## 2. Description is "trigger-shaped"

- [ ] Starts with `Use when ...` (or equivalent — `Triggers when`,
      `Invoke when`).
- [ ] Lists at least 2-3 concrete user phrases the agent should match on.
- [ ] Includes the "even if they don't explicitly say X" pushy clause
      for non-obvious triggers.
- [ ] Describes both **what** the skill does AND **when** to use it.

**No mechanical verifier** — but ask yourself: would an agent reading
this in a list of 50 skill descriptions know precisely when to load this
one?

---

## 3. Body fits the progressive-disclosure budget

- [ ] SKILL.md body is ≤500 lines (after the frontmatter).
- [ ] If you need more, add a `references/<topic>.md` with a clear
      pointer in the body ("For the full schema, read
      `references/schema.md`.").
- [ ] Large reference files (>300 lines) include a table of contents.

**Verify:**

```bash
wc -l skills/<category>/<name>/SKILL.md
# 500-ish lines or fewer including frontmatter is the soft target.
```

---

## 4. Hermes extensions are correctly typed

- [ ] `metadata.hermes` is a dict (not a string from a malformed YAML).
- [ ] `tags`, `related_skills`, `requires_toolsets`, `requires_tools`,
      `fallback_for_toolsets`, `fallback_for_tools` are all **lists**
      (even single-item ones — `tags: [single]`, not `tags: single`).
- [ ] `metadata.hermes.config` entries each have `key` and `description`.
- [ ] No collision between `requires_*` and `fallback_for_*` for the
      same toolset/tool.

**Verify:**

```bash
hermes skills inspect <name>
# Output should show every field you declared. If a field is missing,
# it failed to parse.
```

---

## 5. Required env vars and config are declared correctly

- [ ] Each `required_environment_variables` entry has `name`, `prompt`,
      `help`. Has `required_for` only if the skill **truly** breaks
      without it.
- [ ] Each `metadata.hermes.config` entry has a namespaced `key`
      (e.g. `arxiv.cache_dir`, not bare `cache_dir`) — see the
      collision rule.
- [ ] Default values are sensible — would the skill work on a fresh
      install with just the defaults?

**Verify:**

```bash
hermes skills check <name>
# Reports any missing required env vars / config keys.
```

---

## 6. Cross-references are valid

- [ ] Every name in `related_skills` actually exists.
- [ ] Every toolset name in `requires_toolsets` / `fallback_for_toolsets`
      exists in `toolsets.py`.
- [ ] Every tool name in `requires_tools` / `fallback_for_tools` is a
      real registered tool.

**Verify:**

```bash
# 1. Check skills
hermes skills list | awk '{print $1}'
# Compare with your related_skills list.

# 2. Check toolsets
python -c "from toolsets import _ALL_TOOLSETS; print(sorted(_ALL_TOOLSETS))"

# 3. Check tools
python -c "
from tools.registry import registry
import model_tools  # triggers tool discovery
print(sorted(registry.tools_by_name()))
"
```

Typos here **silently disable the skill** (it's gated on a non-existent
condition that's never satisfied).

---

## 7. Bundled assets are referenced correctly

- [ ] All `${HERMES_SKILL_DIR}/...` paths point to files that exist.
- [ ] Helper scripts under `scripts/` are executable / runnable.
- [ ] `references/` files are referenced by name in the body when
      relevant ("For X, read `references/x.md`.").
- [ ] Templates / assets paths use `${HERMES_SKILL_DIR}/`, not
      hardcoded absolute paths.

**Verify:**

```bash
# Find every ${HERMES_SKILL_DIR}/... reference and check the path exists.
SKILL_DIR=skills/<category>/<name>
grep -oE '\$\{HERMES_SKILL_DIR\}/[a-zA-Z0-9_./-]+' "$SKILL_DIR/SKILL.md" | \
  sort -u | \
  sed "s|\${HERMES_SKILL_DIR}|$SKILL_DIR|" | \
  while read p; do [ -e "$p" ] || echo "MISSING: $p"; done
```

---

## 8. Slash command works

- [ ] `/<skill-name>` is not a built-in (`/help`, `/quit`, `/clear`,
      `/resume`, `/copy`, `/paste`, etc.) — see
      `hermes_cli/commands.py:COMMAND_REGISTRY`.
- [ ] Slash command loads cleanly with no template-substitution errors.

**Verify:**

```bash
# In an interactive session:
hermes
# Then type:
/<skill-name>
# The skill content should appear as a system-style message before the
# agent's reply.
```

---

## 9. Platform gating is honored

- [ ] If the skill calls macOS-only tools (`osascript`, `pbcopy`, ...),
      `platforms: [macos]` is set.
- [ ] If the skill calls Linux-only tools (`xdotool`, `xclip`, ...),
      `platforms: [linux]` is set.
- [ ] If platform-agnostic, omit `platforms` entirely (do not list all
      three explicitly — the spec defines absent = all).

**Verify:**

```bash
python -c "
from agent.skill_utils import skill_matches_platform, parse_frontmatter
from pathlib import Path
fm, _ = parse_frontmatter(Path('skills/<cat>/<name>/SKILL.md').read_text())
print('platforms:', fm.get('platforms', '[all]'))
print('matches current OS:', skill_matches_platform(fm))
"
```

---

## 10. Author + license + version

- [ ] `author` is set (your name, your team, or `Hermes Agent`).
- [ ] `license` is set — `MIT` is the repo default; if you ship a skill
      with a different license, justify it.
- [ ] `version` follows semver (`1.0.0`, `1.2.3`, ...).

---

## 11. Test the trigger

- [ ] Write 2-3 realistic test prompts (not abstract — concrete with
      file names, jargon, casual speech).
- [ ] Include 1-2 near-miss negatives — prompts that share keywords but
      should NOT trigger.
- [ ] Run them in a fresh session and confirm the agent loads the skill
      on the positives and skips it on the negatives.

For deep eval pipelines (subagents, blind comparison, automated
description tuning), defer to Anthropic's `skill-creator`:

```bash
hermes skills install anthropics/skills/skills/skill-creator
```

---

## 12. The skill is in the right location

- [ ] **Personal / experimental** → `~/.hermes/skills/<name>/`. Don't
      commit to the repo.
- [ ] **Broadly useful, lightweight deps** → `skills/<category>/<name>/`
      in the repo. Pick an existing category folder.
- [ ] **Heavy deps or niche** → `optional-skills/<category>/<name>/`
      in the repo. Users install explicitly.
- [ ] Category folder exists already — don't invent new ones without
      checking what's there.

**Verify:**

```bash
ls skills/   # canonical list of categories
ls optional-skills/
```

---

## 13. License compatibility check

- [ ] Skill content is your own work, or attributed (with `author:`
      including the source).
- [ ] Bundled scripts respect their upstream licenses.
- [ ] No copyrighted content (song lyrics, brand assets, etc.) bundled
      without permission.
- [ ] If adapting an existing skill (Anthropic's, a community one), the
      `author` field credits both you AND the original
      (`author: You (adapted from upstream/skill)`).

---

## 14. Run it through the full sanity check

```bash
SKILL=<category>/<name>

# Parses
python -c "
from agent.skill_utils import parse_frontmatter
from pathlib import Path
fm, body = parse_frontmatter(Path(f'skills/$SKILL/SKILL.md').read_text())
print('name:', fm.get('name'))
print('description chars:', len(fm.get('description', '')))
print('body lines:', len(body.splitlines()))
"

# Loads
hermes skills inspect $(basename $SKILL)

# Visible to discovery
hermes skills list | grep $(basename $SKILL)

# Required deps satisfied
hermes skills check $(basename $SKILL)
```

If all four succeed, the skill is technically correct. The remaining
quality is in the writing — does the agent actually do the right thing
when it loads?

---

## 15. (Optional) Description optimization

For a skill that will be used heavily, run the description optimizer
from Anthropic's `skill-creator` to tune the description against a set
of trigger evals:

```bash
hermes skills install anthropics/skills/skills/skill-creator
# then in a session:
/skill-creator
# tell it to optimize the description for <name>
```

The optimizer splits ~20 trigger eval queries into train/test, iterates
on the description with up to 5 rewrites, and picks the version that
scores best on the held-out test set.

---

## 16. Publishing

If the skill is broadly useful, publish it:

- **To this repo** — open a PR adding to `skills/` or `optional-skills/`.
  Follow `CONTRIBUTING.md`.
- **To a hub repo via PR** — `hermes skills publish <path> --repo
  owner/repo`. Hermes opens a PR for you.
- **As a snapshot** — `hermes skills snapshot export bundle.tar.gz`,
  share the file, recipient runs `hermes skills snapshot import bundle.tar.gz`.

For repo PRs, run the test suite first:

```bash
scripts/run_tests.sh tests/agent/  # skill-related tests live here
```
