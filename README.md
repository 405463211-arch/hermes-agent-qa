# hermes-agent-qa

> 测试工程师训练 hermes agent 能力，迭代视觉 UI 自动化、接口自动化、功能用例、mockserver 等。
>
> 本仓库是 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的个人 fork（基线
> upstream **v0.12.0**），在其之上叠加了 **Android 真机 UI 自动化能力**、**LCM 长上下文记忆引擎**、
> **三段式记忆 + 提升梯度学习评分体系**、**Curator 技能伞状合并**、**Self-Learning 错误模式观察**、
> **Obsidian Vault Bridge** 等本地特色。

---

## 目录

1. [Hermes 当前核心能力（继承自 upstream）](#1-hermes-当前核心能力继承自-upstream)
2. [本 fork 新增特色 ① — Android UI 自动化能力](#2-本-fork-新增特色---android-ui-自动化能力)
3. [本 fork 新增特色 ② — 记忆压缩工程](#3-本-fork-新增特色---记忆压缩工程)
4. [本 fork 新增特色 ③ — 三段式记忆储存](#4-本-fork-新增特色---三段式记忆储存)
5. [本 fork 新增特色 ④ — 学习能力评分体系](#5-本-fork-新增特色---学习能力评分体系)
6. [本 fork 新增特色 ⑤ — Skill 生成优化（Curator）](#6-本-fork-新增特色---skill-生成优化curator)
7. [本 fork 新增特色 ⑥ — Self-Learning Plugin](#7-本-fork-新增特色---self-learning-plugin)
8. [本 fork 新增特色 ⑦ — Obsidian Vault Bridge](#8-本-fork-新增特色---obsidian-vault-bridge)
9. [本地 Plugins / Slash 命令一览](#9-本地-plugins--slash-命令一览)
10. [Quick Install / Getting Started](#10-quick-install--getting-started)
11. [License & 致谢](#11-license--致谢)

---

## 1. Hermes 当前核心能力（继承自 upstream）

| 能力 | 简述 |
|---|---|
| **自改进 Agent** | 内置学习闭环：从经验创建 skill / 使用中持续打磨 / 主动 nudge 落盘 / FTS5 全文检索过往会话 / 跨 session 用户建模（Honcho） |
| **Provider 自由** | Nous Portal / OpenRouter（200+ 模型）/ NVIDIA NIM / Xiaomi MiMo / z.ai / Kimi / MiniMax / HuggingFace / OpenAI / 自建 endpoint —— `hermes model` 一键切换，无代码改动 |
| **多平台 Messaging Gateway** | Telegram / Discord / Slack / WhatsApp / Signal / Email —— 单 gateway 进程跨平台对话连续，支持语音转写 |
| **Cron 调度** | 自然语言定时任务（"每天早上 9 点把昨天的 git commit 发到我 Telegram"）+ 跨平台 delivery |
| **子代理并行** | spawn 隔离 subagent + Python script 通过 RPC 调工具，把多步 pipeline 折叠成 0 上下文成本的单轮 |
| **6 种 Terminal Backend** | local / Docker / SSH / Daytona / Singularity / Modal —— Daytona / Modal 提供 serverless persistence（hibernate-on-idle） |
| **TUI** | 完整终端界面：多行编辑 / 斜杠命令自动补全 / 会话历史 / Ctrl+C 中断重定向 / 工具输出流式渲染 |
| **TUI in Dashboard** | `hermes dashboard` 的 `/chat` 路由通过 `pty_bridge.py` + WebSocket + xterm.js 嵌入真实 `hermes --tui`，浏览器和终端共用同一会话 |
| **40+ 工具** | file/terminal/web_search/browser/python/skill/memory/learning/... 自动按 toolset 装载 |
| **Skills 系统** | 程序性记忆 / [agentskills.io](https://agentskills.io) 标准 / Skills Hub 一键安装 / SKILL.md frontmatter |
| **Research-ready** | 批量轨迹生成 / Atropos RL environments / 轨迹压缩用于训练下一代工具调用模型 |

---

## 2. 本 fork 新增特色 ① — Android UI 自动化能力

资产位置：`~/.hermes/project-knowledge/diancaibao-app/automation/`（30+ 个 Python 文件）+ `distilled/pages/*.yaml`（90+ 真实页面知识库）+ `plugins/ui-automation/dashboard/`（运行历史可视化）。

### 2.1 元素定位 5 级回退（按精度 / 速度 / 成本梯度）

| 档位 | 引擎 | 命中场景 | VLM 成本 |
|---|---|---|---|
| **L1** | rapidocr / PaddleOCR **精确匹配**（exact / contains / regex / near_text / char_offset） | 文本可见 + 字体清晰 ~80% 场景 | 0 |
| **L2** | OCR + **region 区域限定** | 已知元素在屏幕某区块 | 0 |
| **L3** | **ShowUI-2B 本地 grounding** + OmniParser snap | 图标 / 无文本按钮，本地推理 70% 准 | 0 |
| **L4** | 云端 **qwen3-vl-flash** + bbox_2d 归一化 + scipy 像素吸附 | 全新页面 / 复杂语义定位 | 1 次 VLM call |
| **L5** | **keypad yaml 缓存** | 数字键盘 / 固定布局键盘 | 0 |

### 2.2 OCR 引擎（`ocr_engine.py`）

- **双引擎**：rapidocr（默认，~100ms/帧）+ PaddleOCR（fallback，对小字体 / 旋转文字更稳）
- **6 种声明类型**：`text` / `exact` / `near_text` / `char_offset` / `icon` / 复合（"在 X 文字下方 80px 的图标"）
- **i18n 同义词表**（`distilled/i18n.json`）：把 "营业额" / "销售额" / "Revenue" 归一到同一锚点

### 2.3 bbox_2d 0-1000 归一化协议（`vl_engine.py`）

历史教训：v1 让 VLM 直接返回像素坐标，对 1080×2400 这种屏幕偏移高达 1194 像素 → v3 强制归一化协议：

```
qwen3-vl-flash system prompt 强制要求 bbox_2d 在 [0, 1000] 区间内
↓ from_json 解析
↓ 用真实 image_dims 反算回像素
↓ scipy 连通域吸附（±80px 窗口）
↓ calibrator.assert_in_screen 边界校验
↓ adb.tap(x, y)
```

**为什么必须归一化**：VLM 的训练数据用的是相对坐标，直接让它输出像素会引入分辨率偏差；归一化后偏移量随屏幕分辨率线性缩放，跨设备一致。

### 2.4 页面识别（`page_matcher.py`）

每个页面 yaml 有 4 类指纹，组合成稳健识别：

| 指纹 | 作用 | 匹配方式 |
|---|---|---|
| `must_contain_text` | 必须出现的文本（标题、固定 label） | 子串匹配 |
| `forbidden_text` | 不能出现的文本（区分相似页面） | 反向断言 |
| `activity_hint` | Android Activity 名提示 | adb dumpsys 校验 |
| `texts_seen_extra` | 高判别度文本集合 | **Jaccard 相似度**（OCR 集合 vs yaml 集合） |

**双引擎策略**：OCR 命中且 Jaccard > 阈值 → 直接确认；Top-2 模糊 → 回退到 `vl.page_match_visual` VLM 兜底。约 **80% 场景跳过 VL 调用**。

### 2.5 scipy 连通域吸附（`vl_engine._apply_snap`）

VLM 给的坐标常落在按钮边缘外 5-30 像素，吸附逻辑：

```python
1. 截图灰度化 + 二值化
2. scipy.ndimage.label 找连通域
3. 在 ±80px 窗口内找最近的连通域质心
4. 把 VL 坐标"吸"到该质心 → 真实可点击点
```

**为什么 hermes 需要这层**：qwen3-vl-flash 是 7B-class 模型，定位精度 ±20px；吸附把它拉回到真实 UI 元素的几何中心，点击成功率从 ~70% 提到 ~95%。

### 2.6 lazy_upgrader 自演进（`lazy_upgrader.py`）

命中草稿页面（`_create_stub` 自动生成的占位）时：

```
读 Flutter source_file (_page.dart + _logic.dart)
  ↓ LLM 精修页面 yaml
  ↓ 覆写 distilled/pages/<page>.yaml
```

知识库**按需演进**——只对真正访问过的页面读源码 + LLM 精修，避免一次性扫全 113 个 page。

### 2.7 AUTO_LEARNED 自学习坐标缓存（`auto_learner.py`）

成功定位的（page_id, target）→ (x, y) 写入 cache，后续命中：

| 优先级 | 行为 | VLM 成本 |
|---|---|---|
| 1 | cache `lookup_cached_xy()` 命中 → 直接 _tap | **0** |
| 2 | miss + ShowUI 可用 → 本地定位 + record_success 种 cache | **0** VL |
| 3 | miss + ShowUI 不可用 → RuntimeError 提示先校准 | — |

**实测收益**：第 2 轮跑同 case → cache 全命中 → **0 VL + 0 ShowUI 调用**，跑用例 70%+ 时间省下。

### 2.8 VLM Budget + dHash 校验（`vlm_budget.py`）

- **Budget**：`HERMES_AUTOMATION_VLM_TOTAL_BUDGET=N` 限制单次 case 最大 VL 调用数，防止失控循环烧 token
- **Tap 后 dHash 校验**：点击前后截图对比 dHash，差异低于阈值 → 判定"瞎点"，触发 healer 重试或失败上报

### 2.9 运行历史持久化 + Dashboard 时间线

落盘约定：`~/.hermes/cache/ui-automation/runs/<YYYY-MM-DD>/<case>_NNN/{steps/, feedback.yaml, result.json}`

Dashboard endpoint（`plugins/ui-automation/dashboard/plugin_api.py`）：

| Endpoint | 用途 |
|---|---|
| `GET /api/v1/plugins/ui_automation/runs` | 运行历史列表 |
| `GET /api/v1/plugins/ui_automation/runs/{date}/{case}_{n}` | 单次运行详情（步骤 / 截图 / 决策日志） |
| `GET /api/v1/plugins/ui_automation/screenshot/{rel_path}/{filename}` | 截图（路径强校验在 `runs_root` 内防遍历） |

---

## 3. 本 fork 新增特色 ② — 记忆压缩工程

文件：`agent/context_compressor.py`（1300+ 行）+ `plugins/context_engine/lcm/engine.py`（1100+ 行）

### 3.1 标准压缩器优化点（在 upstream 基础上）

| 优化 | 解决什么 |
|---|---|
| **Head + Tail 双保护** | head（system + 前 N 轮上下文）+ middle（被压缩为 summary）+ tail（最近 K 轮）—— 让最远的"任务说明"和最近的"工作上下文"都不丢 |
| **Token-budget tail 保护** | 不固定保留 K 轮，按 tail token 预算反推（默认 20% threshold_tokens）—— 长会话不会因 tail 退化为单轮 |
| **Iterative summary 更新** | 多次压缩之间继承上一次 summary 而非重做，避免 N 次压缩后细节流失 |
| **Tool output 预剪枝** | 旧的 `tool` 角色消息内容替换为 `[Old tool output cleared]`，summary 之前先省 50%+ token |
| **Scaled summary budget** | 按"被压缩内容的 20%"动态分配 summary tokens（最低 2000，最高 12000），不是固定 1024 |
| **Multimodal 算成本** | 每张图固定 1600 tokens / 6400 字符等价，避免"5 张图的对话被算成接近 0 token"误判 |
| **Cross-provider reasoning_content 防污染** | DeepSeek-R1 / Kimi-thinking / Anthropic extended-thinking 的 `<think>` 块在跨 provider 重放时会污染历史 → 按 provider 决定是否补 reasoning pad |

### 3.2 LCM —— 检索式 vs 摘要式（`plugins/context_engine/lcm/`）

| 维度 | 标准压缩器 | **LCM 引擎** |
|---|---|---|
| 处理方式 | 一次性 LLM summary 整段历史 | 每条消息嵌入到 SQLite + 向量库 |
| 召回方式 | 不可召回（已被压缩成自然语言摘要） | `lcm_search` / `lcm_recall` 工具按需召回原文 |
| 适合 provider | OpenAI / Claude（aux summary 快） | **GLM-5.1 / DeepSeek-R1**（aux summary 30-90s 慢） |
| 失真度 | 跨多次压缩持续累积 | 0 失真（存原文） |
| 召回粒度 | summary 颗粒度 | 单条消息 / 单个 tool 输出 |

**典型场景**：长会话里"上次让你看过的那段错误日志再贴出来" —— 标准压缩已丢，LCM 能 0 推理成本检索回原文。

### 3.3 句感切分 + 重叠窗口（LCM 切片）

LCM 把消息切成 ≤ 6000 char 的 chunk，**不是简单 char 切**，而是按下面优先级找最近的边界：

```
1. paragraph (\n\s*\n)
2. cn_sentence ([。！？])
3. en_sentence ([.!?] + \s)
4. newline
5. cn_clause ([；，])
6. en_clause ([;,] + \s)
7. space (兜底)
```

**为什么必须句感**：embedding 模型对完整句子的向量稳定，切到 mid-word（"...he started writ" / "ing the function"）会让两个 chunk 都"载噪声"，召回率明显下降。每个 chunk 之间留 200 char 重叠保留跨边界上下文。

---

## 4. 本 fork 新增特色 ③ — 三段式记忆储存

设计参考：`docs/memory-and-learning-design.md`

### 4.1 为什么不是一个大 memory bucket？

upstream 早期版本所有 memory 都进 `MEMORY.md`，单 bucket 强迫模型每轮对每条 entry 三类判断（重要 / 一般 / 过期）。三段式把这个决策**前置到写入时一次完成**：

| Bucket | 心智模型 | 是否当指令读 | 是否自动归档 | 写入方 |
|---|---|---|---|---|
| **`rules`** | 必须遵守的红线 | ✅ | ✅ 80% 容量 + 90d 年龄双触发 | agent（自动）+ user（手动 `/rules add`） |
| **`memory`** | 工作笔记（陈述事实） | ❌ 仅作背景 | ❌ 但 LCM 溢出 | user 为主 |
| **`user`** | 用户身份层（who you are） | ❌ | ❌ | user 手动 |

### 4.2 注入顺序（`run_agent.py::_build_system_prompt`）

```
SOUL.md (identity)
  ↓
PINNED rules        ← 最高显著性，永不归档
  ↓
REGULAR rules       ← 普通层，[NEW] 标签出现在这里
  ↓
... tool guidance / skill guidance ...
  ↓
MEMORY.md           ← 工作笔记，优先级低
  ↓
USER.md             ← 用户身份层
```

**为什么这个顺序**：LLM 注意力对靠近 task 末端的 token 拉力更强（U 型注意力）—— pinned rules 是红线，必须放最显著的位置。

### 4.3 自动归档双触发（防御纵深）

| 触发 | 条件 | 为什么 |
|---|---|---|
| **A. 容量** | 序列化 RULES.md > 80% `rules_char_limit` | 给下次 promotion 留余量；100% 触发会立即再触发 |
| **B. 年龄** | 仅 LRN-* 规则 + 90 天未 recurrence + 30 天未 edit | 用户手写规则永不归档（agent 不擅自删用户意图） |

### 4.4 `[NEW]` 试用期（7 天）

`learning_record` 自动 promote 的规则带 `[NEW — verify before applying]` 标记 7 天：
- **信号**：告诉模型这条规则是**推断的**，不是 user 显式确认的
- **保护**：试用期内豁免年龄归档（一条规则不可能"既在试用又过期")
- **窗口**：给用户一周时间核实并删除

7 天后自动摘掉标记，进入正常 lifecycle。

---

## 5. 本 fork 新增特色 ④ — 学习能力评分体系

文件：`agent/learning_store.py`（500+ 行 SQLite ledger）+ `tools/learning_tool.py`

### 5.1 SQLite Schema（核心字段）

```sql
CREATE TABLE learnings (
    id              TEXT PRIMARY KEY,    -- LRN-YYYYMMDD-XXXXXX (6 hex char)
    category        TEXT NOT NULL,       -- learning | error | feature_request
    pattern_key     TEXT NOT NULL,       -- 稳定 dedupe key
    summary         TEXT NOT NULL,
    suggested_action TEXT,               -- promotion 后变成 rule body
    recurrence_count INTEGER DEFAULT 1,  -- 复发次数
    distinct_tasks  INTEGER DEFAULT 1,   -- 跨任务命中数
    first_seen      REAL,                -- 首次观察时间戳
    last_seen       REAL,                -- 最近观察时间戳
    status          TEXT DEFAULT 'pending', -- pending | promoted | resolved | ...
    promoted_to     TEXT,                -- 'rules' | 'skill:<name>' | NULL
    promoted_at     REAL
);
```

### 5.2 提升梯度（promotion ladder）—— 三阈值合议

```python
class PromotionRule:
    min_recurrence: int = 3      # 至少复发 3 次
    min_distinct_tasks: int = 2  # 至少跨 2 个不同任务
    window_days: int = 30        # 首次到末次窗口 ≤ 30 天
```

四个条件**全部成立**才能 auto-promote 进 RULES.md：

| 条件 | 防什么失败模式 |
|---|---|
| `status == 'pending'` | 防止已 promote 的规则归档后再被重新提升（死循环） |
| `recurrence ≥ 3` | 防止偶然事件被当成规律 |
| `distinct_tasks ≥ 2` | 防止"同一 bug 重试 10 次"被当成 pattern（需要跨任务证据） |
| `window ≤ 30d` | 防止"3 个月前的旧 pattern 今天又冒头"被算成新 pattern（需要近期密集复发） |

**三阈值缺一不可**：单凭 count 会捕捉重试事件；单凭 distinct_tasks 会算上巧合；单凭 window 会算上极少复发的噪声。三者合议 = pattern + 跨任务证据 + 近期性。

### 5.3 ID 抗碰撞设计（6 hex 后缀）

历史教训：原 3 hex 后缀（4096 组合）在 M9 稳定性测试 500 个 pattern_key/天时碰撞率 ~100%（生日悖论）。升级到 6 hex（16M 组合）：

| 单日写入数 N | 碰撞概率 |
|---|---|
| 100 | 0.0003% |
| 1000 | 0.003% |
| 10000 | 3% |

旧 3-char ID 仍然可读（column 是 TEXT，免迁移）。

### 5.4 学习写入路径

```
agent 观察到一个错误 / 知识缺口 / 用户偏好
  ↓
agent 主动调 learning_record(category="learning"|"error"|"feature_request",
                              pattern_key="...", summary="...", task_id="...")
  ↓
LearningStore.record():
   pattern_key 已存在? → recurrence_count++ / last_seen 刷新 / distinct_tasks 累加
   不存在?            → INSERT 新行
  ↓
is_eligible_for_promotion() 校验四阈值
  ↓
合格 → add_rule_with_lifecycle(text, source=LRN-id, promoted_at=now)
       → RULES.md 落盘 + hermes-meta block
       → status = 'promoted' (永久，不可重新 auto-promote)
       → 7 天 [NEW] 试用期开始
```

### 5.5 `/learn` 显式管理命令（本 fork 新增）

| 子命令 | 行为 |
|---|---|
| `/learn list` | 列出所有 pending 学习项（按 recurrence 降序） |
| `/learn show <id>` | 查看某条详情 + 是否 eligible |
| `/learn stats` | 类目统计 + promotion 漏斗 |
| `/learn promote <id>` | 手动强制 promote（绕过自动门槛） |

---

## 6. 本 fork 新增特色 ⑤ — Skill 生成优化（Curator）

文件：`agent/curator.py`（1200+ 行）—— **闲时自动 review skill 库的 background agent**。

### 6.1 触发条件（防打扰）

| Gate | 默认值 | 含义 |
|---|---|---|
| `enabled` | True | 总开关 |
| `paused` | False | 临时暂停 |
| `interval_hours` | **24×7 = 168h** | 距上次 review 间隔 |
| `min_idle_hours` | 2h | 当前 idle 时间下限（agent 不忙才跑） |

满足全部 → `maybe_run_curator()` fork 一个 AIAgent 跑 review。**用 auxiliary client，不污染主会话 prompt cache**。

### 6.2 自动状态迁移（无 LLM，纯函数）

```python
apply_automatic_transitions():
    for skill in agent_created_skills:  # 不动 bundled / hub-installed
        if pinned: continue              # 不动 pinned
        anchor = last_activity_at or created_at
        if anchor <= now - 90d:  → STATE_ARCHIVED
        elif anchor <= now - 30d and STATE_ACTIVE:  → STATE_STALE
        elif anchor > now - 30d and STATE_STALE:    → STATE_ACTIVE  (重新激活)
```

### 6.3 Umbrella-building（伞状合并）

Curator agent 的 system prompt 强制以下规则（`CURATOR_REVIEW_PROMPT` 100+ 行）：

| 铁律 | 含义 |
|---|---|
| **目标是 class-level skill**，不是 narrow-skill | 一个伞状 skill + N 个 labeled subsection > N 个独立 narrow skill |
| **不准删**，只能 archive | 归档可恢复，删除不可恢复 |
| **不准动 bundled / pinned skill** | 仅处理 agent-created skill |
| **不能用 use_count = 0 当跳过理由** | 计数器太新，不能作为价值判据 |
| **不能用"trigger 不同"当跳过理由** | 维护者会写成一个 skill 多个 subsection 的，就该合并 |

### 6.4 4 种处理 + 结构化输出

| 动作 | 何时用 |
|---|---|
| **MERGE INTO EXISTING UMBRELLA** | 簇里已有一个足够宽的 skill 当伞 → patch 它加 subsection，归档其他 |
| **CREATE NEW UMBRELLA** | 没有现成宽 skill → `skill_manage create` 新建一个 class-level skill 吸收所有 |
| **DEMOTE TO references/templates/scripts** | 窄但有价值的 session-specific 内容 → 移到伞 skill 的支持目录 |
| **KEEP** | 已经是 class-level 伞，且没有合并机会 |

强制输出结构化 yaml block 让下游工具能区分**合并** vs **修剪**：

```yaml
consolidations:
  - from: hermes-config-keys
    into: hermes-config
    reason: 同一 skill 加 subsection 即可，无需独立
prunings:
  - name: pr-1234-fix-login
    reason: 一次性修复，已合入主分支，无复用价值
```

每个被 archive 的 skill **必须**出现在 `consolidations` 或 `prunings` 中——降下来的报告才能区分"价值被吸收 vs 真正废弃"。

### 6.5 报告落盘

`~/.hermes/logs/curator/{YYYYMMDD-HHMMSS}/run.json` + `REPORT.md`，与 `agent.log` 同目录便于运维查询。

---

## 7. 本 fork 新增特色 ⑥ — Self-Learning Plugin

文件：`plugins/self_learning/{plugin.yaml, error_detector.py}` —— 首次安装即激活（`auto_enable: true`）。

### 7.1 两个 hook，纯被动观察

```yaml
hooks:
  - post_tool_call    # 观察工具错误
  - pre_llm_call      # 在合适时机注入 nudge
```

### 7.2 工作流

```
post_tool_call 收到工具结果
  ↓ classify_tool_error(result) 把错误归到一个 pattern_key
    （NPE / 文件不存在 / 权限拒绝 / 网络超时 / API 限流 / ...）
  ↓ 累加 per-session 计数
  ↓ 若某 pattern 复发 ≥ DEFAULT_NUDGE_THRESHOLD（默认 2）
pre_llm_call 注入一行 system_hint：
  "[SYSTEM HINT] 你已经撞到 <pattern> 2 次了，
   可以考虑调 learning_record 把规律记下来。"
```

### 7.3 三条不可动摇的设计原则

| 原则 | 为什么 |
|---|---|
| **纯观察，从不修改 tool 结果** | plugin 不破坏工具语义 |
| **不替 agent 写 learning** | agent 是 memory 的唯一写入方（`AGENTS.md` 红线）；plugin 替写会产生 stealth entries |
| **节流：每 pattern_key 每 session 一次 nudge** | 防止 spam |

新会话重新计数 —— `LearningStore` 本身按 pattern_key 跨会话 dedupe，不会双记录；plugin 的 per-session 计数只是**触发 nudge 的本会话信号**。

---

## 8. 本 fork 新增特色 ⑦ — Obsidian Vault Bridge

文件：`agent/obsidian.py`（936 行）—— 把 hermes 已有的 5 层记忆扩展成 6 层。

### 8.1 第 6 层：人工长期手写知识库

| 层 | 内容 | 写入方 |
|---|---|---|
| 1 | `RULES.md` | agent 自动 + user 手动 |
| 2 | `MEMORY.md` | user 手动 |
| 3 | `USER.md` | user 手动 |
| 4 | `~/.hermes/project-knowledge/` | user 手动 + lazy_upgrader |
| 5 | `LearningStore` SQLite + LCM 向量库 | agent 自动 |
| **6** | **Obsidian Vault**（用户多年手写） | **user 长期手写 + agent 受限写入** |

### 8.2 三档 Scope 安全模型

| Scope | 范围 | 默认 |
|---|---|---|
| `hermes_subdir` | 仅 `vault/hermes/` | ✅ |
| `ingest` | + `vault/hermes/ingest/` 显式白名单 | opt-in |
| `all` | 整个 vault | 充分信任后启用 |

防止 `obsidian_search` 把 user 的 daily note（"老板真烦"）暴露给 LLM。

### 8.3 写入受限

agent 通过 `obsidian_save` 的所有写入**默认**落 `vault/hermes/notes/`，**永不**写 user 自己的笔记目录。

### 8.4 Profile 感知

vault 路径全局共享（一人通常一个 vault），但每个 profile 在 vault 内有独立子目录 → `hermes -p coder` 与 `hermes -p personal` 在同一 vault 共存不冲突。

### 8.5 自动导出 hook

`run_agent.py::AIAgent._finalize_session` 末尾调用 `from agent import obsidian as _ob`，把会话级摘要按 profile 写入对应子目录，让 user 在熟悉的 Obsidian 编辑器里翻 hermes 的工作记录。

---

## 9. 本地 Plugins / Slash 命令一览

### 9.1 本 fork 新增 Plugins

| Plugin | 用途 |
|---|---|
| `plugins/context_engine/lcm` | 长上下文记忆引擎（embed-and-stash 替代 summarize-and-discard） |
| `plugins/self_learning` | 错误模式被动观察 + 节流 nudge |
| `plugins/ui-automation/dashboard` | UI 自动化运行历史时间线 + 截图 endpoint |
| `plugins/disk-cleanup` | 定期清理 `~/.hermes/cache/` / 临时截图 / 旧日志 |
| `plugins/observability/langfuse` | LLM 调用 trace 上报 Langfuse 后端 |

### 9.2 本 fork 新增 Slash 命令

| 命令 | Handler | 用途 |
|---|---|---|
| `/lcm` | `cli.py::_handle_lcm_command` | 查询 / 召回 LCM 长上下文记忆 |
| `/rules` | `cli.py::_handle_rules_command` | 双层规则注入管理（pinned + regular） |
| `/memory` | `cli.py::_handle_memory_command` | 显式管理 MEMORY.md / USER.md |
| `/learn` | `cli.py::_handle_learn_command` | 学习库管理（list / show / stats / promote） |

### 9.3 本 fork 新增 Toolset（默认 off）

| Toolset | 工具 | 启用方式 |
|---|---|---|
| `learning` | `learning_record` / `learning_list` / `learning_promote` | `hermes tools` 勾选 |
| `obsidian` | `obsidian_search` / `obsidian_view` / `obsidian_save` | `hermes tools` 勾选 |
| `project_knowledge` | `pk_search` / `pk_view` / `pk_distill` | `hermes tools` 勾选 |

---

## 10. Quick Install / Getting Started

```bash
# upstream 一键安装（Linux / macOS / WSL2 / Termux）
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash

source ~/.bashrc                  # 或 source ~/.zshrc
hermes                            # 进入交互 CLI

# 本 fork 开发（贡献者路径）
git clone <this-fork-url>
cd hermes-agent-main
./setup-hermes.sh                 # 装 uv + 建 venv + 装 .[all] + symlink ~/.local/bin/hermes
./hermes                          # 自动检测 venv，无需 source
```

| 命令 | 用途 |
|---|---|
| `hermes` | 进入交互 CLI |
| `hermes model` | 选 LLM provider / model |
| `hermes tools` | 启停 toolset（含本 fork 新增 3 个：`learning` / `obsidian` / `project_knowledge`） |
| `hermes config set` | 改单个配置项 |
| `hermes gateway` | 启动 messaging gateway（Telegram/Discord/Slack/...） |
| `hermes setup` | 完整 setup wizard |
| `hermes update` | 更新 hermes |
| `hermes doctor` | 诊断环境 |

> **Windows**：原生不支持，请走 [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install)。
> **Android / Termux**：装 `.[termux]` extra（`.[all]` 会拉 Android 不兼容的语音依赖）。

---

## 11. License & 致谢

- **License**：MIT（继承 upstream），见 [LICENSE](LICENSE)
- **Upstream**：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com)
- **Fork 用途**：测试工程师视角的 QA 增强 + Android UI 自动化训练 + 个人知识库（Obsidian）集成
- **Upstream 完整文档**：https://hermes-agent.nousresearch.com/docs/
