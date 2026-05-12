# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

This file is the **load-bearing instructions** — kept short on purpose so it stays
in working memory. Detail lives in `docs/agents/`. Read those files only when
your task touches that area.

## Development Environment

```bash
# Prefer .venv; fall back to venv if that's what your checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv` first, then `venv`, then
`$HOME/.hermes/hermes-agent/venv`.

## Hard Rules (do not violate)

### Profile-safe code (DO NOT hardcode `~/.hermes` paths)

- **Use `get_hermes_home()`** from `hermes_constants` for any read/write of HERMES_HOME state. NEVER hardcode `~/.hermes` or `Path.home() / ".hermes"`.
- **Use `display_hermes_home()`** for user-facing print/log messages.
- Hardcoding `~/.hermes` breaks profiles (PR #3575 fixed 5 such bugs).
- Tests that mock `Path.home()` must also set `HERMES_HOME` env var.
- Profile ops (`_get_profiles_root`) anchor on `Path.home()`, not `get_hermes_home()` — this is intentional.

→ Full rules: `docs/agents/profiles.md`, `docs/agents/pitfalls.md`

### Prompt caching

- Do NOT alter past context, change toolsets, or rebuild system prompts mid-conversation.
- The ONLY time we alter context is during context compression.
- Slash commands that mutate system-prompt state must default to deferred invalidation; opt-in `--now` for immediate.

→ `docs/agents/policies.md`

### Plugin-core boundary (Teknium, May 2026)

Plugins MUST NOT modify core files (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`, ...). Expand the generic plugin surface (new hook, new ctx method) instead — never hardcode plugin-specific logic into core.

→ `docs/agents/plugins.md`

### Testing

- **ALWAYS use `scripts/run_tests.sh`** — never `pytest` directly. Wrapper enforces CI parity (unset credentials, TZ=UTC, LANG=C.UTF-8, `-n 4` xdist).
- Tests must NOT write to `~/.hermes/`. The `_isolate_hermes_home` autouse fixture redirects `HERMES_HOME` to a tmp dir.
- Profile tests must additionally `monkeypatch.setattr(Path, "home", lambda: tmp_path)`.
- Don't write change-detector tests (snapshots of model catalogs, config versions, etc.) — write invariant assertions instead.

→ `docs/agents/testing.md`

### Git / commits

- Only create commits when the user explicitly asks. Do not pre-emptively `git add` / `commit`.
- Never `--amend` a commit that's been pushed unless the user asks.
- Squash merges: ensure branch is up-to-date with `main` first; stale branches silently revert recent fixes.

→ `docs/agents/pitfalls.md`

### Display / TUI

- Do NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code — leaks as `?[K` under `prompt_toolkit`'s `patch_stdout`. Use space-padding.
- Do NOT introduce new `simple_term_menu` usage — has ghost-duplication bugs in tmux/iTerm2. Use `hermes_cli/curses_ui.py` (curses, stdlib).

→ `docs/agents/pitfalls.md`

### Tool schema descriptions (DO NOT hardcode cross-tool references)

Do NOT mention tools from other toolsets by name in a tool's schema description (e.g. `browser_navigate` saying "prefer web_search"). They may be unavailable. Do cross-references dynamically in `get_tool_definitions()`.

→ `docs/agents/adding-tools.md`, `docs/agents/pitfalls.md`

### Gateway approval/control commands

Two message guards exist (`base.py` `_pending_messages` queue + `gateway/run.py` runner intercept). Approval/control commands (`/stop /new /queue /status /approve /deny`) must bypass BOTH and dispatch inline.

→ `docs/agents/pitfalls.md`

## Path Speed Sheet

| Resource | Path |
|----------|------|
| Core agent loop | `run_agent.py` (AIAgent class) |
| Tool dispatch | `model_tools.py` |
| Tool definitions | `toolsets.py` (`_HERMES_CORE_TOOLS`) |
| Tool implementations | `tools/<name>.py` (auto-discovered via `tools/registry.py`) |
| CLI orchestrator | `cli.py` (HermesCLI class) |
| Slash command registry | `hermes_cli/commands.py` (`COMMAND_REGISTRY`) |
| TUI entry | `ui-tui/src/entry.tsx` ↔ `tui_gateway/server.py` |
| Profile-safe paths | `hermes_constants.py` (`get_hermes_home`, `display_hermes_home`) |
| User config | `~/.hermes/config.yaml` (settings), `~/.hermes/.env` (secrets only) |
| Logs | `~/.hermes/logs/agent.log` / `errors.log` / `gateway.log` |
| Tests wrapper | `scripts/run_tests.sh` |

### Common Enums (don't invent new values)

| Field | Allowed values |
|-------|----------------|
| `CommandDef.category` | `"Session"` / `"Configuration"` / `"Tools & Skills"` / `"Info"` / `"Exit"` |
| `OPTIONAL_ENV_VARS[...]["category"]` | `"provider"` / `"tool"` / `"messaging"` / `"setting"` |
| `display.background_process_notifications` | `"all"` / `"result"` / `"error"` / `"off"` |

→ Full tree + dependency chain: `docs/agents/project-structure.md`

## Task → docs/agents/ Index

Read the relevant file *only when your task touches that area*:

| Task | Read |
|------|------|
| **Picking which extension point to use** (skill vs subagent vs hook vs MCP vs CLAUDE.md) | `docs/agents/extension-decision.md` |
| **Authoring / proposing a shell hook** (when to suggest one, `hermes hooks new` / `suggest`) | `skills/productivity/create-hook/SKILL.md` |
| Project layout / what's in which folder | `docs/agents/project-structure.md` |
| Editing AIAgent / understanding the conversation loop | `docs/agents/agent-class.md` |
| Editing CLI / adding a slash command / spinner / banner | `docs/agents/cli-architecture.md` |
| Editing TUI (Ink + JSON-RPC) | `docs/agents/tui-architecture.md` |
| Adding a new tool | `docs/agents/adding-tools.md` |
| Adding config.yaml or .env settings | `docs/agents/adding-configuration.md` |
| Adding a skin / customizing CLI theme | `docs/agents/skin-theme-system.md` |
| Writing a plugin (general / memory / context-engine / image-gen) | `docs/agents/plugins.md` |
| Adding a built-in skill / SKILL.md frontmatter | `docs/agents/skills.md` |
| Prompt caching policy / context-file priority / background process notifications | `docs/agents/policies.md` |
| Profile-safe code patterns | `docs/agents/profiles.md` |
| Avoiding known footguns | `docs/agents/pitfalls.md` |
| Running tests / CI parity / change-detector tests | `docs/agents/testing.md` |
| **Writing / splitting AGENTS.md / SKILL.md** (layered instructions, hard-rule tiers) | `docs/agents/instruction-files.md` |

## Agent Loop One-liner

```
loop until budget/iterations exhausted or no tool calls:
  response = chat.completions.create(messages, tools)
  if response.tool_calls: execute each via handle_function_call(); append; iterate
  else: return response.content
```

Messages: OpenAI format. Reasoning: `assistant_msg["reasoning"]`.
Detail: `docs/agents/agent-class.md`.
