"""CLI handlers for ``hermes obsidian`` subcommands.

Subcommands:

  - ``hermes obsidian setup``        — interactive configuration wizard
  - ``hermes obsidian status``       — show current bridge state
  - ``hermes obsidian export``       — push hermes memory → vault
  - ``hermes obsidian import-rules`` — pull staging-area rules → RULES.md
  - ``hermes obsidian import-notes`` — push vault notes → LCM long-context
  - ``hermes obsidian sync``         — import-rules + export, in that order
  - ``hermes obsidian off``          — disable the bridge

All commands are idempotent and safe to run multiple times.  None of them
require a network connection — Obsidian itself runs locally, and the bridge
is purely filesystem-level.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public entry — dispatched from hermes_cli/main.py
# ---------------------------------------------------------------------------

def obsidian_command(args) -> None:
    """Dispatch ``hermes obsidian <sub>`` to its handler."""
    sub = getattr(args, "obsidian_command", None) or "status"
    handler = {
        "setup": _cmd_setup,
        "status": _cmd_status,
        "export": _cmd_export,
        "import-rules": _cmd_import_rules,
        "import-notes": _cmd_import_notes,
        "sync": _cmd_sync,
        "off": _cmd_off,
    }.get(sub)
    if handler is None:
        print(f"\n  Unknown obsidian subcommand: {sub}")
        print("  Available: setup, status, export, import-rules, import-notes, sync, off\n")
        return
    handler(args)


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def _cmd_setup(args) -> None:
    """Interactive configuration wizard for the Obsidian bridge."""
    from hermes_cli.config import load_config, save_config

    print("\n  Configuring Obsidian bridge:\n")

    # Step 1: Vault path
    cfg = load_config()
    obsidian_cfg = cfg.get("obsidian") or {}
    current_path = str(obsidian_cfg.get("vault_path") or "")
    suggested = current_path or _guess_vault_path()
    if suggested:
        prompt = f"  Vault path [{suggested}]: "
    else:
        prompt = "  Vault path (e.g. ~/Obsidian/MyVault): "
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return
    vault_raw = answer or suggested
    if not vault_raw:
        print("  No vault path provided. Cancelled.\n")
        return
    vault_path = Path(os.path.expanduser(vault_raw)).resolve()
    if not vault_path.is_dir():
        try:
            create = input(f"  {vault_path} does not exist. Create it? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.\n")
            return
        if create not in ("y", "yes"):
            print("  Cancelled.\n")
            return
        try:
            vault_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"  Failed to create vault directory: {exc}\n")
            return

    # Step 2: Scope
    print()
    print("  Which parts of your vault should the agent be able to search?")
    print("    1) hermes_subdir — only vault/hermes/ (recommended; private notes stay private)")
    print("    2) ingest         — only vault/hermes/ingest/ (curated whitelist)")
    print("    3) all            — the whole vault (broadest; daily notes included)")
    try:
        choice = input("  Choice [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return
    scope = {"1": "hermes_subdir", "2": "ingest", "3": "all"}.get(choice, "hermes_subdir")

    # Step 3: Lifecycle hooks
    try:
        auto_export = (input("  Auto-export hermes memory to vault on session end? [Y/n]: ").strip().lower()
                       or "y") in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return
    try:
        auto_import = (input("  Auto-import staged rules from vault on session start? [Y/n]: ").strip().lower()
                       or "y") in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return
    try:
        export_learnings = (input("  Mirror learning store entries to vault? [y/N]: ").strip().lower()
                            or "n") in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return

    # Persist
    cfg.setdefault("obsidian", {})
    cfg["obsidian"].update({
        "enabled": True,
        "vault_path": str(vault_path),
        "search_scope": scope,
        "auto_export_on_session_end": auto_export,
        "auto_import_rules_on_start": auto_import,
        "export_learnings": export_learnings,
    })
    save_config(cfg)

    # Bootstrap the hermes/ subdir + a README so the user knows what's what
    _bootstrap_vault(vault_path)

    # Run a first export so the user has something to look at right away
    try:
        from agent import obsidian as ob
        result = ob.export_memory_files()
        if result.error:
            print(f"\n  ⚠ Initial export failed: {result.error}")
        else:
            print(f"\n  ✓ Wrote {len(result.files_written)} memory file(s) to vault/hermes/")
    except Exception as exc:
        print(f"\n  ⚠ Initial export failed: {exc}")

    print("\n  ✓ Obsidian bridge configured")
    print(f"    Vault:        {vault_path}")
    print(f"    Search scope: {scope}")
    print(f"    Auto export:  {auto_export}")
    print(f"    Auto import:  {auto_import}")
    print()
    print("  Open the vault in Obsidian and you'll see a new `hermes/` folder.")
    print("  Use `hermes obsidian status` to inspect the bridge anytime.\n")


def _guess_vault_path() -> str:
    """Best-effort vault path detection (only looks for typical defaults)."""
    home = Path.home()
    candidates = [
        home / "Obsidian",
        home / "Documents" / "Obsidian",
        home / "iCloud Drive (Archive)" / "Obsidian",
        home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents",
    ]
    for cand in candidates:
        if cand.is_dir():
            # If the user has multiple vaults, pick the first subdir that
            # looks like one (contains a .obsidian folder)
            for child in sorted(cand.iterdir()):
                if (child / ".obsidian").is_dir():
                    return str(child)
            return str(cand)
    return ""


def _bootstrap_vault(vault_path: Path) -> None:
    """Create vault/hermes/ with a README explaining the layout."""
    from agent.obsidian import (
        HERMES_SUBDIR,
        INGEST_DIRNAME,
        NOTES_DIRNAME,
        LEARNINGS_DIRNAME,
        STAGING_RULES_FILENAME,
    )

    hermes_dir = vault_path / HERMES_SUBDIR
    hermes_dir.mkdir(parents=True, exist_ok=True)
    (hermes_dir / NOTES_DIRNAME).mkdir(exist_ok=True)
    (hermes_dir / INGEST_DIRNAME).mkdir(exist_ok=True)
    (hermes_dir / LEARNINGS_DIRNAME).mkdir(exist_ok=True)

    staging = hermes_dir / STAGING_RULES_FILENAME
    if not staging.exists():
        staging.write_text(
            "<!-- Add new rules below as bullet points; they will be imported "
            "into RULES.md on the next `hermes obsidian import-rules` (or at "
            "session start if auto-import is on). -->\n\n"
            "- \n",
            encoding="utf-8",
        )

    readme = hermes_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            "# hermes/ — auto-managed by Hermes Agent\n\n"
            "This subfolder is **not** edited by you (mostly). It mirrors hermes' "
            "own state into your Obsidian vault so you can see it, search it, and "
            "back it up alongside your other notes.\n\n"
            "## What's in here\n\n"
            "- `rules.md` — mirror of RULES.md (mandatory protocols). **Read-only;** "
            "  edit via `/rules add` in hermes or via `rules-staging.md` below.\n"
            "- `memory.md` — mirror of MEMORY.md (working notes).\n"
            "- `user.md` — mirror of USER.md (user profile).\n"
            "- `learnings/` — one Markdown file per learning_store entry (LRN-* ids).\n"
            "- `notes/` — free-form notes the agent saves with `obsidian_save`.\n"
            "- `ingest/` — files **you** drop here are visible to the agent via "
            "  `obsidian_search` (when search_scope = 'ingest').\n"
            "- `rules-staging.md` — write new rules as bullet points; they get "
            "  imported into RULES.md on next session start.\n"
            "- `profiles/<name>/` — per-profile state (when running with "
            "  `hermes -p <profile>`).\n\n"
            "## Workflow\n\n"
            "1. **Want hermes to learn a new rule?** Add a bullet to `rules-staging.md`. "
            "   Next time hermes starts, the rule is in RULES.md.\n"
            "2. **Want hermes to use one of your existing notes?** Drop a copy or "
            "   symlink it into `ingest/`. The agent can then find it via "
            "   `obsidian_search`.\n"
            "3. **Want to bulk-load notes into a project knowledge tree?** Use "
            "   `hermes pk import-from-vault <project> <vault-folder>`.\n\n"
            "Run `hermes obsidian status` for the live state.\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _cmd_status(args) -> None:
    from agent import obsidian as ob

    info = ob.status()
    print("\n  Obsidian bridge status:\n")
    print(f"    enabled:     {info.get('enabled')}")
    print(f"    vault_path:  {info.get('vault_path') or '(not set)'}")
    print(f"    vault_exists:{info.get('vault_exists')}")
    print(f"    scope:       {info.get('search_scope')}")
    if info.get("vault_exists"):
        print(f"    hermes_dir:  {info.get('hermes_dir')}")
        print(f"    ingest:      {info.get('ingest_files', 0)} file(s)")
        print(f"    staging:     {info.get('staging_pending_lines', 0)} pending line(s)")
    print()
    if not info.get("enabled"):
        print("  Run `hermes obsidian setup` to configure.\n")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _cmd_export(args) -> None:
    from agent import obsidian as ob
    from hermes_cli.config import load_config

    if not ob.is_enabled():
        print("\n  Obsidian bridge not configured. Run `hermes obsidian setup`.\n")
        return

    print("\n  Exporting hermes memory to vault...")
    result = ob.export_memory_files()
    if result.error:
        print(f"  ✗ {result.error}\n")
        return
    for path in result.files_written:
        print(f"    ✓ {path}")
    for skipped in result.skipped:
        print(f"    · skipped: {skipped}")

    cfg = load_config()
    if (cfg.get("obsidian") or {}).get("export_learnings", False):
        print("\n  Exporting learning store...")
        l_result = ob.export_learnings()
        if l_result.error:
            print(f"  ✗ {l_result.error}")
        else:
            print(f"  ✓ Wrote {len(l_result.files_written)} learning file(s)")
            if l_result.skipped:
                print(f"    skipped {len(l_result.skipped)}")

    print()


# ---------------------------------------------------------------------------
# import-rules
# ---------------------------------------------------------------------------

def _cmd_import_rules(args) -> None:
    from agent import obsidian as ob

    if not ob.is_enabled():
        print("\n  Obsidian bridge not configured. Run `hermes obsidian setup`.\n")
        return

    print("\n  Importing staged rules from vault...")
    result = ob.import_rules_from_staging()
    if result.error:
        print(f"  ✗ {result.error}\n")
        return
    if not result.rules_added and not result.rules_skipped:
        print("  · staging area is empty — nothing to import.\n")
        return
    for rule in result.rules_added:
        print(f"    ✓ added: {rule[:80]}")
    for skipped in result.rules_skipped:
        print(f"    · skipped: {skipped}")
    print()


# ---------------------------------------------------------------------------
# import-notes (vault → LCM)
# ---------------------------------------------------------------------------

def _cmd_import_notes(args) -> None:
    """Slice + embed all ingest notes into the LCM long-context store."""
    from agent import obsidian as ob

    if not ob.is_enabled():
        print("\n  Obsidian bridge not configured. Run `hermes obsidian setup`.\n")
        return

    files = ob.list_ingest_files()
    if not files:
        print("\n  No files in vault/hermes/ingest/. Drop markdown files there to import.\n")
        return

    print(f"\n  Found {len(files)} file(s) in ingest/. Importing into LCM...")

    # Lazy import — LCM is an optional plugin, may not be installed
    try:
        from plugins.context_engine.lcm.embedder import get_default_embedder
        from plugins.context_engine.lcm.store import ChunkStore
    except Exception as exc:
        print(f"  ✗ LCM context engine unavailable: {exc}\n")
        return

    try:
        from hermes_constants import get_hermes_home
        db_path = get_hermes_home() / "lcm" / "store.db"
        embedder = get_default_embedder()
        store = ChunkStore(db_path=db_path)
    except Exception as exc:
        print(f"  ✗ Failed to initialise LCM store: {exc}\n")
        return

    imported = 0
    failed: list = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            # Chunk by paragraph to keep embeddings meaningful
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            if not paragraphs:
                continue
            embeddings = embedder.embed(paragraphs)
            chunks = [
                {
                    "role": "obsidian_ingest",
                    "content": p,
                    "chunk_type": "obsidian_ingest",
                }
                for p in paragraphs
            ]
            store.add(
                session_id=f"obsidian-ingest:{path.name}",
                chunks=chunks,
                embeddings=embeddings,
                embedder_name=embedder.name,
            )
            imported += 1
        except Exception as exc:
            failed.append(f"{path.name}: {exc}")

    print(f"  ✓ Imported {imported}/{len(files)} file(s) into LCM")
    if failed:
        print("  Failures:")
        for entry in failed[:10]:
            print(f"    · {entry}")
    print("\n  Use `lcm_search` from inside the agent to query.\n")


# ---------------------------------------------------------------------------
# sync (import + export)
# ---------------------------------------------------------------------------

def _cmd_sync(args) -> None:
    print("\n  Running full sync (import-rules → export)...")
    _cmd_import_rules(args)
    _cmd_export(args)


# ---------------------------------------------------------------------------
# off
# ---------------------------------------------------------------------------

def _cmd_off(args) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    if not isinstance(cfg.get("obsidian"), dict):
        cfg["obsidian"] = {}
    cfg["obsidian"]["enabled"] = False
    save_config(cfg)
    print("\n  ✓ Obsidian bridge disabled. Vault files are untouched.\n")
