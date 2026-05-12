# Agent Hook Starter Recipes

Ready-to-use shell hooks that demonstrate the six highest-value patterns
called out in `website/docs/user-guide/features/hooks.md`. Each script
doubles as a template for `hermes hooks new`.

### Guardrails (`pre_tool_call`, can block)

| Script | Matcher | What it does |
|--------|---------|--------------|
| `block-rm-rf.sh` | `terminal` | Blocks `rm -rf /` and `rm -rf /<critical>` (etc, usr, var, bin, ...). Leaves `/tmp` and relative paths alone. |
| `block-env-write.sh` | `write_file\|patch\|terminal` | Blocks any write that targets `.env*`, `~/.aws/credentials`, `~/.ssh/id_*`, `*.pem/*.key`. |
| `block-force-push-main.sh` | `terminal` | Refuses `git push --force` / `-f` to `main`, `master`, `release/*`, `prod`, `production`. |

### Side-effects (`post_tool_call`, observer-only — see notes below)

| Script | Matcher | What it does |
|--------|---------|--------------|
| `auto-format.sh` | `write_file\|patch` | Reformats files in-place: `.py` via `black --quiet`, `.yaml`/`.yml` via `yamlfmt`. Silently skips if the formatter isn't installed. |
| `auto-stage-on-write.sh` | `write_file\|patch` | `git add`s the file the agent just wrote so the diff is queued up for review. |

### Context injection (`pre_llm_call`)

| Script | Matcher | What it does |
|--------|---------|--------------|
| `inject-cwd-context.sh` | _(none — fires every turn)_ | If `git status --porcelain` is dirty, prepends it to the next LLM turn via `{"context": "..."}`. |

> **Important:** `post_tool_call` hooks **cannot** rewrite the tool result the
> agent sees — `_parse_response` only honours `{"context": "..."}` on
> `pre_llm_call` and block decisions on `pre_tool_call`. Use `post_tool_call`
> for disk-side effects (format/stage/notify) and pair them with a
> `pre_llm_call` injector if you need the agent to learn about the result.

## How to enable

### Quickest: `hermes hooks new` (interactive)

```bash
hermes hooks new --from-template block-env-write
```

This copies the chosen template into `~/.hermes/agent-hooks/`, patches
`~/.hermes/config.yaml`, runs `hermes hooks doctor`, and queues an allowlist
prompt for the next CLI launch.

### Manual

1. Copy or symlink the scripts into your hook directory (any path works
   — `~/.hermes/agent-hooks/` is the convention in the docs):

   ```bash
   mkdir -p ~/.hermes/agent-hooks
   cp scripts/agent-hooks-examples/*.sh ~/.hermes/agent-hooks/
   chmod +x ~/.hermes/agent-hooks/*.sh
   ```

2. Add a `hooks:` block to `~/.hermes/config.yaml` pointing at the
   scripts (see the commented example in `cli-config.yaml.example`).

3. The first time each `(event, command)` pair fires, Hermes prompts for
   consent and persists it to `~/.hermes/shell-hooks-allowlist.json`.
   Non-TTY runs need `--accept-hooks`, `HERMES_ACCEPT_HOOKS=1`, or
   `hooks_auto_accept: true`.

4. Inspect with `hermes hooks list` and `hermes hooks doctor`.

## Wire-protocol summary

Each script reads a JSON payload on stdin and writes a JSON response on
stdout. Both shapes are accepted for `pre_tool_call` decisions:

```jsonc
{"decision": "block", "reason":  "..."}    // Claude-Code style
{"action":   "block", "message": "..."}    // Hermes-canonical
```

For `pre_llm_call`, return `{"context": "..."}` to prepend text to the
next user turn. Empty / non-matching output is a silent no-op.

See `website/docs/user-guide/features/hooks.md` for the full schema,
event list, security model, and consent escape-hatches.
