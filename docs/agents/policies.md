# Important Policies

## Prompt Caching Must Not Break

Hermes-Agent ensures caching remains valid throughout a conversation. **Do NOT implement changes that would:**
- Alter past context mid-conversation
- Change toolsets mid-conversation
- Reload memories or rebuild system prompts mid-conversation

Cache-breaking forces dramatically higher costs. The ONLY time we alter context is during context compression.

Slash commands that mutate system-prompt state (skills, tools, memory, etc.)
must be **cache-aware**: default to deferred invalidation (change takes
effect next session), with an opt-in `--now` flag for immediate
invalidation. See `/skills install --now` for the canonical pattern.

## Context-file priority is first-found-wins, not layered merge

`build_context_files_prompt()` in `agent/prompt_builder.py` loads exactly
**one** project context source per session, in this order:

1. `.hermes.md` / `HERMES.md` (walks to git root)
2. `AGENTS.md` / `agents.md` (cwd only)
3. `CLAUDE.md` / `claude.md` (cwd only)
4. `.cursorrules` + `.cursor/rules/*.mdc` (cwd only)

The first match wins — items lower in the list are **not** appended.
`SOUL.md` from `HERMES_HOME` is independent and always included (loaded
separately as the identity slot via `load_soul_md()`).

`SubdirectoryHintTracker` (`agent/subdirectory_hints.py`) is the second
mechanism. When the agent navigates into a subdirectory via a tool call,
the tracker reads that subdirectory's `AGENTS.md` / `CLAUDE.md` /
`.cursorrules` and **appends** the content to the tool result. It does
not replace the root context, and it does not mutate the system prompt
(preserves prompt caching). Each subdirectory is read at most once per
session.

Two implications for designers:

- Do not assume a child's `AGENTS.md` "overrides" the root's at startup.
  At startup only the highest-priority root file is loaded; the child
  hint only arrives the first time the agent touches that subtree.
- All context files (startup and subdirectory) are run through
  `_scan_context_content()` for prompt-injection patterns and capped at
  `CONTEXT_FILE_MAX_CHARS` (currently 20,000). New context sources must
  reuse these helpers — don't roll your own.

## Background Process Notifications (Gateway)

When `terminal(background=true, notify_on_complete=true)` is used, the gateway runs a watcher that
detects process completion and triggers a new agent turn. Control verbosity of background process
messages with `display.background_process_notifications`
in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS` env var):

- `all` — running-output updates + final message (default)
- `result` — only the final completion message
- `error` — only the final message when exit code != 0
- `off` — no watcher messages at all
