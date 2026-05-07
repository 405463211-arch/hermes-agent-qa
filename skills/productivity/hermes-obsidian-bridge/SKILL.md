---
name: hermes-obsidian-bridge
description: Use when the user asks about Obsidian, vault, syncing notes, importing markdown into hermes, exporting hermes rules to Obsidian, or how hermes integrates with their human-edited knowledge base. Operational manual for the Obsidian bridge.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [obsidian, memory, knowledge-base, integration]
    category: productivity
    related_skills: [hermes-memory-guide, hermes-self-audit]
---

# Hermes Obsidian Bridge — User Manual

## What it is

A two-way bridge between hermes' built-in memory and your Obsidian vault.
Hermes' five memory layers (RULES, MEMORY, USER, project-knowledge, LCM)
remain the canonical source of truth — the bridge just makes them
**human-readable in your editor** and lets the agent **query your
human-curated notes** on demand.

## What it is NOT

- Not a replacement for RULES.md / MEMORY.md / USER.md (those stay)
- Not an automatic sync (it's CLI / hook driven, not filewatcher-based)
- Not a way to put your private daily notes into the LLM by default
  (search scope is limited to `vault/hermes/` unless you opt in)

## Architecture in one diagram

```
~/.hermes/                          ~/Obsidian/MyVault/
├── memories/                       ├── 学习笔记/      ← your notes (read-only for hermes)
│   ├── RULES.md      ─ export ──→  ├── 项目/          ← your notes (read-only for hermes)
│   ├── MEMORY.md     ─ export ──→  ├── Daily/         ← your notes (read-only for hermes)
│   └── USER.md       ─ export ──→  │
├── learning_store.db ─ export ──→  └── hermes/        ← managed by hermes
└── project-knowledge/                  ├── README.md
                       ←─ import ──    ├── rules.md           (mirror of RULES.md, read-only)
                                       ├── memory.md          (mirror of MEMORY.md, read-only)
                                       ├── user.md            (mirror of USER.md, read-only)
                                       ├── learnings/         (one .md per LRN id)
                                       │   └── LRN-*.md
                                       ├── notes/             (agent's free-form writes)
                                       ├── ingest/            (you drop files here for the agent)
                                       ├── rules-staging.md   (you write new rules here)
                                       └── profiles/<name>/   (per-profile state)
```

## Getting started

### 1. Configure the bridge

```bash
hermes obsidian setup
```

The wizard asks for:

- **Vault path** — absolute path to the Obsidian vault root
- **Search scope** — which parts of the vault the agent can search:
  - `hermes_subdir` (default) — only `vault/hermes/`
  - `ingest` — only `vault/hermes/ingest/` (a curated whitelist)
  - `all` — the entire vault (your dailies are visible too)
- **Auto export on session end** — Y/n
- **Auto import staged rules on session start** — Y/n
- **Mirror learning store entries** — y/N

Settings are saved to `~/.hermes/config.yaml` under `obsidian:`.

### 2. Verify

```bash
hermes obsidian status
```

You should see `enabled: True`, `vault_exists: True`, scope, and
counts of ingest / staging files.

## Daily workflow

### Want hermes to use one of your existing notes

Drop a copy / symlink into `vault/hermes/ingest/`. Then ask hermes
about the topic:

```
You: "我之前写过一篇关于 PostgreSQL work_mem 调优的笔记，里面具体建议是什么？"
Agent: [calls obsidian_search → finds note → calls obsidian_view → answers]
```

### Want hermes to learn a new rule from Obsidian

Open `vault/hermes/rules-staging.md` in Obsidian. Add bullets:

```markdown
- 写代码注释一律用英文
- PR 描述必须有 testing section
- 不要主动重构未要求的代码
```

Save. On the next session start, hermes auto-imports them into
RULES.md (each carries `source: obsidian-import`). The staging file
gets cleared on success.

### Want to bulk-load a whole folder into project-knowledge

```bash
hermes pk import-from-vault hermes-agent 项目/hermes-agent
```

Copies every `.md` / `.txt` / `.rst` / `.org` file under
`vault/项目/hermes-agent/` into `~/.hermes/project-knowledge/hermes-agent/`.
The agent's project-knowledge index picks it up on next session start.

### Want to bulk-load notes into long-context memory (LCM)

```bash
hermes obsidian import-notes
```

Slices every file in `vault/hermes/ingest/` into paragraphs, embeds them,
and stores them in the LCM SQLite store. The agent can then call
`lcm_search` to find specific fragments.

### Want to push the latest hermes state into Obsidian right now

```bash
hermes obsidian export       # mirror RULES/MEMORY/USER (and learnings if enabled)
hermes obsidian sync         # also runs import-rules first
```

## Cost analysis (token consumption)

| Item | Tokens | When | Cache friendly? |
|---|---|---|---|
| `obsidian_search/view/save` schemas | ~250 once | system prompt | yes |
| `build_obsidian_prompt` instruction block | ~125 once | system prompt | yes |
| `obsidian_search` tool result | 200–800 per call | only when called | yes |
| `obsidian_view` tool result | 1k–5k per call | only when called | yes |

For a 100-turn session:

- **0 calls**: ~37k input tokens cached cost (≈ $0.011 with Claude Sonnet 4 cached pricing)
- **5 calls** (typical): same baseline + ~10k tokens of tool I/O total

Worst case is still under 1¢ for a busy session. The bridge does NOT
inject any vault content into the system prompt by default — that's
deliberate (preserves prefix cache stability).

## Configuration reference (config.yaml → obsidian)

```yaml
obsidian:
  enabled: false                       # master switch
  vault_path: ""                       # ~/Obsidian/MyVault — absolute path
  search_scope: hermes_subdir          # hermes_subdir | ingest | all
  auto_export_on_session_end: true     # mirror to vault on /quit
  auto_import_rules_on_start: true     # pull rules-staging.md on session start
  export_learnings: true               # write LRN-*.md files on each export
```

To turn the bridge off entirely without losing state:

```bash
hermes obsidian off
```

## Tools (agent-side)

Once enabled, three tools become available:

| Tool | Purpose |
|---|---|
| `obsidian_search(query, max_results)` | Substring search across the configured scope. |
| `obsidian_view(path, offset, limit)`  | Read a file (vault-relative path).             |
| `obsidian_save(path, content, mode)`  | Write to `vault/hermes/notes/<path>`.          |

The agent picks them up automatically when `obsidian.enabled: true`.
A small system-prompt block teaches the model when to reach for them
("when the user mentions notes / 笔记 / Obsidian / wrote down before").

## Anti-patterns

| Don't | Why | Do this instead |
|---|---|---|
| Edit `vault/hermes/rules.md` directly | It's a read-only mirror; next export overwrites | Use `rules-staging.md` or `/rules add` |
| Set `search_scope: all` casually | Daily notes / journals visible to the LLM | Use `ingest/` whitelist |
| Use `obsidian_save` for rules | Wrong tool — that's the memory tool's job | Use `memory(target=rules, ...)` |
| Hand-edit `vault/hermes/profiles/<x>/rules.md` to bypass profiles | Same as above | Switch profile with `hermes -p <x>` |
| Rely on real-time sync | Bridge is hook-driven, not filewatcher | Run `hermes obsidian sync` manually if needed |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `obsidian_search` returns 0 hits but the file exists | Scope too narrow | `obsidian.search_scope: ingest` or `all` |
| Staged rules not imported | Auto-import disabled or session not restarted | `hermes obsidian import-rules` |
| Vault has no `hermes/` folder | Setup wasn't run | `hermes obsidian setup` |
| Mirror file shows old content | No export ran since last memory edit | `hermes obsidian export` |
| Agent says "Obsidian bridge not configured" | `obsidian.enabled: false` | Re-run setup or set true in config.yaml |

## Profile separation

Each hermes profile (`hermes -p coder`, `hermes -p personal`, etc.)
gets its own subdirectory under `vault/hermes/profiles/<name>/` so
multiple profiles can share one vault without colliding. The default
profile uses `vault/hermes/` directly.

## See also

- `hermes-memory-guide` skill — the three-bucket memory model this bridge mirrors
- `~/.hermes/config.yaml` → `obsidian:` block — full config reference
- `agent/obsidian.py` — core module (data plane)
- `tools/obsidian_tool.py` — agent-facing tools
- `hermes_cli/obsidian_setup.py` — CLI handlers
