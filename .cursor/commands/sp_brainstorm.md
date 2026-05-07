# /sp_brainstorm — hermes-agent 调试 / 改动头脑风暴

> **基于 superpowers 工作流方法论 · 适配 hermes-agent（Python 多模块 / 非 Django）工程**
>
> 在动手改 hermes 代码之前，通过苏格拉底式提问澄清需求、定位模块、对齐架构红线。

## 使用方式

```
/sp_brainstorm <调试目标 或 功能描述>
/sp_brainstorm 修复 gateway telegram 平台 token 锁竞态
/sp_brainstorm 给 tools/ 加一个新的 markdown 转 PDF 工具
/sp_brainstorm 让 /skin 命令支持运行时切换且不破坏 prompt cache
```

## 何时使用

**任何动 hermes 代码之前都建议先跑一遍**，尤其是：

- 修 Bug 但不确定根因（先澄清现象 → 再假设 → 不要瞎改）
- 新增工具 / 斜杠命令 / Gateway 平台 / Plugin
- 改动 `run_agent.py` / `cli.py` / `agent/prompt_builder.py`（核心环路）
- 改动会影响 `system_prompt` 装配或 toolset 列表
- 改动涉及 `HERMES_HOME` 路径、profile、SQLite session DB
- 重构 / 优化 / 任何会动到 ≥2 个模块的改动

> **铁律：可在脑子里 30 秒说清的微小改动除外**（如改一行注释、改默认值），否则一律先 brainstorm。

---

## 执行流程

### Step 1: 加载工程上下文

> hermes-agent 不是 Django，没有 `manage.py`、没有 `BaseResponse`、没有 App 包概念。
> 它是**模块化的 Python 工程**，主要由 CLI / 核心 Agent / 工具系统 / Gateway / Plugin / TUI 构成。

```bash
# 工程心智地图（必读）
cat AGENTS.md                                   # 项目结构、硬规则、已知坑

# 常见入口
ls run_agent.py cli.py model_tools.py toolsets.py hermes_state.py
ls hermes_cli/                                  # CLI 子命令、setup 向导、skin 引擎
ls tools/                                       # 工具实现（自动发现）
ls gateway/ gateway/platforms/                  # Gateway + 平台适配器
ls plugins/ plugins/memory/                     # 通用插件 + 内存插件
ls agent/                                       # provider adapter、prompt_builder、memory 等
ls ui-tui/src/                                  # Ink TUI 前端

# 当前 git 状态 + 最近改动
git status
git log --oneline -10
```

### Step 2: 一次一问澄清需求

**强制：每次只问一个问题，优先选择题**。下面是 hermes 调试 / 改动场景的标准提问树：

#### Q1：改动属于哪个核心模块？

```
A) 工具（tools/*.py + toolsets.py）— 新增/改 tool handler、schema、check_fn
B) 斜杠命令（hermes_cli/commands.py + cli.py + gateway/run.py）— /xxx 命令
C) Agent 核心环路（run_agent.py）— iteration、interrupt、budget、ToolCall 处理
D) Prompt 装配（agent/prompt_builder.py）— system prompt 各 slot
E) Provider 适配器（agent/*provider*）— 新模型/API
F) 配置（hermes_cli/config.py）— DEFAULT_CONFIG / OPTIONAL_ENV_VARS
G) Gateway 平台（gateway/platforms/）— telegram / discord / slack 等
H) Plugin（plugins/<name>/）— 通用插件 / 内存插件 / 上下文引擎
I) TUI（ui-tui/src/ + tui_gateway/）— Ink 前端 / JSON-RPC 后端
J) Skills（skills/ 或 optional-skills/<category>/<skill>/）
K) 跨多个模块（请简述）
```

#### Q2：改动类型？

```
A) 新增功能
B) Bug 修复（先复现 → 再修，不要瞎猜）
C) 重构/优化（行为不变）
D) 仅调研/探索（暂不落代码）
```

#### Q3：会不会动到 system prompt？

```
A) 会 → 必须确认不破坏 prompt cache（详见架构红线 §1）
B) 不会
C) 不确定 → 先停下，先看 run_agent.py 的 _build_system_prompt 再回来
```

#### Q4：会不会读写 HERMES_HOME 路径？

```
A) 会 → 必须用 get_hermes_home() / display_hermes_home()，禁止硬编码 ~/.hermes
B) 不会（仅 cwd 内文件 / 内存对象）
```

#### Q5：测试覆盖范围？

```
A) 新增 tests/ 下用例（必须用 scripts/run_tests.sh 跑）
B) 已有用例足够覆盖，仅跑相关目录
C) 仅人工 / curl 验证（说明理由）
D) 仅探索，本轮不测
```

> 还可以根据需要追问：是否影响 Gateway 多平台兼容、是否需要新加依赖（须先征求用户同意）、是否影响 profile 隔离、是否影响 cache 命中率。

### Step 3: 探索多种方案（至少 2-3 个）

| 方案 | 思路 | 优点 | 缺点 | 复杂度 | 是否符合 hermes 红线 |
|---|---|---|---|---|---|
| A | …… | …… | …… | 低/中/高 | ✅ / ⚠️（破缓存）|
| B | …… | …… | …… | 低/中/高 | ✅ |
| C | …… | …… | …… | 低/中/高 | ✅ |

**推荐方案 X，因为……**

### Step 4: 架构红线合规检查

> 这一步**强制**。不通过就回 Step 3 改方案，不要硬上。

```markdown
## 架构红线检查（hermes-agent 专用）

### 通用红线（任何改动都要看）
- [ ] 路径：使用 `get_hermes_home()` / `display_hermes_home()`，**禁止** `Path.home() / ".hermes"`
- [ ] 日志：用 `logging.getLogger(__name__)`，profile 感知由 `hermes_logging.py` 统一处理
- [ ] 配置：非密钥用 `config.yaml`（`hermes_cli/config.py::DEFAULT_CONFIG`），仅密钥用 `.env`（`OPTIONAL_ENV_VARS`）
- [ ] 测试：跑 `scripts/run_tests.sh`，不要直接 `pytest`（CI parity）
- [ ] 测试：不写 change-detector（不断言 `_config_version == N`、模型名快照、列表长度）
- [ ] 依赖：新增第三方依赖必须**先询问用户**，安装后同步 `requirements.txt`

### Prompt cache 红线（动 system prompt / toolset 必看）
- [ ] 不在会话中途改变过去的 message
- [ ] 不在会话中途切换 toolset
- [ ] 不在会话中途重新加载 memory / 重建 system prompt
- [ ] 唯一允许动 context 的时机：上下文压缩（context compression）
- [ ] 若是改 slash 命令：默认走 deferred invalidation（下个会话生效），可选 `--now` 立即生效

### 工具开发红线（动 tools/ 必看）
- [ ] handler 返回 **JSON 字符串**（不是 dict）
- [ ] schema description **不**硬编码引用其它 toolset 的工具名（用 `model_tools.py::get_tool_definitions()` 动态拼接）
- [ ] 状态文件路径用 `get_hermes_home()`
- [ ] schema 描述里的路径用 `display_hermes_home()`（profile 友好）
- [ ] 自动发现：`tools/*.py` 在文件顶层 `registry.register()` 即可，不要改 import 列表

### 斜杠命令红线（动 commands 必看）
- [ ] 在 `hermes_cli/commands.py::COMMAND_REGISTRY` 加 `CommandDef`（**单一来源**）
- [ ] CLI 处理器加在 `cli.py::HermesCLI.process_command()`
- [ ] Gateway 处理器加在 `gateway/run.py`（如果该命令在 Gateway 也可用）
- [ ] 持久化设置用 `save_config_value()`
- [ ] 别名通过 `aliases=` 元组添加，**不**改其它任何文件
- [ ] 命令需要在 agent 运行中也可达？同时绕过 base.py 的 `_pending_messages` + `gateway/run.py` 的 active-session 拦截两道关

### Gateway 平台红线（动 gateway/platforms 必看）
- [ ] 用唯一 token 连接的平台，`connect()` 调 `acquire_scoped_lock()`、`disconnect()` 调 `release_scoped_lock()`（避免两个 profile 抢同一 token）
- [ ] `terminal.cwd` 来自 `config.yaml`，不要再用废弃的 `MESSAGING_CWD` env
- [ ] 后台进程通知：尊重 `display.background_process_notifications` 配置

### Plugin 红线（动 plugins/ 必看）
- [ ] **绝对禁止**改核心文件（`run_agent.py` / `cli.py` / `gateway/run.py` / `hermes_cli/main.py`）
- [ ] 缺能力？扩展通用 plugin 接口，不要把插件特定逻辑塞进核心
- [ ] CLI 子命令通过 `register_cli_command(...)` 注册，argparse 在启动时自动接入
```

### Step 5: 分段确认设计

每段 200-300 字，每段后问一句"以上是否符合预期？"，逐段确认：

```markdown
## 1. 现象与根因（仅 Bug 修复）
[一句话现象 + 触发链路 + 假设根因 + 一条复现命令]
---
**根因假设是否成立？**

## 2. 改动范围
[列模块 + 文件清单 + 每个文件改什么]
---
**改动范围是否合理？是否漏了什么？**

## 3. 数据流 / Prompt 流（如涉及）
[请求/事件如何流过模块；如动 prompt，画出 slot 顺序]
---
**数据流是否清晰？**

## 4. 失败处理 + 回滚
[出错路径、回滚策略、feature flag]
---
**失败处理是否完整？**
```

### Step 6: 落盘头脑风暴文档

> **铁律：**
> 1. 文档必须保存到 `.cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_brainstorm.md`
> 2. **`.cursor/` 不会被 hermes 当 prompt 注入**（已删 `.cursor/rules/`，AGENTS.md 优先匹配），可放心落盘
> 3. 三件套（brainstorm / plan / execute）共用同一文件夹 + 同一日期前缀

```bash
mkdir -p .cursor/docs/YYYY-MM-DD-<topic>
# 文件: .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_brainstorm.md
```

### 文档结构（必备一级标题）

```markdown
# [改动主题] 头脑风暴

## 背景与目标
## 现象与根因（仅 Bug 修复）
## 需求澄清记录（Q1-Q5 答案）
## 方案对比
## 推荐方案详细设计
## 架构红线合规检查（勾选清单）
## 涉及文件清单（精确路径）
## 测试策略（哪些 tests/ 目录、运行命令）
## 后续计划（→ /sp_plan 下一步要拆什么）
```

### Step 7: 提示下一步

```
✅ 阶段 1 设计已完成。

📋 产出物:
   - 头脑风暴文档: .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_brainstorm.md

👉 下一步是 **阶段 2 拆分实现计划**（/sp_plan），具体内容:
   - 把推荐方案拆成 2-5 分钟的小任务
   - 每个任务有精确文件路径 + 验证命令（grep / scripts/run_tests.sh / hermes 实跑）
   - 计划文档: .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_plan.md

📌 完整文档链:
   ✅ _brainstorm.md（已完成）
   ⬜ _plan.md（下一步）
   ⬜ _execute.md（待执行）

是否继续进入阶段 2？或者您需要先调整 / 补充什么？
```

---

## 关键原则

| 原则 | 说明 |
|---|---|
| **一次一问** | 不要把 Q1-Q5 一次性甩给用户 |
| **选择题优先** | 比开放问题更容易回答 |
| **YAGNI** | 不上车未明确要求的功能 |
| **至少 2-3 方案** | 第一个想到的方案不一定最合适 |
| **增量确认** | 分段展示，逐段拍板 |
| **架构红线** | 一票否决，不通过就改方案 |
| **不许瞎猜根因** | Bug 修复必须先复现，再假设 |
| **缓存意识** | 任何动 prompt / toolset 的方案，都要回答"会不会破 cache" |

---

## 输出模板

```
╔════════════════════════════════════════════════════════════╗
║                hermes 改动头脑风暴报告                      ║
╠════════════════════════════════════════════════════════════╣
║ 改动主题: [简述]                                            ║
║ 模块归属: [tools / cli / gateway / agent / plugin / TUI]   ║
║ 改动类型: [新增 / 修复 / 重构 / 探索]                       ║
║ 推荐方案: [方案 X]                                          ║
║ 复杂度:   [低 / 中 / 高]                                    ║
╠════════════════════════════════════════════════════════════╣
║ 红线检查:                                                   ║
║   ✅ HERMES_HOME 路径合规                                   ║
║   ✅ 不破 prompt cache                                      ║
║   ✅ 测试用 scripts/run_tests.sh                            ║
║   ✅ 不写 change-detector                                   ║
║   ✅ 无插件碰核心文件                                       ║
╠════════════════════════════════════════════════════════════╣
║ 涉及文件:                                                   ║
║   ~ run_agent.py / cli.py / tools/xxx.py …                  ║
║   + tests/<area>/test_xxx.py                                ║
╠════════════════════════════════════════════════════════════╣
║ 文档目录: .cursor/docs/YYYY-MM-DD-<topic>/                  ║
║   brainstorm: YYYY-MM-DD-<topic>_brainstorm.md  ✅          ║
║   plan:       YYYY-MM-DD-<topic>_plan.md        ⬜          ║
║   execute:    YYYY-MM-DD-<topic>_execute.md     ⬜          ║
║ 下一步: /sp_plan 拆分实现计划                               ║
╚════════════════════════════════════════════════════════════╝
```

---

## 相关命令

- `/sp_plan` — 把方案拆成可执行任务（阶段 2）
- `/sp_execute` — 分批执行 + 检查点（阶段 3-7）

## 参考文档

- `AGENTS.md` — hermes-agent 工程结构、AIAgent 类、工具系统、CLI、TUI、Plugin、Skill、配置、profile、已知坑、测试规范（**必读**）
- `WORK_IN_PROGRESS.md` — 当前正在进行的工作（如果有）
