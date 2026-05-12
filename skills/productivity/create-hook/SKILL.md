---
name: create-hook
description: Use when the user wants to create a shell hook, scaffold a hook script, automate a repeated action via `hermes hooks new`, ask "why am I doing this every turn", or whenever you notice yourself executing the same deterministic side-effect three or more times in a session (formatting, staging, blocking dangerous commands, injecting cwd context, etc.). Triggers on "write a hook", "create a hook", "every time", "always", "I keep doing", "automate this", "stop me from", "block this", "hook to". Pairs with `hermes hooks suggest` (mine recent sessions for repeat patterns) and `hermes hooks new` (scaffold from a starter template).
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [hooks, automation, scaffolding, guardrails, productivity, hermes-internals]
    related_skills: [create-skill, hermes-self-audit]
---

# Create Hook

A guide for **proposing and scaffolding shell hooks** in Hermes. The other
extension authoring guide (`create-skill`) covers skill packaging; this one
covers the deterministic side of automation — things that should happen
every time without LLM judgement.

## When to propose a hook (decision rule)

Hooks are the right tool when **all four** are true:

1. The action is **deterministic** — same input, same output, no judgement.
2. The action repeats **3+ times** in a single session, or **across multiple
   sessions** (use `hermes hooks suggest` to verify).
3. The action is **bounded** — runs in <5s and writes to one or two known
   files, or just prints JSON.
4. The action's **trigger is observable** — a tool name + arg shape that
   matches a Hermes hook event (see "Lifecycle events" below).

If even one is false, **don't propose a hook**. Suggest a skill, a slash
command, or a `~/.hermes/config.yaml` setting instead. See
`docs/agents/extension-decision.md` for the broader decision tree.

## Detection: when to bring it up

You'll see hook-candidate signals two ways:

1. **In-session reminder.** Hermes' `HookHinter` (see `agent/hook_hinter.py`)
   watches your tool calls and appends a one-shot system note like
   `[hermes hook hint] You have now invoked terminal (black) 3 times...`
   to a tool result. The reminder includes a copy-pasteable scaffold
   command — when a bundled starter template fits (e.g. `.py`/`.yaml`
   writes, `black`/`ruff`/`rm`/`git push --force` terminals) the hint
   gives `hermes hooks new --from-template <name>`; otherwise it falls
   back to `--event ... --matcher ...`. When you see this hint, **surface it to the user
   exactly once**, then continue the task. Do not repeat the hint in later
   turns even if you see the same fingerprint again — the system ratchets
   delivery to one shot per fingerprint per session on purpose.
2. **Offline mining.** When the user asks "what hooks should I add?",
   run `hermes hooks suggest --lookback-hours 168 --threshold 5` to
   mine `~/.hermes/sessions/session_*.json` for repeated tool
   fingerprints. Add `--with-llm` for matcher refinement and a rationale
   (off by default — costs an auxiliary LLM call with a 90s wall
   timeout; on timeout it falls back silently to frequency-only output).

## Lifecycle events (which event to choose)

| Event | Fires | What it can do | Common use |
|---|---|---|---|
| `pre_tool_call` | before a tool dispatch | **Block** the call with `{"decision": "block", "reason": "..."}` | Guardrails: block `rm -rf /`, `.env` writes, force-push to main |
| `post_tool_call` | after a tool dispatch | Observer-only — disk-side effects (format, stage, log) | Run `black` after `.py` writes, `git add` written files |
| `pre_llm_call` | before each LLM API call | Inject context via `{"context": "..."}` | Prepend `git status` to the next turn |
| `subagent_stop` | when a child subagent finishes | Observer-only | Slack alert on subagent failure |
| `on_session_start` / `on_session_end` | session lifecycle | Observer-only | Init env, snapshot final state |

> **Important:** `post_tool_call` **cannot** rewrite the tool result the
> agent sees — only `pre_tool_call` can block and only `pre_llm_call` can
> inject context. If you want a formatter's errors to reach the agent,
> pair a `post_tool_call` hook that stashes the report to a file with a
> `pre_llm_call` hook that injects the stash.

## Wire protocol

Every hook reads a JSON payload on stdin and writes a JSON response on
stdout. Both shapes are accepted for `pre_tool_call` blocking:

```jsonc
{"decision": "block", "reason":  "..."}    // Claude-Code-style
{"action":   "block", "message": "..."}    // Hermes-canonical
```

For `pre_llm_call`, return `{"context": "..."}`. Empty / non-matching
output is a silent no-op. Stderr is logged but ignored for routing.

The script's stdin payload always contains:

```jsonc
{
  "hook_event_name": "pre_tool_call",
  "tool_name":       "terminal",
  "tool_input":      {"command": "rm -rf /"},  // event-specific
  "session_id":      "sess_abc123",
  "cwd":             "/path/to/cwd",
  "extra":           {...}                     // event-specific kwargs
}
```

## Scaffolding workflow

When you've decided a hook is the right answer, scaffold it through the
deterministic CLI rather than writing the YAML and shell script by hand:

```bash
# Interactive picker
hermes hooks new

# Or pick a template directly
hermes hooks new --from-template block-env-write

# Custom (no template)
hermes hooks new --event pre_tool_call --matcher "terminal" --name my-guard
```

`hermes hooks new` will:

1. Copy a starter script from `scripts/agent-hooks-examples/` into
   `~/.hermes/agent-hooks/<name>.sh` with executable bit set.
2. Append a properly indented `hooks:` block entry to
   `~/.hermes/config.yaml` (text-level append — preserves comments).
3. Run `hermes hooks doctor` for the new entry (exec bit, allowlist,
   mtime, JSON validity).
4. Tell the user how to flip the first-use allowlist (`--accept-hooks`
   flag, `HERMES_ACCEPT_HOOKS=1` env var, or `hooks_auto_accept: true`).

After every scaffold, **always tell the user to restart Hermes** (or the
gateway) so the new hook registers on the plugin manager.

## Starter templates (and what to crib from them)

All six live in `scripts/agent-hooks-examples/`:

| Template | Event | Purpose |
|---|---|---|
| `block-rm-rf` | `pre_tool_call` | Guard pattern: regex-match the arg, return `{"decision": "block"}` |
| `block-env-write` | `pre_tool_call` | Multi-tool matcher (`write_file\|patch\|terminal`), single regex for paths |
| `block-force-push-main` | `pre_tool_call` | Two-stage match: command verb check, then protected-branch check |
| `auto-format` | `post_tool_call` | Observer pattern: read path, side-effect on disk, return `{}` |
| `auto-stage-on-write` | `post_tool_call` | Disk-side observer with no agent feedback path |
| `inject-cwd-context` | `pre_llm_call` | Context injection: emit `{"context": "..."}` |

Crib the closest one when authoring a new hook. The set/setops in
`scripts/agent-hooks-examples/block-env-write.sh` is a canonical idiom
for matching against multiple tool kinds in a single script.

## Common pitfalls

- **Don't put a matcher on `pre_llm_call`.** Matchers are only honoured
  for `pre_tool_call` and `post_tool_call`. Other events ignore the field.
- **Don't expect `post_tool_call` to modify what the agent sees.** It can
  format the file on disk but not the tool result text. See the warning
  above about `pre_llm_call` pairing.
- **Don't commit secrets to the script.** Read them from env vars or a
  protected file path — hook scripts get logged on stderr capture.
- **Don't write hooks that take >5s.** Slow hooks block tool dispatch.
  Use `timeout:` in the YAML config as a backstop; raise an issue if you
  need >300s (the current hard ceiling).
- **Don't bypass the allowlist.** First-use consent is a security
  feature, not a nuisance. Document the `--accept-hooks` escape hatch
  for CI use only.

## Discovery commands cheat sheet

```bash
hermes hooks list                  # what's wired up + allowlist status
hermes hooks doctor                # exec bit, allowlist, JSON validity
hermes hooks test pre_tool_call --for-tool terminal   # synthetic firing
hermes hooks suggest                                  # mine sessions, frequency only
hermes hooks suggest --with-llm                       # + rationale
hermes hooks suggest --include-all                    # include observation tools
hermes hooks revoke "<command path>"                  # forget allowlist consent
```

## Reading next

- `docs/agents/extension-decision.md` — when a hook beats a skill / MCP / subagent
- `website/docs/user-guide/features/hooks.md` — full schema and security model
- `scripts/agent-hooks-examples/README.md` — template index with one-liners
- `agent/hook_hinter.py` — how in-session detection works (helpful if a
  hint isn't firing or fires too often)
