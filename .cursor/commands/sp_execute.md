# /sp_execute — hermes-agent 计划执行 + 检查点

> **基于 superpowers 工作流方法论 · 适配 hermes-agent 工程**
>
> 加载 `_plan.md`，分批执行任务，每批之间审查 + 反馈，全流程更新 `_execute.md`，支持断点续做。

## 使用方式

```
/sp_execute <计划文档路径>
/sp_execute .cursor/docs/2026-05-06-tg-token-lock/2026-05-06-tg-token-lock_plan.md
```

## 核心原则

```
批量执行 + 检查点 = 高质量、可控的实现
按依赖顺序：叶子在前 → 入口在后
每个任务可独立验证（grep / scripts/run_tests.sh / hermes 实跑）
铁律：没有命令输出作证 = 任务未完成
```

---

## 执行流程

### Step 1: 加载并审查计划

```bash
# 读 plan + 关联的 brainstorm
cat .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_plan.md
cat .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_brainstorm.md   # 如有

# 工程上下文（必读）
cat AGENTS.md
```

**审查清单：**

- [ ] 计划文件命名是否符合 `_plan.md` 规范（同名文件夹 + 同名前缀）？
- [ ] 任务是否都有精确文件路径？
- [ ] 任务顺序是否符合"叶子在前 → 入口在后"？
- [ ] 每个任务是否有验证命令 + 预期输出？
- [ ] 涉及 system prompt / toolset 的任务是否有 cache 影响声明？
- [ ] 涉及 HERMES_HOME 路径的任务是否用了 `get_hermes_home()` / `display_hermes_home()`？
- [ ] 测试任务是否用 `scripts/run_tests.sh`？
- [ ] 是否有 change-detector 测试（断言固定模型名 / version 数字 / 列表长度）？

**有疑问** → 在动手前提出，不要硬上。
**无疑问** → 创建 `_execute.md` 并继续。

### Step 2: 创建执行进度文档

> **铁律：进度文档必须在 `.cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_execute.md`，与同名 plan/brainstorm 同目录。**

```markdown
# [改动主题] 执行进度

> **关联文档（同目录下）:**
> - 头脑风暴: `YYYY-MM-DD-<topic>_brainstorm.md`（如有）
> - 实现计划: `YYYY-MM-DD-<topic>_plan.md`
>
> **文档目录:** `.cursor/docs/YYYY-MM-DD-<topic>/`
>
> **开始时间:** YYYY-MM-DD HH:MM
> **当前状态:** 🔄 执行中

---

## 任务总览

| 序号 | 任务名称 | 状态 | 开始时间 | 完成时间 | 备注 |
|------|---------|------|---------|---------|------|
| 1 | [任务名称] | ⬜ 待执行 | | | |
| 2 | [任务名称] | ⬜ 待执行 | | | |
| 3 | [任务名称] | ⬜ 待执行 | | | |

---

## 批次执行记录

（每个批次完成后追加记录）
```

**同时用 TodoWrite 创建内存任务列表**（与 `_execute.md` 总览一一对应）。

### Step 3: 分批执行

**默认批次大小: 3 个任务**（依赖紧密的可以一批，独立的可以拆开）。

每个任务的执行流程：

1. **标记进行中** — `_execute.md` 总览 + TodoWrite 同步改 `🔄`
2. **按 plan 步骤执行** — 严格按 `_plan.md` 的代码片段 + 命令，**不跳步**
3. **运行验证命令** — 每个 Step 的 `预期:` 都跑一遍，对照输出
4. **跑相关测试**（必须用 `scripts/run_tests.sh`）：

```bash
# 本任务相关单测
scripts/run_tests.sh tests/<area>/test_<file>.py -v

# 一个目录的全测（适合改动影响面较大时）
scripts/run_tests.sh tests/<area>/

# 全量（仅在批次收尾或 release 前）
scripts/run_tests.sh
```

5. **架构红线 grep 检查**（按改动场景挑用）：

```bash
# 路径硬规则：禁止硬编码 ~/.hermes / Path.home() / ".hermes"
rg "Path\.home\(\)\s*/\s*[\"']\.hermes[\"']" <改动文件>
rg '"\~/\.hermes' <改动文件>
# 期望: 无匹配（应改用 get_hermes_home() / display_hermes_home()）

# 工具 handler 必须返回 JSON 字符串
rg "return\s+\{" tools/<name>.py     # 视情况，确认包了 json.dumps
rg "json\.dumps" tools/<name>.py     # 应有

# 斜杠命令必须先在 COMMAND_REGISTRY 注册
rg "CommandDef\(\s*[\"']<name>[\"']" hermes_cli/commands.py
# 期望: 命中

# 插件不许碰核心文件
git diff --name-only HEAD | rg '^(run_agent|cli|gateway/run|hermes_cli/main)\.py'
# 当本次改动是 plugin 时：期望无匹配
```

6. **标记完成** — `_execute.md` 总览 + TodoWrite 同步改 `✅`

### Step 4: 批次完成报告 + 双向落盘

每批完成后**必须同时更新两个文件 + 一份对话报告**。

#### 4.1 更新 `_execute.md`（总览 + 批次记录）

```markdown
## 任务总览（更新状态）

| 序号 | 任务名称 | 状态 | 开始时间 | 完成时间 | 备注 |
|------|---------|------|---------|---------|------|
| 1 | 实现 tools/<name>.py | ✅ 已完成 | 10:00 | 10:08 | |
| 2 | 接入 toolsets.py | ✅ 已完成 | 10:08 | 10:10 | |
| 3 | 写工具单测 | ✅ 已完成 | 10:10 | 10:20 | |
| 4 | 文档更新 | ⬜ 待执行 | | | |

---

## 批次 1 执行记录

**执行时间:** YYYY-MM-DD HH:MM ~ HH:MM
**完成任务:** 1-3

### 已实现
- 任务 1: [简述实现内容]
- 任务 2: [简述实现内容]
- 任务 3: [简述实现内容]

### 文件变更
- `tools/<name>.py` (新增)
- `toolsets.py` (修改 — 加入 _HERMES_CORE_TOOLS)
- `tests/tools/test_<name>.py` (新增)

### 验证结果
- `scripts/run_tests.sh tests/tools/test_<name>.py`: ✅ N passed
- handler JSON 字符串校验: ✅
- schema 自动发现: ✅
- 无硬编码 ~/.hermes: ✅

### 红线检查
- [x] 路径用 get_hermes_home() / display_hermes_home()
- [x] handler 返回 JSON 字符串
- [x] 不影响 system prompt / toolset 中途切换
- [x] 测试不写 change-detector
- [x] 测试通过 scripts/run_tests.sh
```

#### 4.2 更新 `_plan.md`（任务状态标记）

```markdown
### 任务 1: 实现 tools/<name>.py `[✅ 已完成]`
### 任务 2: 接入 toolsets.py    `[✅ 已完成]`
### 任务 3: 写工具单测           `[✅ 已完成]`
### 任务 4: 文档更新             `[⬜ 待执行]`
```

#### 4.3 对话中输出批次报告

```markdown
## 批次 1 完成报告

### 已实现
- 任务 1 / 2 / 3: [简述]

### 验证结果

```bash
scripts/run_tests.sh tests/tools/test_<name>.py -v
# 结果: N passed in X.XXs

python -c "from tools import registry; import tools.<name>; print('<name>' in registry.registry._tools)"
# 结果: True
```

### 文件变更
- `tools/<name>.py` (新增)
- `toolsets.py` (修改)
- `tests/tools/test_<name>.py` (新增)

### 红线 ✅
- [x] HERMES_HOME 路径合规
- [x] handler 返回 JSON 字符串
- [x] 不破 prompt cache
- [x] scripts/run_tests.sh 通过
- [x] 无 change-detector 测试
- [x] 无插件碰核心文件

### 文档链进度（.cursor/docs/YYYY-MM-DD-<topic>/）
- ✅ _brainstorm.md（已完成）
- 🔄 _plan.md（任务 1-3 已标记完成）
- 🔄 _execute.md（批次 1 已记录）

---

**Ready for feedback.** （等待反馈）
```

### Step 5: 处理反馈（断点续做）

根据用户反馈：

- **需要调整** → 改完更新 `_execute.md`，继续下一批次
- **继续执行** → 下一批次
- **暂停** → `_execute.md` 顶部状态改 `⏸️ 暂停`，保存进度，结束本次

#### 断点续做机制

下次重新进入 `/sp_execute` 时：

```bash
# 1. 读进度
cat .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_execute.md

# 2. 读计划任务状态
cat .cursor/docs/YYYY-MM-DD-<topic>/YYYY-MM-DD-<topic>_plan.md

# 3. 跳过 [✅ 已完成]，从第一个 [⬜ 待执行] 开始新批次，追加批次记录
```

### Step 6: 全部代码任务完成 → 提示进入测试阶段

```
✅ 阶段 3 实现已完成。

📋 产出物:
   - 完成任务: [N] 个
   - 新增文件: [N] 个 / 修改文件: [M] 个
   - 文档目录: .cursor/docs/YYYY-MM-DD-<topic>/
   - 执行进度文档: YYYY-MM-DD-<topic>_execute.md（已更新）

📌 完整文档链:
   ✅ _brainstorm.md（已完成）
   ✅ _plan.md（所有代码任务已标记完成）
   ✅ _execute.md（所有批次已记录）

👉 下一步是 **阶段 4 测试**：
   - 单测 / 集成测试覆盖：scripts/run_tests.sh tests/<area>/
   - 涉及多个区域：scripts/run_tests.sh（全量）
   - 验证测试**全部通过**

📌 后续：阶段 5 文档 → 阶段 6 审查 → 阶段 7 实跑验证

是否继续？
```

### Step 7: 测试阶段

```bash
# 7.1 相关目录全过
scripts/run_tests.sh tests/<area>/

# 7.2 涉及核心环路 / Gateway / 大改动 → 全量
scripts/run_tests.sh

# 7.3（可选）覆盖率
# scripts/run_tests.sh 内部已用 -n 4 + 隔离环境，
# 如需 coverage 单独跑：
# coverage run -m pytest tests/<area>/ -n 4
# coverage report -m --include="<area>/*"
```

> **铁律：必须用 `scripts/run_tests.sh`**。直接 `pytest` 会让本地环境（API key、TZ、xdist 并发数、HOME）和 CI 不一致，制造"本地过 CI 挂"或反向的鬼。

测试通过后：

```
✅ 阶段 4 测试已完成。

📋 产出物: N 用例全部通过 / 覆盖率（可选）

👉 下一步是 **阶段 5 文档**：
   - 是否对外行为变更？→ 更新 README / AGENTS.md / website/docs/
   - 新增/改 slash 命令？→ 已自动通过 COMMAND_REGISTRY 反映到 /help
   - 新增配置项？→ 自动反映到 setup 向导（OPTIONAL_ENV_VARS）/ 用户 config
   - 新增工具？→ 默认 tool list 自动包含；如有用法说明，写到对应模块文档

📌 后续：阶段 6 审查 → 阶段 7 实跑验证

是否继续？
```

### Step 8: 文档更新阶段

按改动类型挑：

| 改动类型 | 必须更新的文档 |
|---|---|
| 对外行为变更（CLI 命令、参数、Gateway 命令） | `README.md` 对应章节 + `website/docs/` 相关页（如 `cli-commands.md`、`features/`）|
| 工程结构 / 新模块 / 新硬规则 | `AGENTS.md`（注意是项目根的工程指南） |
| 新增依赖 | `requirements.txt` + 更新 `pyproject.toml`（若有）|
| 新增 / 重命名 toolset / tool | `website/docs/` 工具相关页面 |
| 改 setup 向导问询项 | 顺手测一遍 `hermes setup` |

> **不必每次都改 4 份文档**——hermes 的文档分布与 Django 工程不同。改了什么，更什么；不写空话。

### Step 9: 审查阶段

```bash
# 9.1 类型/语法（如配置了 mypy/ruff，按工程实际跑）
# 9.2 架构合规 grep
rg "Path\.home\(\)\s*/\s*[\"']\.hermes[\"']" <本次改动文件>
rg "from\s+loguru" <本次改动文件>   # 期望: 无（hermes 用标准 logging）
git diff --name-only HEAD | rg '^(run_agent|cli|gateway/run|hermes_cli/main)\.py'
# 当本次是 plugin 改动时: 期望无匹配

# 9.3 安全
rg -n "(?i)(api[_-]?key|token|secret|password)\s*=\s*[\"'][^\"']+[\"']" <本次改动文件>
# 期望: 无硬编码（应走 os.environ.get / config.yaml）

# 9.4 traceback 不返回前端 / 不打到日志（按实际改动检查）
```

### Step 10: 最终实跑验证 + 完成报告

```bash
# 10.1 启动 hermes，肉眼/手动验证
hermes chat
# 或 hermes --tui
# 或 hermes gateway run

# 10.2 跑相关命令 / 工具 / 平台行为，对照 brainstorm 的"成功标准"

# 10.3 全量测试收尾
scripts/run_tests.sh
```

完成报告（更新 `_execute.md` 顶部状态为 `✅ 全部完成`）：

```
╔════════════════════════════════════════════════════════════╗
║              hermes 改动开发生命周期完成报告               ║
╠════════════════════════════════════════════════════════════╣
║ 改动主题:  [简述]                                          ║
║ 模块归属:  [tools / cli / gateway / agent / plugin / TUI]  ║
║ 入口命令:  /sp_execute                                     ║
╠════════════════════════════════════════════════════════════╣
║ 生命周期检查:                                              ║
║   ✅ 阶段 1 设计: _brainstorm.md                           ║
║   ✅ 阶段 2 计划: [N] 任务                                 ║
║   ✅ 阶段 3 实现: [新增 X / 修改 Y]                        ║
║   ✅ 阶段 4 测试: scripts/run_tests.sh 全过                ║
║   ✅ 阶段 5 文档: README / AGENTS.md / website 等          ║
║   ✅ 阶段 6 审查: 红线 grep + 安全检查                     ║
║   ✅ 阶段 7 验证: hermes 实跑 + 全量测试                   ║
╠════════════════════════════════════════════════════════════╣
║ 文档目录: .cursor/docs/YYYY-MM-DD-<topic>/                 ║
║   ✅ _brainstorm.md  ✅ _plan.md  ✅ _execute.md           ║
╚════════════════════════════════════════════════════════════╝
```

---

## 何时停下求助

**立即停止当：**

- 复现失败（说不清 Bug 是怎么触发的，就别"试着修一下")
- 计划里有关键缺口（路径不明确、验证缺失）
- 测试反复失败超过 2 轮还没找到根因
- 改动可能破 prompt cache 但不确定如何规避
- 改动需要碰核心文件而该任务是 plugin
- 不理解某个 hermes 内部约定（profile / lock / dispatch 双层守卫等）

**宁可澄清，不要猜测。**

---

## 验证规则

> **铁律：没有命令输出作证 = 任务未完成。**

| 声明 | 需要的证据 | 不充分的证据 |
|---|---|---|
| 测试通过 | `scripts/run_tests.sh` 输出: N passed | "应该通过"、上次输出 |
| 工具注册成功 | `python -c "...registry.registry._tools..."` 命中 | "我加了 register" |
| 命令可用 | `hermes` 实跑 / autocomplete 命中 | "我加了 CommandDef" |
| Bug 已修复 | 复现用例 RED → GREEN | 改了代码、假设修了 |
| 不破缓存 | 文档说明 + 阅读 prompt 装配代码 | "我没动 prompt" |
| 路径合规 | grep 无 `Path.home()/".hermes"` 命中 | 看了一眼觉得没问题 |

### 常见的虚假完成信号

```
❌ "代码已写好"          → 没跑验证
❌ "上次测试通过了"       → 这次又改了
❌ "逻辑上应该没问题"     → 逻辑正确 ≠ 代码正确
❌ "和 telegram 那边一样的模式"  → 复制粘贴也会出错
❌ "改了一行肯定不会破缓存"     → 必须读 prompt builder 确认
```

---

## 执行检查清单

每个任务完成前：

- [ ] 严格按 plan 步骤执行
- [ ] 跑了所有验证命令（含预期对照）
- [ ] `scripts/run_tests.sh tests/<area>/` 通过（如本任务有测试）
- [ ] 路径用 `get_hermes_home()` / `display_hermes_home()`
- [ ] 不破 prompt cache（如涉及）
- [ ] handler 返回 JSON 字符串（如是工具）
- [ ] CommandDef 已注册（如是斜杠命令）
- [ ] `_execute.md` 总览 + `_plan.md` 状态标记 同步更新
- [ ] TodoWrite 状态同步

每个批次完成前：

- [ ] 所有任务已完成 + 验证
- [ ] 批次报告已写入 `_execute.md`
- [ ] 红线 grep 全过
- [ ] 等待用户反馈

---

## 输出模板

### 任务进度

```
╔════════════════════════════════════════════════════════════╗
║                hermes 计划执行进度                          ║
╠════════════════════════════════════════════════════════════╣
║ 文档目录: .cursor/docs/YYYY-MM-DD-<topic>/                  ║
║ 计划文件: YYYY-MM-DD-<topic>_plan.md                        ║
║ 进度文件: YYYY-MM-DD-<topic>_execute.md                     ║
║ 总任务数: [N]   已完成: [M]   当前批次: [B]                 ║
╠════════════════════════════════════════════════════════════╣
║ 任务状态:                                                   ║
║   [✓] 任务 1: 实现 tools/<name>.py                          ║
║   [✓] 任务 2: 接入 toolsets.py                              ║
║   [→] 任务 3: 写工具单测 (进行中)                            ║
║   [ ] 任务 4: 文档更新                                       ║
╚════════════════════════════════════════════════════════════╝
```

### 批次完成

```
╔════════════════════════════════════════════════════════════╗
║                   批次 N 完成报告                           ║
╠════════════════════════════════════════════════════════════╣
║ 完成任务: 3/N                                               ║
║ 测试结果: ✅ scripts/run_tests.sh tests/tools/ N passed     ║
║ 红线检查: ✅ 全过                                           ║
║ 实跑验证: ✅（如适用）                                      ║
╠════════════════════════════════════════════════════════════╣
║ 变更文件:                                                   ║
║   + tools/<name>.py                                         ║
║   ~ toolsets.py                                             ║
║   + tests/tools/test_<name>.py                              ║
╠════════════════════════════════════════════════════════════╣
║ 状态: ⏸️  等待反馈                                          ║
╚════════════════════════════════════════════════════════════╝
```

---

## 相关命令

- `/sp_brainstorm` — 上两步：方案设计
- `/sp_plan` — 上一步：任务拆分

## 参考文档

- `AGENTS.md` — hermes-agent 工程结构、硬规则、已知坑、测试规范（**必读**）
- `WORK_IN_PROGRESS.md` — 当前正在进行的工作（如果有）
