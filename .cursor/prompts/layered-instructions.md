# Layered Instructions —— 长指令文件分层加载方法学

> 你（Cursor / Claude Code / 其他 AI 编程助手）在调试 hermes-agent 时如果撞到 `AGENTS.md` 只有 130 行，或者某个 `SKILL.md` 主文件很短但旁边有 `references/` 目录——那是有意的。本文是这套"分层加载"机制的**完整方法学**：为什么这么做、怎么做、怎么验证、踩过哪些坑。
>
> 用法：
> - **直接读这一份**就能理解项目里所有 `<File>.md` + `<File>/<refs>/*.md` 的拆分意图
> - **想优化别的长 markdown 文件**时，按本文的"完整工作流"小节执行
> - **改 AGENTS.md / SKILL.md 时**先读"反模式"小节，避免破坏现有结构

---

## 1. TL;DR（30 秒版）

**问题**：`AGENTS.md`、`SKILL.md` 这种 always-applied 或 frequently-loaded 的指令文件，写到 700+ 行后会出现：

1. **token 浪费**——Cursor 每条对话都注入 8k+ tokens，一个 100 turn 会话光指令开销 800k+
2. **U 型注意力稀释**——长 prompt 中段的硬约束（"用 qwen3.6-plus 做 grounding 别回退到旧 qwen3-vl-flash"、"用 `get_hermes_home()` 不要硬编码 `~/.hermes`"）容易被 LLM 忽略
3. **任务无关污染**——你只想加个工具，却被注入 plugins/skin/TUI 等无关章节

**方案**（Anthropic Skills 的 progressive disclosure 模式）：

```
原: One-Big-File.md (700+ 行, 8k tokens, 每次全注入)
新: One-Big-File.md (~130 行, 1.5k tokens, 主文件) 
    └── references/ or docs/<file>/  (6-13 个分主题 .md, 按需 Read)
```

主文件留**硬约束 + 速查 + 子文件入口索引**；详情、API、范例、历史全推到子文件，AI 看到入口索引再 Read。

**收益**（hermes-agent 实测）：
- `AGENTS.md`: 8,440 tok → 1,508 tok / 每条对话，节省 **6,930 tok × N 对话**
- `android-ui-automation/SKILL.md`: 5,549 tok → 1,582 tok / 触发后注入，节省 **3,966 tok × 触发次数**

**风险与对策**：LLM 可能跳过 Read 子文件直接读源码——所以**硬约束必须冗余编码**（主文件 + 源码注释 + 子文件 + 类型 + 运行时 assert + 测试）。详见第 4 节。

---

## 2. 何时用、何时不用

### 适合分层的文件

| 特征 | 阈值 | 例子 |
|------|------|------|
| **文件大小** | > 400 行 / > 4k tokens | `AGENTS.md` 751 行、`SKILL.md` 485 行 |
| **加载频率** | always-applied / 高频触发 | Cursor `AGENTS.md`、热门 skill |
| **任务无关比例** | > 60% 与单次任务无关 | AGENTS.md 里你只想加 tool 时，TUI/skin/plugin 章节都无关 |
| **信息粒度可分** | 章节相互独立、可单独读 | "Adding Tools" 和 "TUI Architecture" 互不依赖 |
| **存在硬约束分散在末尾** | "Known Pitfalls"、"Important Policies" 在第 500+ 行 | 旧 AGENTS.md 第 596 行才到"DO NOT hardcode `~/.hermes`" |

### 不适合分层的文件

| 特征 | 原因 |
|------|------|
| **文件 < 200 行 / < 2k tokens** | 不够大，拆完管理成本高于收益 |
| **整体是一个连贯流程** | 例如 `sp_brainstorm.md` 从头读到尾才有意义，拆了反而割裂 |
| **章节相互高度耦合** | 例如某个深度教程，前后章节互相引用同一个变量名/概念 |
| **加载频率低** | 用户偶尔才看的文档，拆不拆都行 |
| **是 README / 索引性质** | 本身就是入口，不是详情 |

### 决策树

```
文件 > 400 行?
├─ 否 → 不拆
└─ 是 ↓

是 always-applied (Cursor AGENTS.md / Claude Code system prompt)
或高频触发 (热门 skill)?
├─ 否 → 不拆
└─ 是 ↓

任务无关章节占比 > 60%?
├─ 否 → 考虑只压缩，不拆分
└─ 是 ↓

章节可独立阅读 (互不依赖)?
├─ 否 → 不拆，重写更紧凑
└─ 是 → 走分层加载（本文方案）
```

---

## 3. 通用拆分模板

### 3.1 主文件保留这 5 类

| 类别 | 例子（来自 AGENTS.md） | 为什么留主 |
|------|----------------------|-----------|
| **Hard Rules**（硬约束） | "Tests must NOT write to `~/.hermes/`" / "Plugins MUST NOT modify core files" | 违反就出 bug，必须每次都看到 |
| **Path Speed Sheet**（路径速查） | "Slash command registry → `hermes_cli/commands.py`" | 高频查询，避免每次找文件 |
| **Common Enums**（封闭枚举） | `CommandDef.category` 5 个合法值 | LLM 容易"自创新值"，必须钉死 |
| **Task → docs Index**（子文件入口） | "Adding a slash command → `docs/agents/cli-architecture.md`" | 没这个表 LLM 不知道有子文件可读 |
| **One-liners**（核心机制极简描述） | Agent loop 5 行伪代码 | 让 AI 不用读子文件就懂主路径 |

### 3.2 子文件移走这 5 类

| 类别 | 例子 | 为什么可以移 |
|------|------|-------------|
| **详细 API 签名 / 字段说明** | `ActionDecision` 字段含义 / `CommandDef` 全部字段 | 主文件给一行式调用就够，详情按需 |
| **完整使用范例** | "Adding a slash command 4 步" | 主文件给链接，子文件展开 |
| **背景 / 为什么这样设计** | "v2.2 → v3.0 为什么换 OCR 优先" | 不影响"怎么做" |
| **历史变更 / changelog** | Phase 1-4 学习进度 | 历史记录，新会话基本不需要 |
| **配置 / 环境准备 / 依赖** | adb 安装、`.env` 写法、PYTHONPATH | 一次配置，每次重读浪费 |

### 3.3 命名约定

```
案例 A (android-ui-automation):    ~/.hermes/skills/testing/android-ui-automation/SKILL.md
                                    ~/.hermes/skills/testing/android-ui-automation/references/<topic>.md

案例 C (AGENTS.md):                 <repo>/AGENTS.md
                                    <repo>/docs/agents/<topic>.md
```

**规则**：
- 主文件**就近**有一个 `references/` 或 `docs/<file>/` 子目录
- 每个子文件**单一主题**（不要混搭），文件名用 kebab-case 描述主题
- 子文件之间**不互相依赖**（每个都能独立读）
- 子文件第一行用 `# <主题>` 作为 H1，配合 1-2 句"什么时候读这个"

### 3.4 主文件链接子文件的 4 种正确姿势

| 姿势 | 例子 | 何时用 |
|------|------|-------|
| **Task Index 表** | `\| Adding a slash command \| docs/agents/cli-architecture.md \|` | 主入口推荐使用 |
| **章节末尾箭头** | `→ Full tree: docs/agents/project-structure.md` | 章节信息密度大，给一个 follow-up |
| **inline 引用** | "详见 `references/cheatsheet.md`" | 段落里需要导航时 |
| **References 索引段** | 列出所有子文件 + "何时读" 表 | 只在主文件最后一节用一次（avoid 重复） |

❌ **不要**用相对路径如 `./refs/foo.md` 或 `../docs/foo.md`——LLM 容易拼错。**始终用从仓库根 / `~` 开始的完整路径**。

---

## 4. 关键技术：硬约束的冗余编码

> **这是分层加载最容易踩的坑。** 在 hermes-agent 实测里，AI 跳过 docs/ 子文件直接读源码 → 关键约束被忽略 → 实际写出违规代码。
>
> 解决方法：**硬约束必须在 ≥ 2 个地方独立出现**，最理想是 4 层防御纵深。

### 4.1 软约束 vs 硬约束

| 类型 | 定义 | 例子 |
|------|------|------|
| **软约束** | 违反后**美化 / 一致性**问题，不出 bug | "skill 里同类内容尽量合到一节" |
| **硬约束** | 违反后**直接出 bug 或破坏不变量** | "用 `get_hermes_home()` 不要硬编码 `~/.hermes`"（破坏 profile 隔离） |

软约束放子文件即可。**硬约束必须冗余**。

### 4.2 4 层防御纵深（强 → 弱）

| 层 | 形式 | 强度 | 例子 |
|----|------|------|------|
| **L1: 类型系统** | `typing.Literal[...]` / `Enum` | ★★★★★ AI 几乎一定遵守 | `category: Literal["Session", "Configuration", ...]` |
| **L2: 运行时 assert** | `__post_init__` raise / 模块加载时校验 | ★★★★★ 违反时直接抛错 | `if cmd.category not in {...}: raise ValueError(...)` |
| **L3: 源码注释 + 测试** | `# one of: A / B / C` + 自动化测试 | ★★★★ AI 读源码会看到 | CommandDef.category 注释 + `test_all_categories_valid` |
| **L4: 文档** | `AGENTS.md` Hard Rules / `docs/<file>.md` | ★★ AI 可能跳读 | "Common Enums" 表 |

**经验法则**：
- 仅 L4 的硬约束 → **大概率被 LLM 违反**（实测有 hermes 反向覆盖修复的案例）
- L3 + L4 → 绝大多数 LLM 会遵守，但仍有 20% 失败率
- L1 + L3 + L4 → 接近 100% 安全
- L1 + L2 + L3 + L4 → 100% 安全（但代价是侵入代码）

### 4.3 实测案例：仅靠文档约束失守

**任务**：让 hermes 加 `/foo` 命令打印 hello。

**当时的"防御"**：
- ✅ L4 文档：`AGENTS.md` Common Enums 表写了 5 个合法 category
- ✅ L4 文档：`docs/agents/cli-architecture.md` 也写了
- ✅ L3 注释：`commands.py` 第 46 行 `category: str  # one of: "Session", ...`

**结果**：hermes 用了 `"Utility"`（5 个之外的值）。修好后让它再加 `/bar`，它**反向把 `/foo` 改回了 `"Utility"`**——LLM 的"demo 命令归 Utility 是常见模式"语义先验**碾压**了文档级约束。

**教训**：枚举类硬约束**必须**走 L1（`typing.Literal`）或 L2（运行时 assert），单靠 L3 + L4 不够。

### 4.4 实测案例：LLM 跳读子文件直接读源码

**任务**：同上。

**hermes 的活动序列**：
```
┊ 📚 skill   hermes-agent
┊ 💻 $       wc -l docs/agents/cli-architecture.md   ← 只看了一下行数
┊ 🔎 grep    class CommandDef
┊ 🔎 grep    COMMAND_REGISTRY
┊ 🔎 grep    def process_command
┊ 📖 read    hermes_cli/commands.py    ← 直接读源码
┊ 📖 read    cli.py
                                       ← 没有 Read docs/agents/cli-architecture.md
```

**教训**：**不要假定 LLM 会读 docs/ 子文件**。它经常优先读源码（合理偏好——源码是 ground truth）。所以：
- 子文件适合放"详情、范例、对比、历史"——遗漏不致命的内容
- "违反就 bug"的硬约束**必须**在源码本身（注释 + 类型 + 运行时校验）

---

## 5. 案例 A：`android-ui-automation/SKILL.md` 分层

### 5.1 数据

| 项 | 之前 | 之后 |
|----|------|------|
| 主 SKILL.md 行数 | 485 | 143 |
| 主 SKILL.md 字符 | 22,196 | 6,330 |
| 主 SKILL.md ~tokens | 5,549 | 1,582 |
| references/ 总字符 | — | 31,036（7 个文件） |
| **触发后注入节省** | — | **~3,966 tokens** |

### 5.2 拆分前后结构

```
之前：
~/.hermes/skills/testing/android-ui-automation/
└── SKILL.md (485 行)
    ├── frontmatter
    ├── v3.0 重大更新警告 + 复制即用代码块
    ├── 元素定位优先级表
    ├── 项目信息 + 知识库结构（distilled/ 目录树）
    ├── 核心模块页面清单（113 page 描述）
    ├── 环境准备：adb / venv / .env
    ├── 环境变量
    ├── Quirks（9 条）
    ├── v3.0 路径详解（5 档每档展开）
    ├── 主路径 + 学习进度
    └── v2 代码架构（automation/ 文件清单）

之后：
~/.hermes/skills/testing/android-ui-automation/
├── SKILL.md (143 行)         ← 主文件
│   ├── frontmatter
│   ├── 资源路径速查（5 行表）
│   ├── 元素定位优先级表（核心决策树）
│   ├── 复制即用代码块（最常用 3 段）
│   ├── Quirks 速查（9 条精简）
│   ├── 绝对不要做的事（6 条）
│   ├── 主路径（1 行）
│   └── references/ 索引（7 行表）
└── references/                ← 6 个分主题
    ├── cheatsheet.md          —— OCR/VL/CaseEngine 完整 API + 字段表
    ├── decision-tree.md       —— 5 档元素定位每档详解 + offset 经验值
    ├── project.md             —— 项目知识 + 113 page 清单 + 业务流程
    ├── setup.md               —— adb / venv / .env / 环境变量
    ├── architecture.md        —— automation/ 文件清单 + v2/v3 差异
    ├── quirks.md              —— 9 条 Quirk 详细背景
    └── changelog.md           —— v2→v3 变更 + Phase 1-4 进度
```

### 5.3 关键决策点

| 章节 | 留主 / 移走 | 理由 |
|------|-----------|------|
| frontmatter | 留主 | 必须 |
| v3.0 警告 | 浓缩 1 行 + 移历史到 changelog.md | "v2.2 不可靠"是教训，不是当前 API |
| 元素定位优先级表 | 留主 | 决策树，每次任务都需要 |
| 复制即用代码块 | 留主，3 段最常用 | 0 推理成本 |
| 113 page 清单 | 移 project.md | 90% 任务用不到 |
| adb 安装 | 移 setup.md | 一次性配置 |
| Quirks 9 条 | 留主（速查），详情移 quirks.md | 操作时随时撞，速查必须在 |
| 5 档路径详细代码 | 移 cheatsheet.md / decision-tree.md | 主文件留表，详细按需 |
| automation/ 文件清单 | 移 architecture.md | 改代码才需要 |

---

## 6. 案例 C：`AGENTS.md` 浓缩

### 6.1 数据

| 项 | 之前 | 之后 |
|----|------|------|
| 主 AGENTS.md 行数 | 751 | 138 |
| 主 AGENTS.md 字符 | 33,759 | 6,034 |
| 主 AGENTS.md ~tokens | 8,440 | 1,508 |
| docs/agents/ 总字符 | — | 33,723（13 个文件） |
| **每条对话节省** | — | **~6,930 tokens** |
| 100 turn 会话累计节省 | — | **~693k tokens** |

### 6.2 拆分前后结构

```
之前：
<repo>/
└── AGENTS.md (751 行, 15 个 ## 章节)

之后：
<repo>/
├── AGENTS.md (138 行)              ← 主文件
│   ├── Development Environment
│   ├── Hard Rules (do not violate)
│   │   ├── Profile-safe code
│   │   ├── Prompt caching
│   │   ├── Plugin-core boundary
│   │   ├── Testing
│   │   ├── Git / commits
│   │   ├── Display / TUI
│   │   ├── Tool schema descriptions
│   │   └── Gateway approval/control commands
│   ├── Path Speed Sheet (路径速查)
│   ├── Common Enums (封闭枚举防 LLM 自创)
│   ├── Task → docs/agents/ Index (子文件入口表)
│   └── Agent Loop One-liner
└── docs/agents/                    ← 13 个分主题
    ├── project-structure.md        —— 全树 + 依赖链
    ├── agent-class.md              —— AIAgent 类 + Agent Loop
    ├── cli-architecture.md         —— CLI / Slash Command Registry
    ├── tui-architecture.md         —— TUI Ink + JSON-RPC
    ├── adding-tools.md             —— 加 tool 完整流程
    ├── adding-configuration.md     —— config.yaml / .env / Loader
    ├── skin-theme-system.md        —— Skin 引擎 + 自定义 Skin
    ├── plugins.md                  —— General / Memory / Context-engine
    ├── skills.md                   —— skills/ 与 optional-skills/
    ├── policies.md                 —— Prompt cache / Background notif
    ├── profiles.md                 —— Profile-safe code 6 条规则
    ├── pitfalls.md                 —— 9 条 Known Pitfalls
    └── testing.md                  —— scripts/run_tests.sh + change-detector
```

### 6.3 always-applied 文件的特别考虑

`AGENTS.md` 是 Cursor / Claude Code 的 **always-applied** 工作区规则——每条对话都注入到 system prompt。这意味着：

| 考虑 | 影响 |
|------|------|
| **U 型注意力**问题最严重 | 中段（旧版第 300-500 行）的硬规则常被忽略；Hard Rules **必须**集中在主文件顶部 |
| **每行都计 token 成本** | 100 turn 对话节省 ~7k tok × 100 = 700k tok，是 always-applied 类文件最大优化空间 |
| **不能依赖 AI 主动 Read**（因为它已经"看完"主文件了） | Task Index 表必须**主动诱导** AI 去 Read 子文件 |
| **跨会话稳定** | 主文件改完后，每个新会话开局都看到，不需要 prompt |

由于这些特点，AGENTS.md 主文件的 Hard Rules 章节比 SKILL.md 主文件的 quirks 章节更"严"——必须把所有违反就 bug 的规则精简到一页内。

### 6.4 Common Enums 表的设计原因

主 AGENTS.md 第 97-104 行的 Common Enums 表是**专门为防 LLM 自创枚举值**设计的：

```markdown
### Common Enums (don't invent new values)

| Field | Allowed values |
|-------|----------------|
| `CommandDef.category` | `"Session"` / `"Configuration"` / `"Tools & Skills"` / `"Info"` / `"Exit"` |
| `OPTIONAL_ENV_VARS[...]["category"]` | `"provider"` / `"tool"` / `"messaging"` / `"setting"` |
| `display.background_process_notifications` | `"all"` / `"result"` / `"error"` / `"off"` |
```

但**实测证明**——单靠这个表 LLM 还是会犯错（见 4.3）。所以这个表的真实价值是 L4 防御层，**不能替代 L1/L2/L3**。未来如果 AI 又自创了新值，应该把那个枚举升级到 L1/L2 而不是 L4 加更多花字。

---

## 7. 完整工作流：下次想拆别的文件时

按这 8 步走，每步给具体命令。

### Step 1: 探查现状

```bash
# 文件大小
wc -l <FILE>.md
wc -c <FILE>.md

# 章节结构
grep -n "^## " <FILE>.md

# 估算 tokens（chars / 4 是粗估）
python3 -c "import os; print(f'~{os.path.getsize(\"<FILE>.md\")//4:,} tokens')"
```

**判定**：
- 行数 > 400 且 tokens > 4k → 继续
- 否则 → 不值得拆

### Step 2: 章节归类（每节去向）

把每个 `## ` 章节标到一个表里：

```
## Section A    → 留主文件（Hard Rule）
## Section B    → 留主文件（高频速查）
## Section C    → 移 docs/<file>/section-c.md
## Section D    → 移 docs/<file>/section-d.md
## Section E    → 移 docs/<file>/section-e.md（不常用）
```

按第 3 节的"主文件保留 5 类 / 子文件移走 5 类"规则归。

### Step 3: 决定主文件结构

主文件目标 ≤ 200 行 / ≤ 2k tokens。结构（按顺序）：

1. 一句话功能概述
2. 极简的 Quick Start / Speed Sheet（最常用 5-10 行表）
3. **Hard Rules**（违反就 bug 的清单，最重要！）
4. **Common Enums**（如果有封闭枚举）
5. **Task → docs Index**（必备！没有这个表 AI 不知道有子文件）
6. 极简 one-liner 描述核心机制（让 AI 不读子文件也懂主路径）

### Step 4: 写子文件

```bash
# 创建子目录（按命名约定）
mkdir -p docs/<file>/   # 或 references/

# 每个子文件写一个独立主题
# 第一行用 # H1 + "什么时候读这个" 提示
# 内容是从原文件迁移过来 + 不破坏语义的精简
```

每个子文件应该：
- 单一主题，不混搭
- 能独立读懂（不依赖姊妹文件的上下文）
- 文件开头给"什么时候读 / 想了解什么"的导航
- 文件名 kebab-case，描述主题（`adding-tools.md` 而不是 `tools.md`）

### Step 5: 写 Task Index 入口表

主文件最后一节加：

```markdown
## Task → docs/<file>/ Index

| Task | Read |
|------|------|
| 想做 X | `docs/<file>/x.md` |
| 想做 Y | `docs/<file>/y.md` |
| ... |
```

或者每个 Hard Rule 末尾加 `→ docs/<file>/<topic>.md`（章节末尾箭头）。

**关键**：Task Index 是**子文件能被读到的唯一保证**。没这个表 AI 不知道有这些文件可读。

### Step 6: 内容守恒验证

```bash
# 旧文件总字符数
git show HEAD:<FILE>.md | wc -c

# 新文件 + 子文件总字符数
wc -c <FILE>.md docs/<file>/*.md | tail -1

# 两者应该接近（子文件可能多 5-10% 是因为加了 H1 + 导航句）
```

### Step 7: 硬规则覆盖度验证

把所有 "DO NOT" / "MUST NOT" / 关键约束的关键短语列出来，用 grep 验证至少在两个地方出现：

```python
import subprocess
checks = [
    ("DO NOT hardcode `~/.hermes` paths", ["AGENTS.md", "docs/agents/pitfalls.md"]),
    ("simple_term_menu", ["AGENTS.md", "docs/agents/pitfalls.md"]),
    # ... 把每条硬约束的关键短语都列上
]
for needle, expected_files in checks:
    res = subprocess.run(["grep", "-rl", "-E", needle, "AGENTS.md", "docs/agents/"],
                         capture_output=True, text=True)
    found = set(line for line in res.stdout.strip().split("\n") if line)
    missing = set(expected_files) - found
    print(f"{needle:<55} {'OK' if not missing else f'MISSING {missing}'}")
```

**全部 OK 才算合格**——少一个就要补回去（可能换措辞了导致 grep 不命中）。

### Step 8: 实测调用观察

让一个新会话的 AI 做一个**故意需要读子文件才能做对**的任务（例如：加一个 slash 命令、加一个工具、改一个 quirk），观察：

1. AI 是否真的去 `Read` 了 Task Index 指向的子文件？
2. AI 是否遵守了所有硬约束？
3. 如果违反了某条硬约束 → 把那条规则**升级一级防御**（L4 → L3 → L2 → L1）

实测的目的是**校准 LLM 服从度**——不要假定文档级约束足够。

---

## 8. 验证脚本（可复制粘贴）

### 8.1 Token 估算

```python
"""统计主文件 + 子文件的 token 占用 / 节省"""
from pathlib import Path

def estimate(main_file: Path, sub_dir: Path | None = None) -> dict:
    main_chars = main_file.stat().st_size
    sub_chars = sum(p.stat().st_size for p in (sub_dir.glob("*.md") if sub_dir else []))
    return {
        "main": (main_chars, main_chars // 4),
        "sub": (sub_chars, sub_chars // 4),
        "total": (main_chars + sub_chars, (main_chars + sub_chars) // 4),
    }

# 例：跑 AGENTS.md
import subprocess
old_chars = int(subprocess.check_output(
    ["git", "show", "HEAD:AGENTS.md"]).decode().__len__())
result = estimate(Path("AGENTS.md"), Path("docs/agents"))
print(f"旧主文件: {old_chars:,} chars / ~{old_chars//4:,} tokens")
print(f"新主文件: {result['main'][0]:,} chars / ~{result['main'][1]:,} tokens")
print(f"子文件:   {result['sub'][0]:,} chars / ~{result['sub'][1]:,} tokens (按需加载)")
print(f"每对话省: ~{(old_chars - result['main'][0]) // 4:,} tokens")
```

### 8.2 内容守恒检查

```bash
# 旧主文件
OLD_SIZE=$(git show HEAD:<FILE>.md | wc -c)

# 新主文件 + 所有子文件
NEW_SIZE=$(cat <FILE>.md docs/<file>/*.md | wc -c)

echo "Old: $OLD_SIZE"
echo "New: $NEW_SIZE"
echo "Delta: $((NEW_SIZE - OLD_SIZE)) (>0 表示有新增内容如速查表/H1, 正常 +5-15%)"
```

### 8.3 硬规则 grep 覆盖度

把硬规则一致措辞放进 `checks` 数组，跑（用 Python 因为兼容性好）：

```python
import subprocess
import sys

# 改成你的项目实际的硬规则关键短语
RULES = [
    ("DO NOT hardcode `~/.hermes` paths",        ["AGENTS.md", "docs/agents/pitfalls.md"]),
    ("DO NOT hardcode cross-tool",                ["AGENTS.md", "docs/agents/adding-tools.md"]),
    ("simple_term_menu",                          ["AGENTS.md", "docs/agents/pitfalls.md"]),
    ("Plugins MUST NOT modify core files",        ["docs/agents/plugins.md"]),
    ("ALWAYS use `scripts/run_tests.sh`",         ["AGENTS.md", "docs/agents/testing.md"]),
    ("get_hermes_home()",                         ["AGENTS.md", "docs/agents/profiles.md"]),
]
all_ok = True
for needle, expected in RULES:
    res = subprocess.run(["grep", "-rl", "-E", needle, *sum([[p] for p in {f.split("/")[0] for f in expected}], [])],
                         capture_output=True, text=True)
    found = {line for line in res.stdout.strip().split("\n") if line}
    missing = set(expected) - found
    status = "OK" if not missing else f"MISS {missing}"
    print(f"{needle:<60}  {status}")
    if missing: all_ok = False
sys.exit(0 if all_ok else 1)
```

### 8.4 链接路径正确性

```bash
# 把所有 Markdown 里的相对路径链接抽出来，逐一验证文件存在
python3 -c "
import re
from pathlib import Path
for md in [Path('AGENTS.md')] + list(Path('docs/agents').glob('*.md')):
    text = md.read_text()
    for m in re.finditer(r'\`(docs/[^\`]+\.md)\`', text):
        target = Path(m.group(1))
        if not target.exists():
            print(f'BROKEN: {md} → {target}')
print('Link check done')
"
```

---

## 9. 反模式（已踩过的坑）

### ❌ 反模式 1：把 Hard Rules 放子文件

```markdown
## Hard Rules
→ See docs/agents/hard-rules.md
```

**为什么错**：硬规则是"永远要看到"的，放子文件意味着 AI 必须主动 Read 才看得到，违反就会 bug。**Hard Rules 必须留主文件**。

### ❌ 反模式 2：拆得过碎

把 700 行拆成 30 个子文件——每个子文件 < 50 行，AI 不知道该 Read 哪个，反而比集中的"小目录里大文件"难导航。

**正确粒度**：8-15 个子文件，每个 50-200 行。

### ❌ 反模式 3：没有 Task Index

主文件只写 Hard Rules + 速查，不告诉 AI 子文件在哪/什么时候读。

**结果**：AI 不知道有子文件可读，子文件等于不存在。

### ❌ 反模式 4：用相对路径模糊引用

```markdown
详见 ./refs/foo.md  或  ../docs/foo.md  或  refs/foo.md
```

LLM 容易拼错或拼到错的地方。**始终用从仓库根开始的完整相对路径**：

```markdown
详见 `docs/agents/foo.md`  或  `~/.hermes/skills/<name>/references/foo.md`
```

### ❌ 反模式 5：把封闭枚举仅放在 L4 文档

```markdown
| `CommandDef.category` | one of: A / B / C / D / E |
```

**实测**：LLM 仍可能用 `"Utility"` / `"Other"` 这种"看起来合理"的新值。**封闭枚举必须 L1（typing.Literal）或 L2（运行时 assert）**。

### ❌ 反模式 6：子文件之间互相依赖

子文件 A 写"接前文（见子文件 B）..."——AI 读 A 时无法独立理解，必须连着 B 一起读。**每个子文件必须独立可读**。

### ❌ 反模式 7：拆完不验证

写完不跑 token 估算 / 内容守恒 / 硬规则 grep / 链接检查。

**至少**跑一遍硬规则 grep 检查（第 8.3 节脚本）。

### ❌ 反模式 8：把"实测教训"塞到主文件里

主文件应该是"现在该怎么做"，不是"以前为什么这样"。教训放 `changelog.md` 之类的子文件。

---

## 10. 何时升级防御层级

实测中如果发现某条硬约束被 LLM 违反，按下表升级：

| 现象 | 升级到 |
|------|-------|
| 硬约束写在子文件里被忽略 | + 写一份到主文件 Hard Rules |
| 主文件 Hard Rules 写了仍被违反 | + 写到源码注释（L3） |
| 源码注释写了仍被违反（特别是枚举类） | + `typing.Literal[...]`（L1） |
| 类型仍被绕过（动态构造 / type: ignore） | + `__post_init__` 运行时 assert（L2） |
| 运行时 assert 仍被绕（已发布版本） | + 写测试在 CI 拦截 |

**经验法则**：枚举类硬约束（"必须是这 N 个值之一"）**直接上 L1/L2**，别浪费时间做 L3/L4。

---

## 11. 这套方法学的边界

**不是银弹。** 以下情况直接拆未必好：

1. **小文件**（<300 行）—— 拆完管理成本高于收益
2. **教程性文档**—— 流程连贯性比 token 节省更重要
3. **API 参考文档**—— 用户/AI 一般会全文搜索（grep / 全文索引），分层反而麻烦
4. **极少加载的文件**—— 加载频率低，token 节省不显著
5. **代码已经实现强约束**—— 已经有 `typing.Literal` 的 enum，AGENTS.md 不必再写一遍

如果以上特征明显，就别套这套方案——重写更紧凑、删冗余即可。

---

## 12. hermes-agent 项目里的实例（直接引用）

| 文件 | 类型 | 主文件路径 | 子文件目录 | 实测节省 |
|------|------|-----------|-----------|---------|
| 项目根工作区规则 | always-applied | `<repo>/AGENTS.md` | `<repo>/docs/agents/` | ~6,930 tok / 对话 |
| Android UI 自动化 skill | 高频触发 | `~/.hermes/skills/testing/android-ui-automation/SKILL.md` | `~/.hermes/skills/testing/android-ui-automation/references/` | ~3,966 tok / 触发 |

**做新拆分时直接对照这两个实例**——它们各自是"项目根工作区规则"和"hermes skill"两类的标杆。

---

## 13. 给未来 AI 的话

如果你（Cursor / Claude Code / 其他 AI）正在调试这个项目并撞到下面任一情况：

| 情况 | 该做什么 |
|------|---------|
| `AGENTS.md` 只有 138 行 | 这是分层加载主文件，详情在 `docs/agents/`——按 Task Index 表找对应子文件 |
| `SKILL.md` 旁边有 `references/` 目录 | 同上，先读主 SKILL.md 的"references/ 索引"小节 |
| 想加新硬约束 | 先决定它是 L1/L2/L3/L4 哪一层（见第 4 节）。枚举类直接 L1+L2，文档不够 |
| 想优化别的长 markdown | 按本文第 7 节的 8 步工作流执行，最后跑第 8 节的验证脚本 |
| 看到本方法学有破绽 | 加到第 9 节"反模式"或第 10 节"何时升级"，不要静默忽略 |

**永远先读主文件，再决定要不要 Read 子文件。** 不要因为子文件存在就跳过主文件，也不要因为主文件简短就以为没规则。
