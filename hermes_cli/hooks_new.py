"""hermes hooks new — scaffold a shell hook from a starter template.

This is intentionally deterministic — it does NOT call any LLM. The
companion ``hermes hooks suggest`` command handles the "what should I add"
question; this command handles "now actually wire it up".

Lifecycle::

    1. Pick a template (interactive menu or --from-template <name>)
    2. Optionally override name / event / matcher / timeout
    3. Copy template -> ~/.hermes/agent-hooks/<name>.sh + chmod +x
    4. Patch ~/.hermes/config.yaml hooks: block (or print snippet)
    5. Run `hermes hooks doctor` to validate
    6. Tell the user how to flip the allowlist switch

Config patching is text-based on purpose: PyYAML's safe_load+safe_dump
round-trip destroys comments, and ruamel.yaml is not a Hermes dependency.
We never delete or rewrite existing lines — we only append new ones.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from hermes_constants import get_hermes_home


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HookTemplate:
    name: str
    script: str       # filename under scripts/agent-hooks-examples/
    event: str        # default event
    matcher: Optional[str]
    timeout: int
    intent: str       # one-line description (shown in menu)
    requires: tuple   # external CLIs that must be on PATH (advisory only)


_TEMPLATES: Dict[str, HookTemplate] = {
    "block-rm-rf": HookTemplate(
        name="block-rm-rf",
        script="block-rm-rf.sh",
        event="pre_tool_call",
        matcher="terminal",
        timeout=5,
        intent="Block `rm -rf /` and `rm -rf /<critical>` paths",
        requires=("jq",),
    ),
    "block-env-write": HookTemplate(
        name="block-env-write",
        script="block-env-write.sh",
        event="pre_tool_call",
        matcher="write_file|patch|terminal",
        timeout=5,
        intent="Block writes to .env, ~/.aws/credentials, *.pem, etc.",
        requires=("jq",),
    ),
    "block-force-push-main": HookTemplate(
        name="block-force-push-main",
        script="block-force-push-main.sh",
        event="pre_tool_call",
        matcher="terminal",
        timeout=5,
        intent="Refuse `git push --force` to main/master/release/*",
        requires=("jq",),
    ),
    "auto-format": HookTemplate(
        name="auto-format",
        script="auto-format.sh",
        event="post_tool_call",
        matcher="write_file|patch",
        timeout=30,
        intent="Run `black --quiet` on .py files after every write",
        requires=("jq", "black"),
    ),
    "auto-stage-on-write": HookTemplate(
        name="auto-stage-on-write",
        script="auto-stage-on-write.sh",
        event="post_tool_call",
        matcher="write_file|patch",
        timeout=10,
        intent="`git add` files the agent just wrote so the diff stays ready",
        requires=("jq", "git"),
    ),
    "inject-cwd-context": HookTemplate(
        name="inject-cwd-context",
        script="inject-cwd-context.sh",
        event="pre_llm_call",
        matcher=None,
        timeout=10,
        intent="Inject `git status --porcelain` into the next LLM turn",
        requires=("jq", "git"),
    ),
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_new(args) -> int:
    """Entry point invoked by ``hermes hooks new``.

    Returns a POSIX exit code so the parent CLI can propagate non-zero on
    failure (useful for scripted use under ``--non-interactive``).
    """
    interactive = not bool(getattr(args, "non_interactive", False))
    dry_run = bool(getattr(args, "dry_run", False))

    tpl = _select_template(args, interactive=interactive)
    if tpl is None:
        return 2

    name = (getattr(args, "name", None) or tpl.name).strip()
    if not _is_safe_filename(name):
        print(f"Error: --name {name!r} contains unsafe characters; use [A-Za-z0-9._-] only.")
        return 2

    event = (getattr(args, "event", None) or tpl.event).strip()
    matcher = getattr(args, "matcher", None)
    if matcher is None:
        matcher = tpl.matcher
    timeout = getattr(args, "timeout", None) or tpl.timeout
    if not (1 <= int(timeout) <= 300):
        print(f"Error: timeout must be 1-300s (got {timeout}).")
        return 2

    # Validate event against the canonical VALID_HOOKS set so we fail loudly
    # before patching config rather than letting _parse_hooks_block silently
    # drop it at next launch.
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        VALID_HOOKS = set()
    if VALID_HOOKS and event not in VALID_HOOKS:
        print(f"Error: unknown event {event!r}.")
        print(f"  Valid events: {', '.join(sorted(VALID_HOOKS))}")
        return 2

    src = _template_source_path(tpl)
    if not src.exists():
        print(f"Error: template source missing: {src}")
        print("  (this usually means scripts/agent-hooks-examples/ was deleted "
              "from your checkout)")
        return 1

    dest = get_hermes_home() / "agent-hooks" / f"{name}.sh"

    # Step 1: copy script
    print(f"[1/4] script: {dest}")
    if dest.exists():
        if interactive and not _confirm(f"      ⚠ overwrite existing {dest}?", default=False):
            print("      skipped.")
            return 1
        elif not interactive:
            print(f"      ⚠ overwriting existing file (non-interactive mode)")
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        st = os.stat(dest)
        os.chmod(dest, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("      ✓ copied + chmod +x" if not dry_run else "      (dry-run: skipped)")

    # Step 2: patch config.yaml
    cfg_path = get_hermes_home() / "config.yaml"
    snippet = _build_yaml_snippet(event, str(dest), matcher, timeout)
    print(f"[2/4] config: {cfg_path}")
    print("      snippet to install:")
    for line in snippet.rstrip("\n").splitlines():
        print(f"        {line}")
    if not dry_run:
        do_patch = True
        if interactive:
            do_patch = _confirm("      append to ~/.hermes/config.yaml now?", default=True)
        if do_patch:
            patched = _append_to_config_yaml(cfg_path, event, snippet)
            print(f"      ✓ {patched}")
        else:
            print("      skipped. (paste the snippet above into hooks: yourself)")
    else:
        print("      (dry-run: skipped)")

    # Step 3: doctor
    print("[3/4] doctor:")
    if dry_run:
        print("      (dry-run: skipped — re-run without --dry-run to validate)")
    else:
        _run_doctor_for_command(str(dest))

    # Step 4: allowlist guidance
    print("[4/4] next steps:")
    print("      • Restart Hermes (or the gateway) so the new hook registers.")
    print("      • On first registration Hermes will prompt for consent. To")
    print("        skip the prompt in headless runs, use one of:")
    print("            hermes <command> --accept-hooks")
    print("            export HERMES_ACCEPT_HOOKS=1")
    print("            (or set `hooks_auto_accept: true` in config.yaml)")
    print("      • Inspect with: hermes hooks list   |   hermes hooks doctor")
    if tpl.requires:
        _warn_missing_deps(tpl)

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _select_template(args, *, interactive: bool) -> Optional[HookTemplate]:
    requested = getattr(args, "from_template", None)
    if requested:
        if requested not in _TEMPLATES:
            print(f"Error: unknown template {requested!r}.")
            print(f"  Available: {', '.join(sorted(_TEMPLATES))}")
            return None
        return _TEMPLATES[requested]

    if not interactive:
        print("Error: --from-template is required in --non-interactive mode.")
        print(f"  Available: {', '.join(sorted(_TEMPLATES))}")
        return None

    print("Pick a starter template:\n")
    items = list(_TEMPLATES.values())
    for i, tpl in enumerate(items, 1):
        matcher_part = f" matcher={tpl.matcher!r}" if tpl.matcher else ""
        print(f"  [{i}] {tpl.name}")
        print(f"        event={tpl.event}{matcher_part} timeout={tpl.timeout}s")
        print(f"        {tpl.intent}")
        print()

    while True:
        raw = _prompt("Number to scaffold (or 'q' to abort): ").strip().lower()
        if raw in ("q", "quit", "exit", ""):
            return None
        try:
            idx = int(raw)
        except ValueError:
            print("  please enter a number")
            continue
        if 1 <= idx <= len(items):
            return items[idx - 1]
        print(f"  pick a number 1..{len(items)}")


def _template_source_path(tpl: HookTemplate) -> Path:
    # Templates ship inside the Hermes checkout under scripts/agent-hooks-examples/.
    # __file__ -> hermes_cli/hooks_new.py, two parents up is the project root.
    return Path(__file__).resolve().parents[1] / "scripts" / "agent-hooks-examples" / tpl.script


def _is_safe_filename(name: str) -> bool:
    # No path separators, no leading dot, no spaces. We allow ascii letters,
    # digits, dash, underscore, dot (for versioning like 'my-hook.v2').
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name))


def _build_yaml_snippet(event: str, script_path: str, matcher: Optional[str], timeout: int) -> str:
    # We always emit a leaf list entry — _append_to_config_yaml decides
    # whether to also emit the parent `hooks:` and `<event>:` keys.
    quoted_path = _yaml_quote(script_path)
    out: List[str] = [f"  - command: {quoted_path}"]
    if matcher is not None:
        out.append(f"    matcher: {_yaml_quote(matcher)}")
    if timeout != 60:
        out.append(f"    timeout: {int(timeout)}")
    return "\n".join(out) + "\n"


def _yaml_quote(value: str) -> str:
    # Always emit double-quoted scalars: clearer for users reading the diff
    # and removes the regex / special-char escape headaches around matchers
    # like "write_file|patch".
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _append_to_config_yaml(cfg_path: Path, event: str, leaf_snippet: str) -> str:
    """Append a hook entry to ``cfg_path`` while preserving all comments.

    Returns a short status string describing what was done. Strategy:

      1. If ``hooks:`` block doesn't exist yet, append the full block at EOF.
      2. If ``hooks:`` block exists but the requested event subkey doesn't,
         append ``  <event>:\\n<leaf>`` under hooks:.
      3. If both ``hooks:`` and the event subkey exist, append a sibling
         ``-`` list item under the event.

    We use a text-level scan (not PyYAML round-trip) because the user almost
    always has comments and ordering in config.yaml that we must not destroy.
    """
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        block = f"\nhooks:\n  {event}:\n{leaf_snippet}"
        cfg_path.write_text(block)
        return f"created {cfg_path} with new hooks: block"

    original = cfg_path.read_text()
    # Detect line-ending style so our injection matches.
    eol = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()

    hooks_idx, hooks_is_empty = _find_top_level_hooks_line(lines)
    if hooks_idx is None:
        # No existing hooks: at end of file.
        suffix_lines = ["", f"hooks:", f"  {event}:"]
        suffix_lines.extend(leaf_snippet.rstrip("\n").splitlines())
        suffix_lines.append("")
        # Avoid double-blank if file already ends with blank line.
        body = original.rstrip("\n") + eol + eol.join(suffix_lines)
        if not body.endswith(eol):
            body += eol
        cfg_path.write_text(body)
        return "added new hooks: block at end of config.yaml"

    # If we encountered `hooks: {}` / `hooks: null` / `hooks: ~`, rewrite the
    # one offending line to a block-style `hooks:` so the subsequent appended
    # children are valid YAML. We preserve any trailing comment on that line.
    if hooks_is_empty:
        original_line = lines[hooks_idx]
        comment_match = re.search(r"\s+(#.*)$", original_line)
        comment = comment_match.group(1) if comment_match else ""
        lines[hooks_idx] = "hooks:" + ((" " + comment) if comment else "")

    # Find the end of the hooks: block (next top-level key or EOF).
    end_idx = _find_block_end(lines, hooks_idx)

    # Inside the hooks: block, look for `  <event>:` at exactly 2-space indent.
    event_pattern = re.compile(r"^  " + re.escape(event) + r"\s*:\s*(#.*)?$")
    event_idx = None
    for i in range(hooks_idx + 1, end_idx):
        if event_pattern.match(lines[i]):
            event_idx = i
            break

    if event_idx is None:
        # Event subkey doesn't exist — insert `  <event>:\n<leaf>` just before
        # end_idx so the list lands inside the hooks: block.
        insertion = [f"  {event}:"]
        insertion.extend(leaf_snippet.rstrip("\n").splitlines())
        new_lines = lines[:end_idx] + insertion + lines[end_idx:]
        cfg_path.write_text(eol.join(new_lines) + (eol if original.endswith("\n") else ""))
        return f"added `{event}:` subkey under existing hooks: block"

    # Event exists — find the end of its list (next sibling at 2-space indent
    # or end of hooks: block).
    sibling_pattern = re.compile(r"^  \S")
    event_end = end_idx
    for i in range(event_idx + 1, end_idx):
        if sibling_pattern.match(lines[i]):
            event_end = i
            break

    insertion = leaf_snippet.rstrip("\n").splitlines()
    new_lines = lines[:event_end] + insertion + lines[event_end:]
    cfg_path.write_text(eol.join(new_lines) + (eol if original.endswith("\n") else ""))
    return f"appended new entry under hooks.{event}"


def _find_top_level_hooks_line(lines: List[str]) -> tuple:
    """Locate a top-level ``hooks:`` declaration.

    Returns a ``(line_index, is_empty_placeholder)`` tuple. The second
    element is True when we encounter the common "empty placeholder" forms
    ``hooks: {}`` / ``hooks: null`` / ``hooks: ~`` — callers must rewrite
    that line to plain ``hooks:`` before appending children, otherwise the
    result is malformed YAML.

    Returns ``(None, False)`` when no top-level hooks declaration exists.
    """
    proper = re.compile(r"^hooks\s*:\s*(#.*)?$")
    empty_placeholder = re.compile(r"^hooks\s*:\s*(\{\}|null|~)\s*(#.*)?$")
    for i, line in enumerate(lines):
        if proper.match(line):
            return i, False
        if empty_placeholder.match(line):
            return i, True
    return None, False


def _find_block_end(lines: List[str], start_idx: int) -> int:
    """Return the index of the line AFTER the block that started at *start_idx*.

    A block ends when we hit the next top-level key (any line whose first
    non-blank char sits at column 0, isn't ``#``, and whose first ``:`` is
    followed by whitespace / end-of-line / a YAML flow opener / a comment).
    Blank lines inside the block are kept inside the block.
    """
    # First char non-space non-`#` non-`-` (list items are children of the
    # previous mapping, not new top-level keys). The colon must be followed
    # by a separator: whitespace, EOL, '#' for a comment, or one of YAML's
    # flow / block scalar indicators ('{', '[', '|', '>') for things like
    # ``hooks: {}`` or ``security: |``.
    next_top = re.compile(r"^[^\s#\-][^:]*?:(\s|$|#|[\{\[\|>])")
    for i in range(start_idx + 1, len(lines)):
        if next_top.match(lines[i]):
            return i
    return len(lines)


def _run_doctor_for_command(script_path: str) -> None:
    try:
        from hermes_cli.config import load_config
        from agent import shell_hooks
    except Exception as exc:
        print(f"      ⚠ could not load doctor backend: {exc}")
        return

    specs = shell_hooks.iter_configured_hooks(load_config())
    specs = [s for s in specs if s.command == script_path or s.command.endswith(script_path)]
    if not specs:
        print("      ⚠ hook not found in parsed config — config patch may not")
        print("        have taken effect; check ~/.hermes/config.yaml manually.")
        return

    spec = specs[0]
    if shell_hooks.script_is_executable(spec.command):
        print(f"      ✓ {spec.command} exists, executable")
    else:
        print(f"      ✗ {spec.command} not executable — `chmod +x` it")

    entry = shell_hooks.allowlist_entry_for(spec.event, spec.command)
    if entry:
        print("      ✓ already allowlisted")
    else:
        print("      ℹ not allowlisted yet — Hermes will prompt on first launch")


def _warn_missing_deps(tpl: HookTemplate) -> None:
    missing = [d for d in tpl.requires if shutil.which(d) is None]
    if missing:
        print(f"      ⚠ template uses {', '.join(missing)} which is not on PATH —")
        print("        install before relying on this hook")


def _prompt(text: str) -> str:
    # Wrap input() so we degrade gracefully under captured stdin (CI logs).
    try:
        return input(text)
    except EOFError:
        return ""


def _confirm(text: str, *, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    raw = _prompt(text + suffix).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")
