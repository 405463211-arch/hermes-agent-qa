# .cursor/prompts/ — 工程方法学 + 升级笔记

> 这个目录存放**长期参考**型的工程文档：方法学、升级笔记、版本沉淀。
>
> 与 `.cursor/docs/`（单次改动的过程产出）和 `.cursor/commands/`（流程 skills）不同——本目录的文件是**事后归档、跨 session 翻阅**用的。
>
> Hermes 本身**不会**读取本目录任何内容（与整个 `.cursor/` 同属 IDE 辅助目录）。

## 索引

| 文件 | 主题 | 何时翻 |
|---|---|---|
| `layered-instructions.md` | 长指令文件分层加载方法学（AGENTS.md / SKILL.md 为什么短 + 怎么拆） | 改 `AGENTS.md` 或某个 `SKILL.md` 前必读；想优化其他长 markdown 也参考这套 |
| `v0.12.0_upgrade_notes.md` | 2026-05-07 合并上游 v0.12.0 的全部改动清单 + 注意事项 | 排查"v0.12.0 之后某个行为变了/坏了"；下次升级前读一遍套路 |

## 与其他目录的关系

```
.cursor/commands/                ← 工作流 skills（"怎么做"，主动触发）
.cursor/docs/                    ← 单次改动的 brainstorm/plan/execute 三件套（过程产出，会落盘）
.cursor/prompts/                 ← 方法学 + 升级笔记（事后归档，长期参考）  ← 你在这里
docs/agents/                     ← 给 AI agent 看的开发规范（hermes 不读，但 AGENTS.md 索引到这里）
```

## 命名约定

| 主题 | 命名 |
|---|---|
| 方法学/范式（与版本无关） | `<topic>.md`（如 `layered-instructions.md`） |
| 版本升级笔记 | `v<X.Y.Z>_upgrade_notes.md`（如 `v0.12.0_upgrade_notes.md`） |
| 事故复盘 | `incident_<YYYY-MM-DD>_<short-name>.md` |
