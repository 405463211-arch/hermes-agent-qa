# /sp_plan — hermes-agent 实现 / 调试计划拆分

> **基于 superpowers 工作流方法论 · 适配 hermes-agent 工程**
>
> 把头脑风暴产出的方案拆成 **2-5 分钟** 的小任务，每个任务都有精确文件路径、完整代码片段、验证命令。

## 使用方式

```
/sp_plan <头脑风暴文档路径 或 需求描述>
/sp_plan .cursor/docs/2026-05-06-tg-token-lock/2026-05-06-tg-token-lock_brainstorm.md
/sp_plan 给 cli 加 /skin <name> 持久化（已有 brainstorm）
```

## 核心原则

```
假设执行者对 hermes 代码库零上下文、判断力有限
→ 文档化所有需要知道的内容（精确路径、完整代码、验证命令）
→ 每个任务都是 2-5 分钟的小步骤
→ 任务顺序 = 模块依赖顺序（叶子在前、入口在后）
→ 每步可独立验证（grep / scripts/run_tests.sh / hermes 实跑）
```

---

## 执行流程

### Step 1: 加载上下文

```bash
# 1. 优先读对应的 brainstorm
ls .cursor/docs/
cat .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_brainstorm.md

# 2. 读 hermes 工程心智地图
cat AGENTS.md

# 3. 看相关模块当前结构（按 brainstorm 的"涉及文件清单"逐一打开）
ls run_agent.py cli.py model_tools.py toolsets.py
ls hermes_cli/ tools/ gateway/ gateway/platforms/ plugins/ agent/ ui-tui/src/
```

### Step 2: 创建计划文档

> **铁律：计划文档必须保存到 `.cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_plan.md`**，
> 与同名 `_brainstorm.md` 在同一文件夹。

```bash
# 没有对应 brainstorm？用今天日期 + 主题名新建一个文件夹
mkdir -p .cursor/docs/YYYY-MM-DD-<topic>
```

#### 文档头部（必填）

```markdown
# [改动主题] 实现计划

> **关联文档（同目录下）:**
> - 头脑风暴: `YYYY-MM-DD-<topic>_brainstorm.md`（如有）
> - 执行进度: `YYYY-MM-DD-<topic>_execute.md`（/sp_execute 生成）
>
> **文档目录:** `.cursor/docs/YYYY-MM-DD-<topic>/`
>
> **执行方式:** 使用 `/sp_execute .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_plan.md` 逐任务执行

**目标:** [一句话描述这个计划要做什么]

**模块归属:** [tools / cli / gateway / agent / plugin / TUI / 跨多模块]

**方案概述:** [2-3 句话描述实现思路 + 关键设计决策]

**技术栈:** Python 3.10+, hermes-agent 多模块工程, scripts/run_tests.sh

---
```

### Step 3: 拆分任务（按模块依赖顺序）

> hermes 不是 Django，**不要**套"错误码 → Utils → View → URL → 测试"。
> 改用按"叶子在前、入口在后"的依赖顺序：

#### 通用顺序（按模块归属选片段）

| 顺序 | 内容 | 对应位置 |
|---|---|---|
| 1 | 配置 schema 新增（如需） | `hermes_cli/config.py::DEFAULT_CONFIG` 或 `OPTIONAL_ENV_VARS` |
| 2 | 数据结构 / dataclass / 状态 | `agent/*` 或对应模块的 `_state.py` |
| 3 | 核心实现（叶子函数） | `tools/<x>.py` / `agent/<x>.py` / `gateway/platforms/<x>.py` 等 |
| 4 | 接入点 / 注册 | `toolsets.py` / `COMMAND_REGISTRY` / `gateway/run.py` 路由 / 自动发现 |
| 5 | 入口接线（CLI / Gateway / TUI） | `cli.py::process_command` / `gateway/run.py` / `ui-tui/src/*` |
| 6 | 单元测试 | `tests/<area>/test_<file>.py` |
| 7 | 集成 / E2E 验证 | `scripts/run_tests.sh tests/<area>` + `hermes` 实跑命令 |
| 8 | 文档更新（README / AGENTS.md / 网站文档） | 仅当对外行为变更 |

> **每个任务必须给出 4 段：文件路径 → Step 编辑 → Step 验证（含预期输出）→ 完成判定。**

### Step 4: 任务模板（按场景挑用）

下面给 6 个 hermes 高频场景的「任务起手式」，照抄改填即可。

---

#### 场景 A：新增工具（tools/）

```markdown
### 任务 A1: 实现 tools/<name>.py `[⬜ 待执行]`

**文件:**
- 创建: `tools/<name>.py`

**Step 1: 编写工具**

```python
import json
import os
from tools.registry import registry
from hermes_constants import get_hermes_home, display_hermes_home


def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))


def example_tool(param: str, task_id: str = None) -> str:
    """单行说明：做什么 + 何时用"""
    state_dir = get_hermes_home() / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    return json.dumps({"success": True, "data": "..."})


registry.register(
    name="example_tool",
    toolset="example",
    schema={
        "name": "example_tool",
        "description": (
            "做什么 + 何时用。"
            f" Default output dir: {display_hermes_home()}/example/"
        ),
        "parameters": {
            "type": "object",
            "properties": {"param": {"type": "string", "description": "..."}},
            "required": ["param"],
        },
    },
    handler=lambda args, **kw: example_tool(
        param=args.get("param", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**Step 2: 验证 schema 注册成功**

```bash
python -c "from tools import registry; import tools.<name>; print('example_tool' in registry.registry._tools)"
```

**预期:** `True`

**完成判定:** 自动发现命中、handler 返回 JSON 字符串、check_fn 用 env 守卫。

---

### 任务 A2: 接入 toolset `[⬜ 待执行]`

**文件:**
- 修改: `toolsets.py`

**Step 1:** 在 `_HERMES_CORE_TOOLS`（默认全平台）或新建一个 toolset 列表中加入 `"example_tool"`。

**Step 2:** `scripts/run_tests.sh tests/tools/ -k example`（如有用例）。

**完成判定:** 用例通过 + `hermes tools list` 能看到。
```

---

#### 场景 B：新增斜杠命令

```markdown
### 任务 B1: 注册 CommandDef `[⬜ 待执行]`

**文件:**
- 修改: `hermes_cli/commands.py`

**Step 1:** 在 `COMMAND_REGISTRY` 列表追加：

```python
CommandDef(
    "<name>",
    "<一句话描述>",
    "<Session | Configuration | Tools & Skills | Info | Exit>",
    aliases=("<short>",),
    args_hint="<arg1> [arg2]",
    # gateway_only=True / cli_only=True / gateway_config_gate="display.xxx"
)
```

**Step 2 验证:**

```bash
python -c "from hermes_cli.commands import resolve_command; print(resolve_command('<name>'))"
```

**预期:** 输出对应 `CommandDef`。

---

### 任务 B2: CLI 处理器 `[⬜ 待执行]`

**文件:**
- 修改: `cli.py::HermesCLI.process_command`

**Step 1:** 在 `canonical == "<name>":` 分支调用 `_handle_<name>(cmd_original)`，并实现该方法。

**Step 2 验证:**

```bash
hermes chat
# > /<name> ...
```

---

### 任务 B3: Gateway 处理器 `[⬜ 待执行]`（仅当 Gateway 也用）

**文件:**
- 修改: `gateway/run.py`

**Step 1:** 在 dispatch 分支加 `if canonical == "<name>":` 并实现 async 处理器。

**Step 2:** 若该命令需要在 agent 运行中也能被处理（如审批、停止），**必须同时**绕过：
1. `gateway/platforms/base.py` 的 `_pending_messages` / `_active_sessions` 拦截
2. `gateway/run.py` 入口的 active-session 拦截

**完成判定:** Telegram/Slack 发送命令能立刻响应，不被 agent 锁住。

---

### 任务 B4: 测试 `[⬜ 待执行]`

**文件:**
- 创建: `tests/cli/test_<name>_command.py`（CLI）
- 创建: `tests/gateway/test_<name>_dispatch.py`（Gateway，如适用）

**Step 1:** 用例覆盖：成功路径、错误参数、别名解析、autocomplete 列表包含。

**Step 2 运行:**

```bash
scripts/run_tests.sh tests/cli/test_<name>_command.py -v
```

**预期:** 全部 PASS。
```

---

#### 场景 C：新增配置项

```markdown
### 任务 C1: 增加 config.yaml 默认值 `[⬜ 待执行]`

**文件:**
- 修改: `hermes_cli/config.py::DEFAULT_CONFIG`

**Step 1:** 加键。**仅当**重命名/重构旧键才 bump `_config_version`，新增键 deep-merge 自动生效。

**Step 2 验证:**

```bash
scripts/run_tests.sh tests/hermes_cli/test_config.py -v
```

---

### 任务 C2: 增加 .env 密钥（仅密钥）`[⬜ 待执行]`

**文件:**
- 修改: `hermes_cli/config.py::OPTIONAL_ENV_VARS`

**Step 1:** 加 `"NEW_API_KEY": {description, prompt, url, password=True, category}`。

**Step 2:** 非密钥（超时、阈值、开关、路径）一律走 `config.yaml`，**不**进 `.env`。
```

---

#### 场景 D：动 system prompt / toolset

```markdown
### 任务 D1: 评估 cache 影响 `[⬜ 待执行]`

**Step 1:** 回答下列问题，写进计划文档：
- 改动会让 system prompt 在会话**中途**变化吗？
- 改动会让 toolset 列表在会话**中途**变化吗？
- 改动会触发**重新加载** memory / 重建 system prompt 吗？

**Step 2:** 任何一个"是" → 改方案，或确保走 deferred invalidation（下个会话生效）+ 提供 `--now` 立即失效开关。

---

### 任务 D2: 落地改动 `[⬜ 待执行]`

**文件:**
- 修改: `agent/prompt_builder.py` 或 `run_agent.py::_build_system_prompt` 或 `model_tools.py::get_tool_definitions`

**Step 1:** 实现，**保持冻结快照模式**，不要在循环里重建。

**Step 2:** 跑 prompt-builder 用例：

```bash
scripts/run_tests.sh tests/agent/test_prompt_builder.py -v
```
```

---

#### 场景 E：Gateway 新平台 / 改平台

```markdown
### 任务 E1: 平台适配器 `[⬜ 待执行]`

**文件:**
- 创建: `gateway/platforms/<name>.py`

**Step 1:** 继承基类、实现 `connect()` / `disconnect()` / 消息收发。
**Step 2:** 用唯一 token 连接的平台，在 `connect()` 调 `acquire_scoped_lock()`、`disconnect()` 调 `release_scoped_lock()`（参考 `telegram.py`）。

**Step 3 验证:**

```bash
scripts/run_tests.sh tests/gateway/ -v
```
```

---

#### 场景 F：Bug 修复（必须先复现，再修）

```markdown
### 任务 F0: 写复现用例（RED）`[⬜ 待执行]`

**文件:**
- 创建/修改: `tests/<area>/test_<bug>_repro.py`

**Step 1:** 写**会失败**的用例描述当前 Bug。
**Step 2 运行:**

```bash
scripts/run_tests.sh tests/<area>/test_<bug>_repro.py -v
```

**预期:** **失败**（红）—— 未失败说明没有真复现 Bug，停下来重新分析。

---

### 任务 F1: 最小修复（GREEN）`[⬜ 待执行]`

**文件:** [按 brainstorm 列出的精确路径]

**Step 1:** 改最小必要代码。
**Step 2:** 上面那个用例**变绿**。

**预期:** 测试 PASS。

---

### 任务 F2: 回归保障 `[⬜ 待执行]`

```bash
scripts/run_tests.sh tests/<area>/  # 该目录全过
scripts/run_tests.sh                # （可选）整套全过
```
```

### Step 5: 给每个任务标状态

```markdown
### 任务 1: ... `[⬜ 待执行]`
### 任务 2: ... `[⬜ 待执行]`
### 任务 3: ... `[⬜ 待执行]`
```

状态枚举：

- `[⬜ 待执行]` — 未开始
- `[🔄 执行中]` — 正在执行
- `[✅ 已完成]` — 已完成
- `[❌ 失败]` — 执行失败，需修复
- `[⏸️ 暂停]` — 暂停（断点续做）

### Step 6: 计划质量自检（强制）

| 检查项 | 说明 |
|---|---|
| **精确文件路径** | 每个任务明确 `创建/修改: 路径`，不写"修改 cli" |
| **完整代码片段** | 给完整代码而非"加个判断" |
| **依赖顺序** | 叶子在前 → 入口在后；测试**不**写在最末尾，每个核心任务后立刻验证 |
| **验证命令** | 每个任务带 `scripts/run_tests.sh ...` 或 `python -c ...` 或 `hermes ...`，并写**预期输出** |
| **缓存意识** | 动 prompt / toolset 的任务，必有 cache 影响声明 |
| **profile 安全** | 任何路径用 `get_hermes_home()` / `display_hermes_home()` |
| **测试不写 change-detector** | 不断言 `_config_version == N`、模型清单快照、列表长度 |
| **依赖审核** | 新增第三方依赖任务必有「先询问用户 + 安装后更新 requirements.txt」步骤 |

### Step 7: 保存并提示

```
✅ 阶段 2 拆分实现计划已完成。

📋 产出物:
   - 文档目录: .cursor/docs/YYYY-MM-DD-<topic>/
   - 计划文档: YYYY-MM-DD-<topic>_plan.md
   - 关联头脑风暴: YYYY-MM-DD-<topic>_brainstorm.md（如有）

📌 完整文档链:
   ✅ _brainstorm.md（已完成）
   ✅ _plan.md（已完成）
   ⬜ _execute.md（下一步 /sp_execute 生成）

**任务总数:** [N]    **预估时间:** [N × 2-5 分钟]

**执行方式:**
1. **逐步执行（当前会话）** → /sp_execute .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_plan.md
2. **暂存稍后执行** → 计划已保存，后续可从 _plan.md 任务状态继续

选择哪种方式？
```

---

## 输出模板

```
╔════════════════════════════════════════════════════════════╗
║                hermes 实现计划生成报告                      ║
╠════════════════════════════════════════════════════════════╣
║ 改动主题: [简述]                                            ║
║ 模块归属: [tools / cli / gateway / agent / plugin / TUI]   ║
║ 任务数量: [N] 个任务                                        ║
║ 预估时间: [N × 2-5 分钟]                                    ║
╠════════════════════════════════════════════════════════════╣
║ 任务列表（按依赖顺序）:                                     ║
║   1. [任务名称]               [⬜ 待执行]                   ║
║   2. [任务名称]               [⬜ 待执行]                   ║
║   3. [任务名称]               [⬜ 待执行]                   ║
║   ...                                                       ║
╠════════════════════════════════════════════════════════════╣
║ 文档目录: .cursor/docs/YYYY-MM-DD-<topic>/                  ║
║   plan:       YYYY-MM-DD-<topic>_plan.md       ✅           ║
║   brainstorm: YYYY-MM-DD-<topic>_brainstorm.md ✅           ║
║   execute:    YYYY-MM-DD-<topic>_execute.md    ⬜ 待生成    ║
║ 执行命令: /sp_execute .cursor/docs/YYYY-MM-DD-<topic>/...   ║
╚════════════════════════════════════════════════════════════╝
```

---

## 相关命令

- `/sp_brainstorm` — 上一步：方案设计
- `/sp_execute` — 下一步：执行 + 检查点

## 参考文档

- `AGENTS.md` — hermes-agent 工程结构、模块归属、硬规则、已知坑（**必读**）
- `WORK_IN_PROGRESS.md` — 当前正在进行的工作（如果有）
