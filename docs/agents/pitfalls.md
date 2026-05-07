# Known Pitfalls

## DO NOT hardcode `~/.hermes` paths

Use `get_hermes_home()` from `hermes_constants` for code paths. Use `display_hermes_home()`
for user-facing print/log messages. Hardcoding `~/.hermes` breaks profiles — each profile
has its own `HERMES_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

## DO NOT introduce new `simple_term_menu` usage

Existing call sites in `hermes_cli/main.py` remain for legacy fallback only;
the preferred UI is curses (stdlib) because `simple_term_menu` has
ghost-duplication rendering bugs in tmux/iTerm2 with arrow keys. New
interactive menus must use `hermes_cli/curses_ui.py` — see
`hermes_cli/tools_config.py` for the canonical pattern.

## DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code

Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

## `_last_resolved_tool_names` is a process-global in `model_tools.py`

`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

## DO NOT hardcode cross-tool references in schema descriptions

Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

## The gateway has TWO message guards — both must bypass approval/control commands

When an agent is running, messages pass through two sequential guards:
(1) **base adapter** (`gateway/platforms/base.py`) queues messages in
`_pending_messages` when `session_key in self._active_sessions`, and
(2) **gateway runner** (`gateway/run.py`) intercepts `/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny` before they reach
`running_agent.interrupt()`. Any new command that must reach the runner
while the agent is blocked (e.g. approval prompts) MUST bypass BOTH
guards and be dispatched inline, not via `_process_message_background()`
(which races session lifecycle).

## Squash merges from stale branches silently revert recent fixes

Before squash-merging a PR, ensure the branch is up to date with `main`
(`git fetch origin main && git reset --hard origin/main` in the worktree,
then re-apply the PR's commits). A stale branch's version of an unrelated
file will silently overwrite recent fixes on main when squashed. Verify
with `git diff HEAD~1..HEAD` after merging — unexpected deletions are a
red flag.

## Don't wire in dead code without E2E validation

Unused code that was never shipped was dead for a reason. Before wiring an
unused module into a live code path, E2E test the real resolution chain
with actual imports (not mocks) against a temp `HERMES_HOME`.

## Tests must not write to `~/.hermes/`

The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.hermes/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_hermes_home()` resolve within the temp dir.
Use the pattern from `tests/hermes_cli/test_profiles.py`:

```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

## BOOT.md built-in hook removed (upstream v0.12.0)

The `boot-md` built-in gateway hook was removed in upstream PR #17093. The
`gateway/builtin_hooks/` directory is now an extension point with **no shipped
hooks**. Old comments referring to "boot-md" are obsolete.

## TUI clipboard — three-tier strategy & headless gotchas (upstream v0.12.0)

Hermes TUI clipboard handling uses a three-tier strategy. The order matters
because tier 1 fails or hangs in headless environments.

1. **Native OS tools** (`pbcopy`, `wl-copy`, `xclip`, `xsel`, `clip.exe`) — only
   when a display server is present (`$DISPLAY` for X11 or `$WAYLAND_DISPLAY`
   for Wayland). On Linux in headless environments (Docker, remote SSH without
   X11 forwarding), these tools fail or hang. The code short-circuits
   immediately if both env vars are unset.
2. **tmux buffer** (`tmux load-buffer`) — when inside a tmux session; requires
   `set-clipboard on` for system clipboard propagation.
3. **OSC 52 escape** — written to stdout; the terminal emulator must intercept
   and set the clipboard. Support varies: iTerm2 disables it by default,
   VS Code may block it behind a permission prompt, raw PTYs without an
   emulator drop it silently.

**Environment variables:**

| Variable | Purpose |
|---|---|
| `HERMES_TUI_CLIPBOARD_OSC52` / `HERMES_TUI_COPY_OSC52` | Force OSC 52 emission (`1`/`true`) or disable (`0`/`false`). Ignored when native tools are expected to work (macOS local, or Linux with `$DISPLAY/$WAYLAND_DISPLAY`). |
| `HERMES_TUI_DEBUG_CLIPBOARD` | Set to `1` to log detailed debug info to stderr about which clipboard path is taken. |
| `SSH_CONNECTION` | Presence indicates an SSH session; gates native tool usage and prefers OSC 52. |
| `TMUX`, `STY` | Tmux/screen detection for passthrough or buffer loading. |

**Common false-positive**: The "copied selection" UI message displays
**unconditionally** after Ctrl+C, even if all clipboard mechanisms fail. In a
headless Docker container or non-OSC52-capable terminal you'll see the message
but nothing is copied. Use `HERMES_TUI_DEBUG_CLIPBOARD=1` to diagnose.

**Dashboard caveat**: The dashboard's `Ctrl+C` path relies on
OSC 52 → xterm's handler → browser Clipboard API. Because the Clipboard API
requires a user gesture, this can fail if the OSC 52 response arrives outside
the key event's activation window. Use `Ctrl+Shift+C` (Cmd+Shift+C on macOS)
as a reliable fallback — it calls `navigator.clipboard.writeText()` directly
inside the key handler.
