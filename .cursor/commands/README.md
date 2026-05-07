# Cursor Commands — hermes-agent 调试 Skills

> 这套斜杠命令**只服务于在 Cursor 里调试 hermes-agent**。Hermes 本身不会读取 `.cursor/` 下任何东西（已删除 `.cursor/rules/`，`.cursor/commands` 和 `.cursor/docs` 也无 Python 代码引用）。

## 这是什么

**主线三件套**（hermes 调试闭环）+ **两个独立工作流**（GitHub push / upstream merge）+ 一份索引：

| 文件 | 命令 | 阶段 | 做什么 |
|---|---|---|---|
| `sp_brainstorm.md` | `/sp_brainstorm` | 1. 设计 | 苏格拉底式提问 → 多方案 → 架构红线检查 → 落地设计文档 |
| `sp_plan.md` | `/sp_plan` | 2. 计划 | 把方案拆成 2-5 分钟小任务，每任务带精确路径 + 验证命令 |
| `sp_execute.md` | `/sp_execute` | 3-7. 执行 | 分批执行 + 检查点 + 双向落盘(plan 状态 + execute 进度) |
| `sp_sync_github.md` | `/sp_sync_github` | 独立（出站） | 安全把本地工程同步到 GitHub：密钥扫描 / shallow 修复 / fork README 冲突 / commit 身份等 |
| `sp_sync_upstream_release.md` | `/sp_sync_upstream_release` | 独立（入站） | 安全把上游新发布版（v0.x.0 tag）合并到本地 fork：worktree 隔离 / 防 hermes update 抢资源 / 11 类冲突决策 / 回归测试 |
| `README.md` | — | 索引 | 全套使用指南（本文件） |

## 三件套工作流

```
┌────────────────────────────────────────────────────────────┐
│                hermes 调试 / 改动闭环                       │
│                                                             │
│   阶段 1 设计           阶段 2 计划         阶段 3-7 执行    │
│  ┌──────────────┐     ┌──────────┐     ┌──────────────┐   │
│  │/sp_brainstorm│ ──► │ /sp_plan │ ──► │ /sp_execute  │   │
│  │  方案对比     │     │ 任务拆分  │     │ 批次执行+落盘 │   │
│  └──────┬───────┘     └─────┬────┘     └──────┬───────┘   │
│         │                   │                  │            │
│         ▼                   ▼                  ▼            │
│  _brainstorm.md  ──►  _plan.md     ──►   _execute.md       │
│  （同一个 .cursor/docs/YYYY-MM-DD-<topic>/ 文件夹）          │
└────────────────────────────────────────────────────────────┘
```

## 文档落盘约定

> **所有过程文档都落到 `.cursor/docs/`，不会被 hermes 加载。**

```
.cursor/docs/
  YYYY-MM-DD-<topic>/                        ← 一个改动一个文件夹
    YYYY-MM-DD-<topic>_brainstorm.md         ← /sp_brainstorm 产出
    YYYY-MM-DD-<topic>_plan.md               ← /sp_plan 产出
    YYYY-MM-DD-<topic>_execute.md            ← /sp_execute 产出
```

三个文件**共用同一日期前缀**，便于追踪同一改动的设计 → 计划 → 执行链路。

## 使用建议

### 场景一：完整闭环（推荐用于核心改动）

```
1. /sp_brainstorm <主题>     → 出 _brainstorm.md
2. /sp_plan <brainstorm 路径> → 出 _plan.md
3. /sp_execute <plan 路径>    → 出 _execute.md，分批执行直到完工
```

适合：

- 修复涉及核心环路（`run_agent.py` / `cli.py` / `agent/prompt_builder.py`）的 Bug
- 新增工具 / 斜杠命令 / Gateway 平台 / Plugin
- 任何会影响 system prompt / toolset / profile 路径的改动
- 跨多个模块的重构

### 场景二：小改动 / 快路径

如果改动 30 秒能说清且只动一个文件，直接动手即可，不用强行套三件套。

但**任何会影响以下任意一项**的改动，都建议至少跑 `/sp_brainstorm`：

- system prompt 装配 / toolset 列表 / memory 加载（**prompt cache 红线**）
- HERMES_HOME 路径 / profile 隔离（**profile 红线**）
- 核心文件被 plugin 改（**插件红线**）
- 测试断言固定模型名 / version 数字 / 列表长度（**change-detector 红线**）
- 引入新的第三方依赖（**先征得用户同意 + 同步 requirements.txt**）

### 场景三：Bug 修复（推荐）

```
1. /sp_brainstorm <现象 + 触发链路>
   → 现象、根因假设、复现命令、最小修复方案
2. /sp_plan <brainstorm 路径>
   → 任务 F0: 写复现用例（必须 RED）
     任务 F1: 最小修复（让用例 GREEN）
     任务 F2: 回归保障（相关目录 / 全量）
3. /sp_execute <plan 路径>
```

**铁律：复现失败就别"试着修一下"，回去重新分析现象。**

### 场景四：把本地工程同步到 GitHub（独立工作流，不走三件套）

```
/sp_sync_github                              # 默认推到当前 origin
/sp_sync_github 推到我的新仓库 git@github.com:xxx/yyy.git
```

适用：

- 第一次把本地工程推到一个**新建的 GitHub 仓库**（处理 README 冲突、Initial commit）
- 本地有大量 untracked / modified 文件，需要先做密钥安全扫描再 push
- 上游 hermes-agent 出新版本前，先把本地 wip 推到 fork 备份避免被覆盖
- 仓库是 shallow clone（`.git/shallow` 存在）导致 push 失败（`did not receive expected object`）
- 推完发现 GitHub contribution graph 不增加（committer 身份没配对）

**核心铁律**：

- 公开仓库先扫密钥再 push（不可逆）
- "点开头" ≠ "隐私"（`.gitignore` `.envrc` `.github/` 等本来就该公开）
- `git fetch --unshallow upstream` **只补 git 数据库，不动代码**
- shallow clone 不能 push，必须先 unshallow

完整流程见 `sp_sync_github.md`。

## hermes-agent 架构红线（设计 / 计划 / 执行全程对照）

完整清单见各 skill 文件，这里给一份速查：

| 红线 | 说明 | 文档参考 |
|---|---|---|
| **prompt cache** | 不在会话中途改 history / 切 toolset / 重建 system prompt（除压缩外） | `AGENTS.md` Important Policies |
| **profile 路径** | `get_hermes_home()` / `display_hermes_home()`，禁止 `Path.home()/".hermes"` | `AGENTS.md` Profiles |
| **插件不碰核心** | `plugins/` 不许改 `run_agent.py`/`cli.py`/`gateway/run.py`/`hermes_cli/main.py` | `AGENTS.md` Plugins |
| **测试用 wrapper** | `scripts/run_tests.sh`，**不**直接 `pytest`（CI parity） | `AGENTS.md` Testing |
| **不写 change-detector** | 不断言 `_config_version == N` / 模型名快照 / 列表长度 | `AGENTS.md` Testing |
| **新依赖须询问** | 安装前先问用户 + 安装后同步 `requirements.txt` | `AI_DEV_RULES.md` 依赖管理（如果存在）|

## 隔离保证

- **`.cursor/rules/` 已删除** → Hermes 的 `_load_cursorrules` glob 落空
- **`.cursor/commands/` / `.cursor/docs/` / `.cursor/prompts/`** → 全仓 ripgrep 无 Python 代码引用，Hermes 完全不读
- **想要绝对保险**？启动 hermes 时加 `--ignore-rules` 或永久 `export HERMES_IGNORE_RULES=1`，会跳过所有自动 context 注入

## 参考

- `AGENTS.md`（项目根） — hermes-agent 工程结构、AIAgent 类、工具系统、CLI、TUI、Plugin、Skill、profile、已知坑、测试规范（**调试前必读**）
- `WORK_IN_PROGRESS.md`（项目根） — 当前正在进行的工作（如果有）
- `agent/prompt_builder.py` — 看 system prompt 怎么装配（动 prompt 必看）
- `hermes_cli/commands.py` — `COMMAND_REGISTRY`，所有斜杠命令的单一来源
- `tools/registry.py` — 工具自动发现与分发
- `gateway/run.py` — 消息守卫、命令分发、active session 拦截
