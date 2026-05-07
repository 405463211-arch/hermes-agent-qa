---
name: whitebox-qa-review
description: Use when reviewing already-implemented features (yours or someone else's) for production readiness. Senior-QA-level white-box + integration testing methodology — finds real bugs, locks invariants, separates new regressions from baseline noise.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [testing, qa, whitebox, regression, code-review, quality]
    related_skills: [test-driven-development, systematic-debugging, requesting-code-review]
---

# White-Box QA Review

## Overview

TDD prevents bugs in code you write. This skill catches bugs in code you've **already written** (or someone else has) before it ships.

**Core principle:** Read the actual implementation. Find the invariants the code is relying on. Lock them in tests so future refactors can't silently break them.

**This is the methodology:** structured, layered, baseline-aware testing that finds real bugs without drowning in pre-existing noise.

## When to Use

**Always:**
- Before merging a feature PR (especially large ones)
- After completing your own multi-file feature
- When reviewing someone else's PR with significant logic changes
- When the user asks for "comprehensive testing" or "QA review"

**Especially valuable when:**
- The change spans multiple layers (data → tools → agent → CLI → gateway)
- The codebase has a known baseline of pre-existing failures
- You need to ship with high confidence

**Skip when:**
- Single-file trivial change
- Pure documentation
- Throwaway prototype

## The Layered Testing Matrix

Don't test everything at once. Decompose by **layer of responsibility**, then walk bottom-up. The layers below are the canonical 11; adapt names to your stack.

| Layer | Tests | Examples |
|---|---|---|
| M0 Infrastructure | Config keys exist, types correct, registries populated | DEFAULT_CONFIG has new keys; tools registered; plugins discoverable |
| M1 Data storage | Pure functions, parse/serialize round-trip, persistence | parse_X / serialize_X / SQLite CRUD |
| M2 Tools | Each tool's input validation + JSON contract + error paths | tool_handler returns valid JSON, rejects bad input |
| M3 Agent integration | System prompt assembly, tool dispatch routing | injection order, dispatch passes `store=` kwarg |
| M4 Plugin coupling | Hook registration + isolation + throttling | plugin doesn't crash on weird input, sessions don't leak |
| M5 CLI / UX | Slash command registration + handlers | command in registry; dispatch branch exists |
| M6 Config / switches | Defaults + user overrides + each switch actually disables | `feature=false` → no-op end-to-end |
| M7 Gateway coupling | Messaging-platform exposure + dispatch | command in GATEWAY_KNOWN_COMMANDS, help text covers it |
| M8 Boundary / exception | Empty files, garbled metadata, concurrent writers, unavailable deps | 0-byte file → no crash; 50 thread writes → no lost updates |
| M9 Performance / resources | Scaling, no quadratic blowup, no deadlock, no leak | N=10/100/500 ratio bounded; concurrent reads consistent |
| M10 Full regression | Whole suite N times vs baseline | failures match baseline cluster-for-cluster; zero flaky over 3 runs |
| M11 Compress / cache windows | The ONE legal mid-session rebuild path's invariants (if your system has them) | snapshot frozen / load_from_disk refreshes / order preserved |
| M12 Cross-process concurrency | True multi-process file/DB contention | spawn N processes, no data loss, no corruption |
| M13 Volume / scaling | 5k+ entries (real long-term users) | render < 5s, ratio < 15x vs 1k, archive at scale |
| M14 Profile / multi-tenant isolation | Each tenant's HERMES_HOME / equivalent stays isolated | env-var swap → all paths follow |
| M15 Data migration | Old format / mixed format / missing columns | round-trip works; legacy IDs queryable; future-compat keys preserved |
| **M16 Contract** | **Cross-module promises** | **handler returns valid JSON / store dedupes / hooks never propagate / config keys reachable** |

**Why bottom-up:** if M1 (data) is broken, every higher layer's test will fire spurious failures. Fix data first, then move up.

**Don't skip layers**, even when you're sure they're fine — the act of writing the probe forces you to read the code, and reading the code is where the bugs reveal themselves.

## The Methodology — 11 Techniques

### 1. Static Analysis as a Test

When the code path is hard to instantiate (needs a full agent, a real DB, a network), test the **source code text** instead.

```python
def test_dispatch_passes_store_to_learning_record():
    src = (REPO / "run_agent.py").read_text()
    idx = src.find('elif function_name == "learning_record":')
    assert idx > 0
    window = src[idx : idx + 600]
    assert "store=self._memory_store" in window, (
        "main-path learning_record dispatch lost the store kwarg!"
    )
```

This locks **structural invariants** that would otherwise be invisible. Future refactor forgets the kwarg → test fails immediately.

### 2. Baseline Locking for Pre-Existing Issues

When you find a violation that already existed on `main`, you have two bad options and one good one:

- ❌ Fail the test → blocks unrelated work
- ❌ Skip the check entirely → silently accumulates more violations
- ✅ **Lock a baseline set** → existing violations allowed; **new** violations fail

```python
# Pre-existing violations (sorted, frozen). Touching this set must be
# justified: either fixing one of them (then remove the entry), or
# discovering a new branch that legitimately crosses the boundary.
BASELINE_BAREWORD: frozenset[tuple[str, str]] = frozenset({
    ("browser_navigate", "web_search"),
    ("clarify_tool", "ask_user"),
    # ...
})

def test_no_new_cross_tool_references():
    violations = scan_repo()
    new = violations - BASELINE_BAREWORD
    assert not new, f"NEW violations introduced: {new}"

def test_baseline_violations_still_present():
    """Symmetric guard — if you fix one, sync the baseline."""
    violations = scan_repo()
    fixed = BASELINE_BAREWORD - violations
    assert not fixed, (
        f"Baseline drift: {fixed} no longer apply. Remove them from "
        f"BASELINE_BAREWORD."
    )
```

Both directions matter. Asymmetric baseline = silent rot.

### 3. Invariants Over Snapshots

Per AGENTS.md: tests that read like a snapshot of current data are **change-detectors** — they fail every time data changes legitimately, costing engineering time. Write **relationship** tests instead.

```python
# ❌ Change-detector
assert _PROVIDER_MODELS["gemini"] == ["gemini-2.5-pro", "gemini-3-flash"]

# ✅ Invariant
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1
for m in _PROVIDER_MODELS["gemini"]:
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER  # contract
```

The rule: **does this test fail when data legitimately changes?** If yes, it's testing the wrong thing.

### 4. Bug Found → Fix + Lock In One Cycle

When a probe surfaces a real bug, do all three in the same commit:

1. Reproduce as a failing test (the probe that found it)
2. Fix the production code
3. Verify the test now passes

This guarantees a **regression test exists** for every fix. Future refactor that re-introduces the bug fails immediately.

### 5. Sandbox Workarounds Beat Permission Escalation

When tests hit sandbox restrictions:

- ❌ Request `all` permission → user-facing prompt, slow, dangerous
- ✅ Pre-set environment to redirect writes to `/tmp` → invisible, safe, fast

```bash
export HERMES_HOME=/tmp/hermes-test-$$
mkdir -p $HERMES_HOME
scripts/run_tests.sh tests/...
```

The `conftest.py` `_isolate_hermes_home` fixture redirects mid-test, but **module imports run before fixtures**. Pre-setting the env var redirects from line one.

### 6. Baseline Verification Before Reporting Failures

Before claiming "this branch broke X":

```bash
git stash                                   # set aside current work
scripts/run_tests.sh tests/path/that/failed
git stash pop                               # restore
```

If the same tests fail on clean `main`, **it's not your regression**. Document as baseline, move on.

This single workflow has saved hours of false-alarm debugging.

### 7. Stability Verification — N Runs, Zero Drift

Tests passing once is necessary but not sufficient. Flakiness only shows up under repeat runs.

```bash
for i in 1 2 3; do
  echo "=== run #$i ==="
  scripts/run_tests.sh tests/_white_box/ tests/<module>/ 2>&1 | tail -3
done
```

Three runs, identical pass count, zero flaky → stable. Different counts → root-cause the flake before merging.

### 8. Bug Severity Triage

Classify every finding so the report communicates priority:

- 🔴 **High** — breaks main flow / silent data corruption / security
- 🟡 **Medium** — convention / contract violation, doesn't break runtime today
- 🟢 **Low** — cosmetic / minor improvement

```markdown
### 🐛 [BUG-M8-1] — LCM exception escapes run_auto_archive

**Severity**: 🔴 High
**Location**: `tools/memory_tool.py:run_auto_archive()`
**Symptom**: Comment promises silent failure but no outer try/except…
**Fix**: Wrap the call in try/except + debug log
**Regression test**: M8 `TestLCMUnavailable.test_index_archive_to_lcm_silent_on_error`
```

The "Regression test" line is mandatory — it forces the fix-and-lock pairing.

### 9. Threshold Is Approximate, Direction Is Strict

Performance probes shouldn't be micro-benchmarks (machine-dependent). They should catch **direction changes**.

```python
def test_scaling_is_subquadratic():
    t100 = time_render(100)
    t500 = time_render(500)
    ratio = t500 / max(t100, 1e-6)
    # Quadratic = 25x; allow up to 15x as a regression bound. Linear = ~5x.
    assert ratio < 15, f"Ratio {ratio:.1f} suggests quadratic"
```

Generous absolute thresholds + tight ratio bounds = catches quadratic regressions without flaking on a slow CI box.

### 10. Contract Testing — Lock Cross-Module Promises

Unit tests answer "did I compute right?" Integration tests answer "does the
system run?" **Contract tests** answer the third, often-skipped question:
**"if I behave badly, does my consumer survive?"** They lock the *promises*
one module makes to another into executable form.

Five canonical contracts you should always lock when systems touch each other:

| Contract surface | The promise to lock |
|---|---|
| Tool/handler registry → caller | "Every handler returns a JSON string. Even with garbage args. Never raises." |
| Cached/snapshot store → consumer | "Same input → byte-identical output. Empty → returns None, not exception." |
| Persistent store → tools | "Same dedup key → UPDATE not INSERT. Every result has the flags consumers depend on." |
| Plugin/hook system → main loop | "invoke() never propagates exceptions. Bad plugins don't kill good ones. Unknown hooks return []." |
| DEFAULT_CONFIG ↔ code reads | "Every key the code reads exists in defaults. Every default has the right type and range." |

```python
# Contract: tool handler always returns valid JSON, even with garbage args
@pytest.mark.parametrize("bad_args", [{}, {"unknown": "x"}, {"target": None}])
def test_handler_garbage_in_json_out(bad_args):
    for name in SAMPLE_TOOLS:
        try:
            result = registry.get_entry(name).handler(bad_args)
        except Exception as exc:
            pytest.fail(f"{name} raised {exc!r} — must wrap errors")
        json.loads(result)  # must parse

# Contract: plugin hook never leaks exceptions — bad plugin can't kill loop
def test_invoke_hook_isolates_failures():
    def bad(**kw): raise RuntimeError("on fire")
    def good(**kw): return {"answer": 42}
    mgr._hooks["test"] = [bad, good]
    results = mgr.invoke_hook("test")
    assert {"answer": 42} in results  # good survived bad's failure

# Contract: alias never collides with a canonical name in another command
def test_no_alias_collides():
    canonicals = {c.name for c in REGISTRY}
    for c in REGISTRY:
        for alias in c.aliases:
            assert alias not in canonicals, f"alias {alias} shadows {c.name}"
```

Contract tests run fast (no real I/O), catch the most painful class of
regression (silent contract violations across module seams), and cost very
little to maintain because they only break when the contract genuinely
breaks. They are the cheapest defensive layer in the matrix.

### 11. The Report Speaks for the Tests

After all 11 layers, write a report following this structure:

```markdown
# Test Report — <feature>

## Conventions
<test wrapper, env vars, common fixture pattern>

## Module progress

| Module | Layer | Status | Notes |
|---|---|---|---|
| M0 | Infrastructure | ✅ pass | 21/21 white-box |
| ...

## M0 — Infrastructure
- Test scope
- Existing-suite results
- White-box probe results (table by class)
- Key invariants guarded
- Bugs found (with severity tag)

## MX — Summary
- 🐛 Bug list (table)
- 📋 Known risks (intentionally unfixed)
- ✅ Invariants guarded (numbered)
- 📊 Test matrix totals
- 🎯 Stability proof (N-run results)
```

The report's job is to make the **next** reviewer's job 10x easier — they should be able to scan in 5 minutes.

## The Iron Loop

For each layer M0..M16:

```
1. Read the code in that layer (use Grep + Read, don't guess)
2. Identify 3-7 invariants the code relies on
3. Write a probe in tests/_white_box/<layer>_probe.py
4. Run it — investigate every failure
5. For each real bug:
   - Fix production code
   - Confirm probe now passes (regression locked)
   - Tag in report with severity
6. For each baseline issue:
   - Lock with BASELINE_<KIND> set
   - Note in report under "Known risks"
7. Run existing related test suite — confirm no regression
8. Update report module section
```

Don't skip steps. Don't merge layers. Don't move to M(N+1) until M(N) is green.

## Hermes Agent Integration

### Use the test wrapper

```python
terminal("scripts/run_tests.sh tests/_white_box/m0_probe.py")
# Pre-set HERMES_HOME for tests that bring up the agent
terminal(
    "export HERMES_HOME=/tmp/hermes-qa-$$ && mkdir -p $HERMES_HOME && "
    "scripts/run_tests.sh tests/agent/test_prompt_builder.py"
)
```

### Use Grep + Read for probe construction

```python
# Find the invariant first
grep("def format_rules_by_tier", glob="tools/**/*.py")
read("tools/memory_tool.py", offset=1018, limit=60)
# Then write the probe that locks it
```

### With delegate_task

When dispatching the QA pass to a subagent:

```python
delegate_task(
    goal="Run a senior-QA white-box review of <feature> per the whitebox-qa-review skill",
    context="""
    Follow whitebox-qa-review skill. Decompose into M0..M10 layers
    (plus M11..M16 if the system has cache windows, multi-process I/O,
    multi-tenancy, migration history, or formal cross-module contracts).
    For each layer: read the code, identify invariants, write white-box
    probes in tests/_white_box/, run them, fix any bugs found, lock
    regressions with tests, update TEST_REPORT.md.

    Run scripts/run_tests.sh (NOT pytest directly).
    Pre-set HERMES_HOME=/tmp/... to avoid sandbox traps.
    Verify all "new" failures against baseline (git stash + re-run on main).
    Keep a 3-run stability check at the end.
    """,
    toolsets=["file", "terminal"]
)
```

### With systematic-debugging

When a probe surfaces a bug, switch into systematic-debugging to root-cause it before applying the fix. Then come back, write the regression test, re-run the probe.

## Common Pitfalls

| Pitfall | Why it bites | Fix |
|---|---|---|
| Skipping M0 ("config is trivial") | Misses missing keys / wrong types — every higher layer fires false errors | Always do M0 first |
| Treating sandbox failures as regressions | Wastes hours chasing nothing | git stash → re-run on main |
| Failing on existing violations | Blocks unrelated work | BASELINE locking |
| Snapshot tests | Fail every time data changes | Test relationships, not data |
| Single test run | Misses flakiness | 3 runs minimum |
| Bug fix without regression test | Bug returns | Fix-and-lock in one commit |
| Asking for `all` permission | Slow + risky | Pre-set HERMES_HOME |
| Mixed up failure clusters | "It's all broken!" panic | Cluster failures by file, then triage |
| Module progression skipped | Hidden coupling between layers | Bottom-up, no skipping |
| Report dumps raw output | Reviewer can't navigate | Structured per-layer + summary table |

## Verification Checklist

Before marking the QA review complete:

- [ ] Read the actual implementation for every layer (not guessed)
- [ ] White-box probe exists for every layer M0..M9 (always) and M11..M16 (when applicable: cache windows, multi-process, scale, multi-tenant, migration, contracts)
- [ ] Every probe passes
- [ ] Every bug found has a fix + regression test in the same commit
- [ ] Every "new" failure verified against baseline (git stash workflow)
- [ ] Pre-existing violations locked in BASELINE_* sets (both directions)
- [ ] Existing test suites for affected modules show no regression
- [ ] Stability run done — 3 iterations, identical results
- [ ] Report has every section: conventions / progress table / per-module / summary
- [ ] Bug list classified by severity 🔴/🟡/🟢
- [ ] Known risks documented (not silently swept under)

Can't check all boxes? You skipped a layer. Go back.

## Final Rule

```
Every fix → regression test
Every "new" failure → baseline-verified
Every layer → invariant locked
```

No claim of "ready to ship" without all three.
