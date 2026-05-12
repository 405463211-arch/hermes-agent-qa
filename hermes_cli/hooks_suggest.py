"""hermes hooks suggest — mine recent sessions for repeated tool calls.

We scan ``~/.hermes/sessions/session_*.json`` modified within the lookback
window, fingerprint each ``tool_calls[]`` entry, and report fingerprints
that crossed a frequency threshold. Each candidate is annotated with a
recommended event + matcher so the user can pipe straight into
``hermes hooks new``.

Why session JSON, not ``agent.log``: the log is human-readable text that
varies across providers and log levels; the session files are stable
OpenAI-style JSON with ``messages[].tool_calls[].function.{name,arguments}``
already populated. Zero regex.

LLM rationale step is **opt-in** via ``--with-llm``: it sends the top
candidates to the configured ``session_search`` auxiliary model and asks
for a JSON object with concrete matcher regexes + rationale. Default
behaviour is purely frequency-based so the command stays fast and free.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

# Tools whose call cadence is "just how the agent works" — high frequency
# alone is not a hook signal. Listing them here demotes their score so the
# top of the suggest menu surfaces actionable patterns instead.
_OBSERVATION_TOOLS = frozenset({
    "read_file", "search_files", "list_files", "glob",
    "memory", "todo", "skill_manage", "skills_browse",
    "execute_code",   # often used for ad-hoc python eval, not a hook target
})

# Terminal verbs that are pure navigation / inspection. Same demotion logic.
_NAVIGATION_VERBS = frozenset({
    "cd", "pwd", "ls", "echo", "cat", "head", "tail",
    "which", "type", "env", "printenv",
})

# Tool categories — drives default filtering. UNKNOWN means we don't have
# strong evidence either way; we still surface those so users can decide.
CAT_HOOKABLE = "hookable"     # write/patch + terminal with action verbs
CAT_OBSERVATION = "observation"
CAT_NAVIGATION = "navigation"
CAT_UNKNOWN = "unknown"


@dataclass
class Fingerprint:
    """A normalised key for a tool call.

    Two tool calls with the same fingerprint are considered "the same kind
    of thing the agent keeps doing" for the purpose of hook suggestion.
    """
    tool: str
    detail: str           # e.g. "black", ".py", "git push", "" for tools with no useful sub-key
    suggested_event: str  # default event when proposing a hook
    suggested_matcher: Optional[str]  # default matcher regex (None = always-fire)
    category: str = CAT_UNKNOWN

    @property
    def key(self) -> str:
        return f"{self.tool}::{self.detail}" if self.detail else self.tool

    @property
    def human(self) -> str:
        if self.detail:
            return f"{self.tool}  ({self.detail})"
        return self.tool


@dataclass
class Candidate:
    fingerprint: Fingerprint
    count: int
    sessions_touched: int
    sample_calls: List[Dict[str, Any]] = field(default_factory=list)

    def to_public(self) -> Dict[str, Any]:
        return {
            "tool": self.fingerprint.tool,
            "detail": self.fingerprint.detail,
            "suggested_event": self.fingerprint.suggested_event,
            "suggested_matcher": self.fingerprint.suggested_matcher,
            "count": self.count,
            "sessions_touched": self.sessions_touched,
            "samples": self.sample_calls[:3],
        }


# Tools whose argument signature is worth fingerprinting more granularly
# than just the tool name. For everything else we fall back to tool-name.
def _fingerprint_terminal(args: Dict[str, Any]) -> Fingerprint:
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return Fingerprint("terminal", "", "pre_tool_call", "terminal", CAT_UNKNOWN)
    # Take first 1-2 shell tokens so `black foo.py` and `black bar.py` collide,
    # but `git push origin main` keeps the verb and subverb together.
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return Fingerprint("terminal", "", "pre_tool_call", "terminal", CAT_UNKNOWN)
    verb = tokens[0].rsplit("/", 1)[-1]  # strip path on /usr/bin/black
    detail = verb
    multi_verb_tools = {"git", "npm", "pnpm", "yarn", "kubectl", "docker",
                        "pip", "uv", "cargo", "go", "make"}
    if verb in multi_verb_tools and len(tokens) > 1:
        # Sub-command (push/install/build/...) makes a much more useful signal.
        detail = f"{verb} {tokens[1]}"

    if verb in _NAVIGATION_VERBS:
        category = CAT_NAVIGATION
    else:
        # Anything with a verb that survives the navigation/observation filter
        # is plausibly hookable (formatter, linter, package manager, git, etc.)
        category = CAT_HOOKABLE
    return Fingerprint("terminal", detail, "pre_tool_call", "terminal", category)


def _fingerprint_write(args: Dict[str, Any], tool: str) -> Fingerprint:
    path = args.get("path") or args.get("filename") or ""
    suffix = Path(path).suffix.lower() if path else ""
    detail = suffix or "(no-ext)"
    # post_tool_call so we can hook formatters / linters / staging after writes.
    return Fingerprint(tool, detail, "post_tool_call", "write_file|patch", CAT_HOOKABLE)


_FINGERPRINT_DISPATCH = {
    "terminal":   _fingerprint_terminal,
    "write_file": lambda a: _fingerprint_write(a, "write_file"),
    "patch":      lambda a: _fingerprint_write(a, "patch"),
    "edit":       lambda a: _fingerprint_write(a, "edit"),
}


def fingerprint_tool_call(tool_name: str, args: Dict[str, Any]) -> Fingerprint:
    fn = _FINGERPRINT_DISPATCH.get(tool_name)
    if fn is not None:
        return fn(args)
    # Generic fallback: tool name alone. Suggest pre_tool_call as a guardrail
    # vehicle since that's the only point where you can actually block, but
    # we leave matcher set to the tool name so it stays scoped.
    category = CAT_OBSERVATION if tool_name in _OBSERVATION_TOOLS else CAT_UNKNOWN
    return Fingerprint(tool_name, "", "pre_tool_call", tool_name, category)


def best_starter_template(fp: "Fingerprint") -> Optional[str]:
    """Return the name of an existing starter template that fits the fingerprint.

    High-precision / low-recall on purpose: we'd rather say "no template fits,
    write one yourself" than steer the user toward a script that almost-fits
    and silently misbehaves. Lookups intentionally hand-curated — see
    ``scripts/agent-hooks-examples/`` for the canonical six.
    """
    if fp.tool == "terminal":
        if not fp.detail:
            return None
        verb = fp.detail.split()[0]
        # User manually running a Python formatter → install the post-write
        # version so it stops happening on every turn.
        if verb in ("black", "ruff"):
            return "auto-format"
        # `rm` heavily used — if it's `-rf` they'll thank us; if not the
        # script's own regex is a strict-superset guard that won't false-fire.
        if verb == "rm":
            return "block-rm-rf"
        # `git push` repeated — could be normal pushing or repeated force
        # pushes. The block-force-push template only fires on --force/-f to
        # protected branches, so it's safe to suggest broadly.
        if fp.detail.startswith("git push"):
            return "block-force-push-main"
        return None
    if fp.tool in ("write_file", "patch", "edit"):
        # auto-format.sh handles .py (black) and .yaml/.yml (yamlfmt); both
        # silently no-op if the formatter isn't installed, so suggesting it
        # is always safe.
        if fp.detail in (".py", ".yaml", ".yml"):
            return "auto-format"
        return None
    return None


# ---------------------------------------------------------------------------
# Session scanning
# ---------------------------------------------------------------------------

def _iter_recent_session_paths(lookback_hours: int) -> List[Path]:
    root = get_hermes_home() / "sessions"
    if not root.is_dir():
        return []
    cutoff = time.time() - max(1, lookback_hours) * 3600
    out: List[Path] = []
    for p in root.glob("session_*.json"):
        try:
            if p.stat().st_mtime >= cutoff:
                out.append(p)
        except OSError:
            continue
    # Newest first so display samples favour recent activity.
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _extract_tool_calls(session_path: Path) -> List[Tuple[str, Dict[str, Any]]]:
    try:
        raw = json.loads(session_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    messages = raw.get("messages")
    if not isinstance(messages, list):
        return []

    out: List[Tuple[str, Dict[str, Any]]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name")
            if not isinstance(name, str):
                continue
            raw_args = fn.get("arguments")
            args: Dict[str, Any] = {}
            if isinstance(raw_args, str):
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                except json.JSONDecodeError:
                    pass
            elif isinstance(raw_args, dict):
                args = raw_args
            out.append((name, args))
    return out


def collect_candidates(
    lookback_hours: int,
    threshold: int,
    top: int,
    *,
    include_all: bool = False,
) -> List[Candidate]:
    paths = _iter_recent_session_paths(lookback_hours)
    counts: Counter[str] = Counter()
    sessions_for_key: Dict[str, set] = defaultdict(set)
    fingerprints: Dict[str, Fingerprint] = {}
    samples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for p in paths:
        seen_in_this_session: set = set()
        for tool_name, args in _extract_tool_calls(p):
            fp = fingerprint_tool_call(tool_name, args)
            counts[fp.key] += 1
            fingerprints[fp.key] = fp
            seen_in_this_session.add(fp.key)
            if len(samples[fp.key]) < 3:
                samples[fp.key].append({
                    "session": p.name,
                    "tool": tool_name,
                    "args_keys": sorted(args.keys())[:6],
                    "args_preview": _short_args(args),
                })
        for k in seen_in_this_session:
            sessions_for_key[k].add(p.name)

    # Sort hookable first, then unknown, then observation/navigation last —
    # secondary key is raw count. This matters because high-frequency
    # observation tools (read_file etc.) would otherwise swamp the top of
    # the menu and bury the actionable candidates.
    category_rank = {
        CAT_HOOKABLE: 0,
        CAT_UNKNOWN: 1,
        CAT_OBSERVATION: 2,
        CAT_NAVIGATION: 3,
    }

    ranked = sorted(
        counts.items(),
        key=lambda kv: (category_rank.get(fingerprints[kv[0]].category, 9), -kv[1]),
    )

    candidates: List[Candidate] = []
    for key, n in ranked:
        if n < threshold:
            continue
        fp = fingerprints[key]
        if not include_all and fp.category in (CAT_OBSERVATION, CAT_NAVIGATION):
            continue
        candidates.append(Candidate(
            fingerprint=fp,
            count=n,
            sessions_touched=len(sessions_for_key[key]),
            sample_calls=samples[key],
        ))
        if len(candidates) >= top:
            break
    return candidates


def _short_args(args: Dict[str, Any]) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= 120 else s[:117] + "..."


# ---------------------------------------------------------------------------
# Optional LLM rationale step
# ---------------------------------------------------------------------------

_LLM_PROMPT = """You are reviewing repeated tool calls from a coding agent's recent sessions.
Each candidate below crossed a frequency threshold and is therefore a potential
hook target — meaning the same deterministic action is happening over and over
and should be moved out of the LLM context into a shell hook.

For each candidate, return a JSON object with these fields:
  - "key":         the candidate's "tool::detail" key, exactly as given
  - "verdict":     one of "hook_candidate", "leave_in_llm", "ambiguous"
  - "event":       suggested Hermes event (e.g. "pre_tool_call", "post_tool_call",
                   "pre_llm_call"). Only suggest "pre_tool_call" if blocking or
                   gating makes sense; "post_tool_call" for formatters/staging;
                   "pre_llm_call" for context injection.
  - "matcher":     suggested matcher regex (string) or null if event is not
                   pre_tool_call / post_tool_call
  - "rationale":   one sentence (<=20 words) explaining the verdict
  - "starter_template": one of "block-rm-rf", "block-env-write",
                        "block-force-push-main", "auto-format",
                        "auto-stage-on-write", "inject-cwd-context", or null
                        if no existing template fits

Respond with ONLY a JSON object of shape {"verdicts": [<one per candidate>]}.
No prose, no markdown, no commentary.

Candidates:
"""


def _annotate_with_llm(
    candidates: List[Candidate], *, wall_timeout: float = 90.0
) -> Dict[str, Dict[str, Any]]:
    """Best-effort LLM annotation. Returns ``{candidate_key: verdict_dict}``
    on success, empty dict on any failure (timeout, no provider, bad JSON).

    The LLM call runs on a background thread with a hard wall-clock deadline
    because the OpenAI SDK ``timeout`` kwarg isn't honored uniformly across
    providers — some configurations let the request hang indefinitely on
    DNS / TLS / streaming reads. We'd rather abandon the rationale step
    than block the CLI forever.
    """
    try:
        from agent.auxiliary_client import call_llm
    except Exception:
        return {}

    payload = [
        {
            "key": c.fingerprint.key,
            "tool": c.fingerprint.tool,
            "detail": c.fingerprint.detail,
            "count": c.count,
            "sessions": c.sessions_touched,
            "samples": [s.get("args_preview", "") for s in c.sample_calls[:2]],
        }
        for c in candidates
    ]
    prompt = _LLM_PROMPT + json.dumps(payload, ensure_ascii=False, indent=2)

    import threading

    result_holder: Dict[str, Any] = {}

    def _do_call() -> None:
        try:
            resp = call_llm(
                task="session_search",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2000,
                timeout=wall_timeout - 5.0,  # give SDK a hair less than our wall clock
            )
            result_holder["content"] = resp.choices[0].message.content or ""
        except Exception as exc:
            result_holder["error"] = f"{type(exc).__name__}: {exc}"

    print(f"  ⏳ LLM rationale (wall timeout {int(wall_timeout)}s)...", file=sys.stderr)
    t = threading.Thread(target=_do_call, daemon=True)
    t.start()
    t.join(timeout=wall_timeout)
    if t.is_alive():
        print(f"  ⚠ LLM rationale step timed out after {int(wall_timeout)}s; "
              f"showing frequency analysis only.", file=sys.stderr)
        return {}
    if "error" in result_holder:
        print(f"  ⚠ LLM rationale step failed ({result_holder['error']}); "
              f"showing frequency analysis only.", file=sys.stderr)
        return {}
    content = result_holder.get("content", "")
    if not content:
        return {}

    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(json)?\s*", "", content)
        content = re.sub(r"```\s*$", "", content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        print("  ⚠ LLM returned non-JSON; showing frequency analysis only.",
              file=sys.stderr)
        return {}

    verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(verdicts, list):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        k = v.get("key")
        if isinstance(k, str):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def run_suggest(args) -> int:
    candidates = collect_candidates(
        lookback_hours=args.lookback_hours,
        threshold=args.threshold,
        top=args.top,
        include_all=bool(getattr(args, "include_all", False)),
    )

    if not candidates:
        if getattr(args, "as_json", False):
            print("[]")
            return 0
        print(
            f"No tool-call patterns crossed the threshold of {args.threshold} "
            f"repetitions in the last {args.lookback_hours}h."
        )
        if not getattr(args, "include_all", False):
            print("Note: observation/navigation tools (read_file, memory, cd, ls,")
            print("      grep) are filtered by default. Re-run with --include-all")
            print("      to surface them too.")
        print("Try lowering --threshold or widening --lookback-hours.")
        return 0

    verdicts: Dict[str, Dict[str, Any]] = {}
    if getattr(args, "with_llm", False):
        verdicts = _annotate_with_llm(candidates)

    if getattr(args, "as_json", False):
        public = []
        for c in candidates:
            row = c.to_public()
            v = verdicts.get(c.fingerprint.key)
            if v:
                row["llm"] = v
            public.append(row)
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0

    _print_human_menu(candidates, verdicts, lookback_hours=args.lookback_hours)
    return 0


def _print_human_menu(
    candidates: List[Candidate],
    verdicts: Dict[str, Dict[str, Any]],
    *,
    lookback_hours: int,
) -> None:
    n = len(candidates)
    label = "candidate" if n == 1 else "candidates"
    src = "frequency analysis"
    if verdicts:
        src = "frequency analysis + LLM rationale"
    print(f"\n{n} hook {label} (last {lookback_hours}h, source: {src}):\n")
    print(f"{'#':>3}  {'tool / detail':<28}  {'count':>5}  {'sessions':>8}  suggested wire-up")
    print(f"{'-'*3}  {'-'*28}  {'-'*5}  {'-'*8}  {'-'*40}")

    for i, c in enumerate(candidates, 1):
        v = verdicts.get(c.fingerprint.key) or {}
        event = v.get("event") or c.fingerprint.suggested_event
        matcher = v.get("matcher")
        if matcher is None:
            matcher = c.fingerprint.suggested_matcher
        matcher_part = f" matcher={matcher!r}" if matcher else ""
        print(
            f"{i:>3}  {c.fingerprint.human[:28]:<28}  "
            f"{c.count:>5}  {c.sessions_touched:>8}  {event}{matcher_part}"
        )
        if v.get("rationale"):
            print(f"      → LLM: {v['rationale'][:200]}")

        # Scaffold-line priority:
        #   1. LLM suggested a concrete starter_template
        #   2. Deterministic best_starter_template() finds a fit
        #   3. Fall back to a custom command with whatever matcher we have
        #      (LLM-refined if available, otherwise the rule-based one)
        tpl = v.get("starter_template") if isinstance(v, dict) else None
        if not tpl:
            tpl = best_starter_template(c.fingerprint)
        if tpl:
            print(f"      → scaffold: hermes hooks new --from-template {tpl}")
        elif matcher:
            print(
                f"      → scaffold: hermes hooks new --event {event} "
                f"--matcher {shlex.quote(matcher)}"
            )

    print()
    print("Pick a candidate and run the printed `hermes hooks new` command,")
    print("or re-run with `--json` to consume programmatically, or `--with-llm`")
    print("for matcher refinement + rationale.")
