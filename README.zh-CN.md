# Hermes Agent ☤ 中文使用文档

> 适用版本：`hermes-agent` 0.10.0
> 项目主页：<https://hermes-agent.nousresearch.com>
> 仓库：<https://github.com/NousResearch/hermes-agent>

Hermes Agent 是 **Nous Research** 推出的「会自我进化」的 AI Agent：它会从对话中沉淀技能（skills）、自动整理记忆、能在终端 / Telegram / Discord / Slack / WhatsApp / 飞书 / 企业微信等平台上工作，可以跑在你的笔记本、$5 的 VPS、Docker、SSH、Daytona、Modal 等几乎任何环境里。

本文档覆盖：环境准备 → 安装 → 首次配置 → 基础使用 → 进阶玩法 → 常见问题。

---

## 目录

1. [项目结构速览](#1-项目结构速览)
2. [环境要求](#2-环境要求)
3. [安装方式](#3-安装方式)
4. [首次配置（设置向导）](#4-首次配置设置向导)
5. [启动与基础使用](#5-启动与基础使用)
6. [常用 CLI 子命令](#6-常用-cli-子命令)
7. [对话中的斜杠命令](#7-对话中的斜杠命令)
8. [模型与提供商管理](#8-模型与提供商管理)
9. [工具系统（Tools / Toolsets）](#9-工具系统tools--toolsets)
10. [技能系统（Skills）](#10-技能系统skills)
11. [记忆与会话](#11-记忆与会话)
12. [消息平台网关 Gateway](#12-消息平台网关-gateway)
13. [TUI / Ink 终端界面](#13-tui--ink-终端界面)
14. [定时任务 Cron](#14-定时任务-cron)
15. [Profiles 多实例隔离](#15-profiles-多实例隔离)
16. [配置文件位置](#16-配置文件位置)
17. [开发与测试](#17-开发与测试)
18. [常见问题排查](#18-常见问题排查)

---

## 1. 项目结构速览

```text
hermes-agent/
├── run_agent.py          # AIAgent 类，核心对话循环
├── cli.py                # HermesCLI 交互式 CLI
├── model_tools.py        # 工具调度
├── toolsets.py           # 工具集合定义
├── hermes_state.py       # SQLite 会话存储（带 FTS5 全文搜索）
├── hermes_cli/           # CLI 各种子命令（setup / model / tools / gateway 等）
├── tools/                # 40+ 个工具实现（每个工具一个文件）
├── gateway/              # 消息平台网关（Telegram / Discord / Slack / WhatsApp / 飞书 / 钉钉 / 企微 …）
├── skills/               # 内置技能库（自动同步到 ~/.hermes/skills/）
├── ui-tui/               # 基于 Ink (React) 的现代 TUI
├── tui_gateway/          # TUI 的 Python JSON-RPC 后端
├── acp_adapter/          # ACP 协议适配器（VS Code / Zed / JetBrains 集成）
├── cron/                 # 定时任务调度器
├── environments/         # 强化学习训练环境（Atropos）
├── tests/                # ~3000 个 pytest 用例
├── pyproject.toml
└── setup-hermes.sh       # 一键开发环境安装脚本
```

用户配置统一存放在 `~/.hermes/`：

| 文件 | 用途 |
|---|---|
| `~/.hermes/config.yaml` | 主要设置（模型、工具开关、显示外观等） |
| `~/.hermes/.env` | API Key 等密钥 |
| `~/.hermes/skills/` | 已安装的技能 |
| `~/.hermes/sessions.db` | 会话 SQLite 数据库 |
| `~/.hermes/profiles/` | 多实例（profile）独立目录 |

---

## 2. 环境要求

- **操作系统**：macOS、Linux、WSL2、Android（Termux）。**不支持原生 Windows**，请用 WSL2。
- **Python**：3.11 或更高（推荐 3.11，仓库内 `pyproject.toml` 限定 `>=3.11`）。
- **可选**：
  - `ripgrep`（`rg`）—— 大幅加速文件搜索；
  - Node.js 18+ —— 仅在你想用 TUI（`hermes --tui`）时需要；
  - Docker / SSH —— 仅在你需要在容器或远程主机里执行命令时需要；
  - 各家 LLM 的 API Key（OpenRouter / OpenAI / Anthropic / Gemini / GLM / Kimi / MiniMax / Hugging Face / Nous Portal …，任选其一即可）。

---

## 3. 安装方式

### 方式 A：官方一键脚本（最快）

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

安装完成后：

```bash
source ~/.bashrc      # zsh 用户：source ~/.zshrc
hermes                # 启动 Hermes
```

### 方式 B：克隆仓库并使用 `setup-hermes.sh`（推荐开发者用）

仓库已经在你的工作目录 `/Users/yhn/PyCharmMiscProject/hermes-agent-main`，可以直接：

```bash
cd /Users/yhn/PyCharmMiscProject/hermes-agent-main
./setup-hermes.sh
```

脚本会自动：

1. 安装 [`uv`](https://docs.astral.sh/uv/) 包管理器；
2. 准备 Python 3.11；
3. 创建 `venv/` 虚拟环境并执行 `uv sync --all-extras --locked`（带哈希校验）；
4. 把 `hermes` 命令软链到 `~/.local/bin/hermes`；
5. 复制 `.env.example` 到 `.env`；
6. 同步 `skills/` 到 `~/.hermes/skills/`；
7. 询问是否立即运行 `hermes setup` 设置向导。

> 工程根目录还有一个 `./hermes` 包装脚本，会自动找到 `venv/` 里的 Python，所以 **不必手动 `source venv/bin/activate`** 也能运行 `./hermes ...`。

### 方式 C：手动用 `uv` 或 `pip` 安装

```bash
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent

# 使用 uv（推荐）
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"

# 或使用 pip
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[all]"          # 全功能
# 或最小安装
pip install -e "."
```

`pyproject.toml` 中提供的可选依赖组：

| extra | 作用 |
|---|---|
| `messaging` | Telegram、Discord、Slack 等消息平台 |
| `slack` / `matrix` / `dingtalk` / `feishu` | 单独某个平台 |
| `voice` | 本地语音转写（Whisper） |
| `tts-premium` | ElevenLabs 高级 TTS |
| `mcp` | MCP（Model Context Protocol）客户端 |
| `cron` | 定时任务 |
| `honcho` | Honcho 用户记忆 |
| `acp` | VS Code / Zed / JetBrains 集成 |
| `web` / `bedrock` / `mistral` | 其他能力 |
| `dev` | 测试 + 调试 |
| `all` | 全部开（推荐） |

---

## 4. 首次配置（设置向导）

```bash
hermes setup
```

向导分 5 个独立模块，可分别重跑：

1. **模型 / Provider**：选择默认 LLM。可选：
   - **Nous Portal**（Nous Research 自家）
   - **OpenRouter**（一个 Key 用 200+ 模型）
   - **OpenAI / Anthropic / Google Gemini**
   - **NVIDIA NIM、z.ai (GLM)、Kimi (Moonshot)、MiniMax、Hugging Face、Ollama Cloud、Arcee、OpenCode Zen/Go、Mistral、Bedrock**
   - **自定义**：填 `base_url + api_key`，可对接 vLLM / llama.cpp / OneAPI 等任意 OpenAI 兼容端点。
2. **终端后端**：在哪里执行 shell 命令？
   - `local`（默认） / `docker` / `ssh` / `daytona` / `modal` / `singularity`
3. **Agent 设置**：最大迭代次数、自动压缩上下文、会话重置策略等。
4. **消息平台**：勾选要启用的 Telegram / Discord / Slack / WhatsApp / 飞书 / 钉钉 / 企微 / Signal / Matrix / Email / Home Assistant 等，并交互式输入对应 Token。
5. **工具配置**：TTS、Web 搜索（Parallel / Firecrawl / Exa）、图像生成（fal.ai）、视觉模型、浏览器（Browserbase）等。

如果只想改其中一项，也可以单独跑：

```bash
hermes model        # 改模型 / 提供商
hermes tools        # 配置工具开关
hermes config set <key> <value>   # 直接改某个 config.yaml 字段
hermes config get <key>
hermes config list
```

> 所有 API Key **不会被写进 git**，只保存在 `~/.hermes/.env` 中。

---

## 5. 启动与基础使用

```bash
hermes                  # 默认：进入交互式 CLI
hermes chat             # 同上
hermes "写个 Python 脚本统计当前目录代码行数"   # 一次性提问
hermes --tui            # 启动现代 Ink TUI（需要 Node.js）
hermes status           # 查看当前配置状态
hermes doctor           # 诊断：检测依赖 / 网络 / 配置问题
hermes version          # 版本号
hermes update           # 升级到最新版
hermes uninstall        # 卸载
```

进入对话后界面大致如下：

```
☤ Hermes Agent · anthropic/claude-opus-4.6 · ~/code/myproj
> 帮我看看这个目录里有什么 bug
```

**键盘快捷键：**
- `Ctrl+C` —— 中断当前任务（再按一次退出）
- `Ctrl+D` —— 退出
- `Tab` —— 自动补全斜杠命令 / 文件路径
- `↑ / ↓` —— 历史输入
- 多行输入：直接粘贴，或用 `\` 续行

---

## 6. 常用 CLI 子命令

```bash
# 会话
hermes sessions browse        # 交互式浏览历史会话（带搜索）
hermes sessions resume <id>   # 恢复某个会话

# 模型
hermes model                  # 交互选择模型
hermes model list             # 列出已登录的所有 provider/模型
hermes model use openrouter:anthropic/claude-opus-4.6

# 工具 / 技能
hermes tools                  # curses 界面切换工具开关
hermes skills list
hermes skills install <name>  # 从 agentskills.io 安装

# 凭证
hermes login <provider>       # OAuth 登录（Nous Portal、Copilot 等）
hermes logout
hermes auth status

# 网关（消息平台）
hermes gateway                # 前台运行
hermes gateway start          # 后台守护进程
hermes gateway stop
hermes gateway status
hermes gateway install        # 安装为 systemd / launchd 服务
hermes gateway uninstall

# 定时任务
hermes cron list
hermes cron status

# Profiles（多实例）
hermes profile list
hermes profile create <name>
hermes -p coder          # 用 coder profile 启动一次

# Honcho（用户记忆）
hermes honcho setup
hermes honcho status

# 从 OpenClaw 迁移
hermes claw migrate --dry-run

# 编辑器集成
hermes acp                    # 启动 ACP 协议服务，给 VS Code / Zed / JetBrains 用
```

完整列表：`hermes --help` 或查看 `hermes_cli/main.py` 的文件头注释。

---

## 7. 对话中的斜杠命令

输入 `/` 会出现自动补全。下面按类别列出主要命令（CLI 和大多数消息平台都通用）：

### 会话控制
| 命令 | 说明 |
|---|---|
| `/new`、`/clear` | 开启全新会话 |
| `/history` | 查看完整对话历史 |
| `/save [名字]` | 保存当前对话 |
| `/retry` | 重发上一条消息 |
| `/undo` | 删除上一轮对话 |
| `/title <名字>` | 给本会话起标题 |
| `/branch <名字>` | 从当前分叉一个新会话 |
| `/compress` | 手动压缩上下文 |
| `/rollback` | 列出 / 恢复文件系统快照 |
| `/snapshot` | 创建 / 恢复 Hermes 状态快照 |
| `/stop` | 杀掉所有后台进程 |
| `/background <prompt>` | 把任务丢到后台跑 |
| `/queue <prompt>` | 排队，下一回合再处理 |
| `/steer <message>` | 在下一次工具调用之间插入指引 |
| `/agents` | 查看正在运行的子 Agent |
| `/resume <名字>` | 恢复一个曾命名的会话 |

### 配置
| 命令 | 说明 |
|---|---|
| `/config` | 查看当前配置 |
| `/model [provider:model]` | 切换模型（本会话 / `--global` 永久） |
| `/provider` | 查看已配置的 provider |
| `/personality [名字]` | 切换预设人格（用 `~/.hermes/SOUL.md` 等） |
| `/statusbar` | 切换底部上下文/模型状态条 |
| `/verbose` | 工具进度详细程度循环：off → new → all → verbose |
| `/yolo` | 开关 YOLO 模式（跳过所有危险命令确认） |
| `/reasoning` | 推理强度（low/medium/high） |
| `/fast` | OpenAI Priority / Anthropic Fast Mode |
| `/skin [名字]` | 切换主题皮肤（default / ares / mono / slate / 自定义） |
| `/voice` | 切换语音模式 |

### 工具与技能
| 命令 | 说明 |
|---|---|
| `/tools [list\|enable\|disable] [...]` | 管理工具 |
| `/toolsets` | 查看可用工具集合 |
| `/skills` | 浏览 / 搜索 / 安装技能 |
| `/<skill 名>` | 直接执行某个技能 |
| `/cron` | 管理定时任务 |
| `/reload` | 重新加载 `.env` |
| `/reload-mcp` | 重新加载 MCP 服务器 |
| `/browser` | 把浏览器工具连接到你的 Chrome（CDP） |
| `/plugins` | 列出已安装插件 |

### 信息
| 命令 | 说明 |
|---|---|
| `/help` | 帮助 |
| `/commands` | 翻页浏览所有命令和技能 |
| `/usage` | 当前会话 token 用量、限速 |
| `/insights [--days N]` | 用量分析 |
| `/platforms` | 网关平台状态 |
| `/copy` | 把上一条回复复制到剪贴板 |
| `/paste` | 从剪贴板贴一张图片 |
| `/image <文件>` | 在下一条提问前附加本地图片 |
| `/update` | 升级 Hermes |
| `/debug` | 上传诊断报告 |
| `/quit` | 退出 |

---

## 8. 模型与提供商管理

Hermes 内置了对 **15+ 个 LLM 提供商** 的统一封装（见 `hermes_cli/auth.py` 的 `PROVIDER_REGISTRY`）。三种使用方式：

### 8.1 用 OpenRouter 一站式（最简单）

在 `~/.hermes/.env` 中：

```dotenv
OPENROUTER_API_KEY=sk-or-...
```

然后：

```bash
hermes model use openrouter:anthropic/claude-opus-4.6
# 或
hermes model use openrouter:openai/gpt-5
hermes model use openrouter:google/gemini-2.5-pro
```

### 8.2 直连官方 API

```dotenv
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
GLM_API_KEY=...
KIMI_API_KEY=sk-kimi-...
MINIMAX_API_KEY=...
HF_TOKEN=hf_...
```

```bash
hermes model use openai:gpt-5
hermes model use anthropic:claude-opus-4.6
hermes model use gemini:gemini-2.5-pro
```

### 8.3 自定义 OpenAI 兼容端点（vLLM / llama.cpp / OneAPI / 公司内网）

```dotenv
OPENAI_BASE_URL=https://my-internal-llm.company.com/v1
OPENAI_API_KEY=anything-or-empty
```

或在 `config.yaml` 里用 dict 形式：

```yaml
model:
  default: my-model-name
  provider: custom
  base_url: https://my-internal-llm.company.com/v1
  api_key: xxx
```

### 8.4 多 Key 池 / 灰度

`hermes setup` 中的"凭证池策略"可以为同一个 provider 配置多个 Key 做轮询、负载均衡或灾备。

---

## 9. 工具系统（Tools / Toolsets）

工具实现都在 `tools/` 目录，每个工具一个文件，**自动发现**：只要文件中调用了 `registry.register(...)`，就会被自动注册。

代表性工具：

| 工具 | 文件 | 说明 |
|---|---|---|
| `terminal` | `tools/terminal_tool.py` | 执行 shell 命令（支持 local/docker/ssh/modal/daytona） |
| `process` | `tools/process_registry.py` | 后台进程管理 |
| `read_file` / `write_file` / `patch` / `search_files` | `tools/file_tools.py` | 文件操作 |
| `web_search` / `web_extract` | `tools/web_tools.py` | Parallel / Firecrawl / Exa |
| `browser_*` | `tools/browser_tool.py` | Browserbase / CDP 浏览器自动化 |
| `vision_analyze` | `tools/vision_tools.py` | 多模态视觉 |
| `image_generate` | `tools/image_generation_tool.py` | fal.ai 图像生成 |
| `text_to_speech` | `tools/tts_tool.py` | Edge TTS / ElevenLabs |
| `execute_code` | `tools/code_execution_tool.py` | 沙箱代码执行 |
| `delegate_task` | `tools/delegate_tool.py` | 派生子 Agent 并行 |
| `mcp_*` | `tools/mcp_tool.py` | MCP 协议客户端 |
| `cronjob` | `tools/cronjob_tools.py` | 定时任务 |
| `send_message` | `tools/send_message_tool.py` | 跨平台发消息 |
| `ha_*` | `tools/homeassistant_tool.py` | Home Assistant 智能家居 |
| `memory` / `todo` / `skills_*` / `session_search` | 多文件 | 自我学习相关 |

**Toolsets** 是一组工具的命名集合，定义在 `toolsets.py`：

```bash
hermes tools                  # 交互式启用 / 禁用
/toolsets                     # 在对话里查看
/tools list
/tools disable web
```

---

## 10. 技能系统（Skills）

技能 = 可复用的「领域 SOP」。每个技能是一个目录，里面有 `SKILL.md` 描述触发条件，Agent 会在合适时机自动加载。仓库 `skills/` 下内置了 25+ 类：

```
apple   autonomous-ai-agents  creative   data-science  devops
diagramming  domain  email  feeds  gaming  github  inference-sh
mcp  media  mlops  note-taking  productivity  red-teaming
research  smart-home  social-media  software-development …
```

`./setup-hermes.sh` 会把它们同步到 `~/.hermes/skills/`，你也可以：

```bash
hermes skills list
hermes skills install <name>          # 从 agentskills.io 安装第三方技能
/skills                               # 在对话里搜索 / 浏览
/<skill-name>                         # 直接触发
```

Hermes 的最大特色之一是 **会自己写技能**：完成复杂任务后，它会主动总结成新的 SKILL.md 写进 `~/.hermes/skills/`。

---

## 11. 记忆与会话

- **会话存储**：`~/.hermes/sessions.db`（SQLite + FTS5），所有对话默认持久化，`session_search` 工具支持全文检索 + LLM 总结。
- **记忆**：
  - `MEMORY.md` —— 全局记忆，Agent 自己增删改；
  - `USER.md` —— 用户画像；
  - `SOUL.md` —— 人格 / 角色设定。
- **Honcho（可选）**：接入 [Honcho](https://github.com/plastic-labs/honcho) 做对话式用户建模，支持「混合模式」与本地记忆共存。
  ```bash
  hermes honcho setup
  hermes honcho mode hybrid
  ```
- **上下文压缩**：超出模型上下文时自动压缩；也可手动 `/compress`。

---

## 12. 消息平台网关 Gateway

让 Hermes 不再绑死在你的笔记本上 —— 跑在服务器，通过 IM 跟它对话。

```bash
hermes gateway setup       # 配置启用哪些平台、谁能用、工作目录
hermes gateway             # 前台启动（开发调试用）
hermes gateway start       # 后台守护进程
hermes gateway status
hermes gateway stop
hermes gateway install     # 安装为系统服务（systemd / launchd）
```

支持的平台（`gateway/platforms/`）：

- **Telegram**（推荐入门）
- **Discord**
- **Slack**
- **WhatsApp**（Twilio）
- **Signal**
- **飞书 / Lark**（含富文本评论）
- **企业微信 (WeCom)**、**钉钉 (DingTalk)**
- **微信 (Weixin)**（社区方案，参见 [HermesClaw](https://github.com/AaronWong1999/hermesclaw)）
- **Matrix**
- **Email** / **SMS** / **Webhook** / **Mattermost** / **HomeAssistant** / **BlueBubbles (iMessage)**
- **QQ Bot**（`gateway/platforms/qqbot/`）

通用环境变量：

```dotenv
TELEGRAM_BOT_TOKEN=...
DISCORD_BOT_TOKEN=...
SLACK_BOT_TOKEN=...
SLACK_APP_TOKEN=...
HOMEASSISTANT_URL=...
HASS_TOKEN=...
MESSAGING_CWD=/var/lib/hermes-workspace    # 网关工作目录（默认家目录）
```

详细每个平台的对接步骤参考 `gateway/platforms/ADDING_A_PLATFORM.md` 与官方 docs。

---

## 13. TUI / Ink 终端界面

如果你装了 Node.js 18+，可以体验更现代的 Ink (React) TUI：

```bash
hermes --tui
# 或
HERMES_TUI=1 hermes
```

开发模式：

```bash
cd ui-tui
npm install
npm run dev          # 监听构建
npm start            # 生产模式启动
npm run type-check
npm run lint
npm test
```

底层走 stdio JSON-RPC（`tui_gateway/`）跟 Python Agent 通信，前端负责所有渲染、补全、流式输出、审批弹窗等。

---

## 14. 定时任务 Cron

需要安装 `cron` extra（`pip install -e ".[cron]"`）。

```bash
hermes cron list
hermes cron status
```

或在对话里：

```
/cron
/cronjob
帮我每天早上 8 点把昨天的 GitHub 通知整理成一封邮件发给我
```

任务可以指定通过哪个平台投递（CLI 日志 / Telegram / Email …）。

---

## 15. Profiles 多实例隔离

每个 profile 有独立的 `HERMES_HOME`，互不干扰（独立配置、Key、记忆、技能、会话）：

```bash
hermes profile list
hermes profile create coder
hermes profile create personal

hermes -p coder           # 用 coder 启动一次
hermes -p personal setup  # 给 personal 单独跑设置向导
```

底层机制见 `AGENTS.md` 的 "Profiles" 章节 —— `hermes_cli/main.py:_apply_profile_override()` 在导入任何模块前就设置好 `HERMES_HOME` 环境变量。

---

## 16. 配置文件位置

| 路径 | 内容 |
|---|---|
| `~/.hermes/config.yaml` | 主配置（模型、显示、工具、网关 …） |
| `~/.hermes/.env` | API Keys / Tokens |
| `~/.hermes/sessions.db` | 会话历史 |
| `~/.hermes/skills/` | 用户技能 |
| `~/.hermes/skins/*.yaml` | 自定义皮肤 |
| `~/.hermes/MEMORY.md`、`USER.md`、`SOUL.md` | 记忆 / 人格 |
| `~/.hermes/profiles/<name>/` | 各 profile 自己的副本 |
| `~/.hermes/agent.log`、`errors.log` | 运行日志 |

仓库根目录的 `cli-config.yaml.example` 是一份带详细注释的样例配置，可以作为模板抄到 `~/.hermes/config.yaml`。

---

## 17. 开发与测试

激活虚拟环境后再操作：

```bash
source venv/bin/activate
```

**运行测试（强制使用包装脚本，保持与 CI 一致）：**

```bash
scripts/run_tests.sh                          # 全量
scripts/run_tests.sh tests/gateway/           # 单个目录
scripts/run_tests.sh tests/agent/test_x.py::test_y
scripts/run_tests.sh -v --tb=long             # 透传 pytest 参数
```

> **不要直接 `pytest`** —— `scripts/run_tests.sh` 会清掉本地 API Key、固定 TZ=UTC / LANG=C.UTF-8、限定 4 个 xdist worker，保持和 GitHub Actions 完全一致，避免「本地通过 / CI 失败」。

**新增工具：** 在 `tools/your_tool.py` 中调用 `registry.register(...)`，再在 `toolsets.py` 把名字加到 `_HERMES_CORE_TOOLS` 即可，无需手动 import。

**新增斜杠命令：** 在 `hermes_cli/commands.py` 的 `COMMAND_REGISTRY` 加一条 `CommandDef`，再分别在 `cli.py:process_command()` 和（可选）`gateway/run.py` 加分支处理。

**新增配置项：** 改 `hermes_cli/config.py` 中的 `DEFAULT_CONFIG`，并把 `_config_version` +1（触发已有用户的 config 自动迁移）。

更深的开发指南见仓库根目录的 `AGENTS.md` 与 `CONTRIBUTING.md`。

---

## 18. 常见问题排查

### Q1：`hermes: command not found`
- 还没把 `~/.local/bin` 加到 PATH。
  ```bash
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
  source ~/.zshrc
  ```
- 或者直接用仓库根目录的包装脚本：`./hermes`（自动找到 `venv/`）。

### Q2：第一次进去就报 "no provider configured"
- 跑 `hermes setup` 选一个 provider，或者在 `~/.hermes/.env` 里手动填 `OPENROUTER_API_KEY=...` 然后 `hermes model use openrouter:anthropic/claude-opus-4.6`。

### Q3：网络在国内 / 公司代理后面
- `~/.hermes/.env` 里加：
  ```dotenv
  HTTPS_PROXY=http://127.0.0.1:7890
  HTTP_PROXY=http://127.0.0.1:7890
  ```
- 或在 `config.yaml`：
  ```yaml
  network:
    force_ipv4: true
  ```

### Q4：`hermes doctor` 提示缺依赖
- 全功能：`uv pip install -e ".[all]"`；
- 只想要消息平台：`uv pip install -e ".[messaging]"`；
- Termux：脚本会自动用 `.[termux]`。

### Q5：被卡在某个长任务想中断
- `Ctrl+C` 一次：中断当前工具调用，让 Agent 收到中断信号；
- 再按一次：完全退出 CLI。
- 在对话里：`/stop` 杀掉所有后台进程。

### Q6：怎么完全清掉重来？
```bash
hermes uninstall                    # 卸载 hermes
rm -rf ~/.hermes                    # 清掉所有用户数据（谨慎）
rm -rf venv                         # 清虚拟环境
```

### Q7：从 OpenClaw 迁移
```bash
hermes claw migrate --dry-run       # 先预览
hermes claw migrate                 # 实际迁移：SOUL/MEMORY/USER/技能/Key/平台 …
```

### Q8：想把 Hermes 接进 VS Code / Zed / JetBrains
- 安装 `acp` extra：`uv pip install -e ".[acp]"`
- 启动：`hermes acp`
- 编辑器侧用对应的 ACP（Agent Client Protocol）插件连接即可。

---

## 拓展阅读

- 官方完整文档：<https://hermes-agent.nousresearch.com/docs/>
- 架构与开发约定：本仓库 `AGENTS.md`
- 安全策略：`SECURITY.md`
- 贡献指南：`CONTRIBUTING.md`
- 各版本变更：仓库根目录的 `RELEASE_v*.md`
- 社区：[Discord](https://discord.gg/NousResearch) ｜ [Skills Hub](https://agentskills.io) ｜ [Issues](https://github.com/NousResearch/hermes-agent/issues)

---

> 本文档为基于源码（v0.10.0）整理的中文使用指南，命令与配置项以 `hermes --help` 和 `cli-config.yaml.example` 的实际输出为准。
