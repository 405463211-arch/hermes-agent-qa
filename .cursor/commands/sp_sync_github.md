# /sp_sync_github — 安全把本地工程同步到 GitHub

> **基于一次真实 push 的经验沉淀**（首次把 hermes-agent-main 推到 hermes-agent-qa）。
>
> 适用场景：本地工程改了很多东西想推到 GitHub 备份，但担心密钥泄露、担心 fork 上游历史不全、担心代码被改、担心 README 冲突等。

---

## 使用方式

```
/sp_sync_github                              # 默认推到当前 origin
/sp_sync_github 推到我的新仓库 git@github.com:xxx/yyy.git
/sp_sync_github 把上游 hermes-agent 同步到我的 fork
```

## 何时使用

- 第一次把本地工程推到一个**新建的 GitHub 仓库**
- 本地有大量 untracked / modified 文件，需要先做密钥安全扫描再 push
- 本地是 fork，上游有新版本想拉下来又怕被覆盖（先 push 备份再 merge）
- 担心 detached HEAD 上的改动哪天被 git GC 清掉

> **不适用**：日常单分支 push（直接 `git push` 即可，无需走本流程）

---

## 核心铁律

| 铁律 | 说明 |
|---|---|
| **公开仓库先扫密钥再 push** | push 是不可逆的；一旦泄露只能 rotate 所有 key + force push 重写历史 |
| **点开头 ≠ 隐私** | `.gitignore` `.dockerignore` `.envrc` `.github/` 等都是项目配置，必须公开；真正的密钥靠 `.gitignore` 屏蔽 `.env` |
| **fetch ≠ pull** | `git fetch --unshallow upstream` **只补全 git 数据库**，不动工作区；只有 `git pull` / `git merge` 才会改代码 |
| **shallow clone 不能 push** | 如果 `.git/shallow` 存在，GitHub 会拒绝（"did not receive expected object"），必须先 `git fetch --unshallow upstream` |
| **公开仓库再小的私密文档也要排除** | `.cursor/docs/` 里的家庭部署计划、本地路径、profile 名都属于隐私 |

---

## 执行流程（8 步）

### Step 1: 现状诊断

```bash
git status              # 看 untracked / modified 数量
git remote -v           # 看远端配置
git branch --show-current   # 是否在分支上（detached HEAD 要先绑分支）
ls .git/shallow 2>/dev/null && echo "⚠️ 是 shallow clone" || echo "✓ 完整 clone"
git log -1 --format='%an'       # 看 committer 名字是否对
```

**判断点**：
- detached HEAD？→ 先 `git switch -c main` 绑分支
- shallow clone？→ Step 6 必须先 unshallow
- committer name 不是你想要的（如默认的 `<user>`）？→ Step 7 设置 `git config user.name`

---

### Step 2: 三个决策（必须问用户）

| 决策 | 选项 |
|---|---|
| **目标仓库** | A. fork 上游公开 / B. 自建 private 备份 / C. 自建 public 仓库 |
| **commit 拆分** | A. 一个大 commit（最快）/ B. 拆 2-4 个主题（推荐折中）/ C. 按文件类型精细拆 |
| **隐私文件** | A. 排除所有可疑（`.cursor/docs/` `WORK_IN_PROGRESS.md` 等）/ B. 只排部署文档 / C. 全入库（仅 private 适用） |

> public 仓库 → 强烈选 A 排除所有可疑；private 仓库 → 看个人偏好

---

### Step 3: 密钥安全扫描（**push 前的最后一道防线**）

```bash
# 1. 验证 .env 已被忽略
git check-ignore -v .env

# 2. 主流密钥模式扫描（已 staged + 已修改 + untracked 全覆盖）
git diff --cached | grep -E "^\+" | grep -E "(sk-[A-Za-z0-9]{20,}|sk-ant-|ghp_|gho_|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|hf_[A-Za-z0-9]{30,}|xoxb-[0-9])"
git diff | grep -E "^\+" | grep -E "(sk-[A-Za-z0-9]{20,}|sk-ant-|ghp_|gho_|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"

# 3. untracked 大目录扫密钥
git ls-files --others --exclude-standard | head -30
# 用 Grep 工具针对每个新目录扫上面那批模式

# 4. 文件名敏感词
git ls-files --others --exclude-standard | grep -iE "(\.env$|\.env\.|secret|credential|\.key$|\.pem$|\.ppk$|password|token)"

# 5. 硬编码本地路径（暴露 username）
grep -rE "/Users/<your-username>|/home/<your-username>" --include="*.py" --include="*.md" -l . 2>/dev/null | head -10
```

**任何一步命中真实密钥** → 立即停止，要么从文件里删掉，要么加到 `.gitignore`，再重新 staged。

---

### Step 4: 加 .gitignore 隐私规则

根据 Step 2 的决策 + Step 3 的扫描结果，在 `.gitignore` 末尾追加：

```gitignore

# Local-only docs (privacy: home deploy plans, work-in-progress notes, local paths)
.cursor/docs/
WORK_IN_PROGRESS.md      # 如果有
TEST_REPORT.md           # 如果有
*.local.md               # 通用本地笔记后缀
```

`.cursor/commands/` `.cursor/skills/` `.cursor/prompts/` 等通用资产**保留入库**，只排 `.cursor/docs/`。

---

### Step 5: 改 README.md（fork 场景）

如果是 fork 公开仓库，且新仓库已经有 Initial commit 含一行简短 README，**两份 README 文件名相同但内容不同会冲突**。三选一：

| 方案 | 命令策略 | 适合 |
|---|---|---|
| A. 保留远端 README | `git checkout --theirs README.md` 后 `git add` | 新仓库定位说明已经写好 |
| B. 用本地覆盖远端 | `git checkout --ours README.md` 后 `git add` | 不在乎首页提示 |
| **C. 合并（推荐）** | 在本地 README 顶部插入 fork 简介段落，merge 时 `--ours` | 既保留 fork 定位 + 又保留上游完整介绍 |

C 方案模板（手动加到本地 README.md 最顶部）：

```markdown
# <你的仓库名>

> <一行中文/英文说明这个 fork 是干嘛的>
>
> 本仓库是 [upstream-org/upstream-repo](https://github.com/upstream-org/upstream-repo) 的个人 fork，
> 基线 commit `<上游基线 hash>` (`<版本>`)，在其基础上添加了 <核心改动>。

---

下文为 <upstream-repo> 上游原始 README：

<原 README 内容>
```

---

### Step 6: 远端配置 + commit + push

```bash
# 6.1 detached HEAD 绑分支（如有）
git switch -c main

# 6.2 双远端配置：upstream = 原上游（只读同步），origin = 你的（push 目标）
git remote rename origin upstream                                  # 把上游改名
git remote add origin git@github.com:<你的账号>/<你的仓库>.git    # 设新 origin
git remote -v                                                      # 验证

# 6.3 staged + commit
git add .gitignore README.md      # 先单独验证这两个文件的 diff 干净
git diff --cached -- .gitignore README.md
git add -A
git status --porcelain | grep -E "(\.env$|secret|credential|\.cursor/docs/)" || echo "✓ 敏感文件已排除"
git commit -m "snapshot: local WIP before upstream sync

Includes: <主要新增/修改的模块清单>
Baseline: upstream <hash> (<version>)
Purpose: snapshot to GitHub to avoid being overwritten by upstream sync."

# 6.4 处理远端 Initial commit（fork 场景，远端已有 README）
git fetch origin
git merge origin/main --allow-unrelated-histories --no-edit \
  -m "merge: incorporate remote initial commit"
# 若 README.md 冲突 → 按 Step 5 选定的 A/B/C 处理
git checkout --ours README.md      # C 方案：本地已含合并版本
git add README.md
git commit --no-edit

# 6.5 ⚠️ 关键：如果是 shallow clone，必须先 unshallow（否则 push 失败）
ls .git/shallow 2>/dev/null && {
  echo "⚠️ 检测到 shallow clone，先从上游拉完整历史（不会改工作区）"
  git fetch --unshallow upstream    # 1-10 分钟，看上游仓库大小
}

# 6.6 push
git push -u origin main
```

---

### Step 7: 修正 commit 身份（可选）

如果发现 committer name 是 `<user>` 这种系统默认值（不是你想要的显示名）：

```bash
# 只为本仓库设置（不影响其他项目）
git config user.name "<你想显示的名字>"
```

**注意**：已 push 的 commit 不会自动改身份，强行 amend 会触发 force push 重写历史，建议**只对未来的 commit 生效**。

---

### Step 8: 收尾 — 分支清理 + tag/release（push 完成后）

push 完成后通常会留下临时的同步/特性分支（如 `sync/v0.12.0`、`wip-xxx`、`feature/xxx`），合到 `main` 后建议清理；版本节点建议打 tag 让 GitHub 的 Tags / Releases 页有迹可循。

#### 8.1 分支清理（已合并的临时分支）

```bash
# 1. 看哪些分支已经合到 main（候选删除目标）
git branch --merged main | grep -v "^\*\| main$"

# 2. 删本地分支（用 -d 不用 -D，未合并会报错保护你）
git branch -d sync/v0.12.0

# 3. 删远端分支
git push origin --delete sync/v0.12.0
# 等价：git push origin :sync/v0.12.0

# 4. 验证只剩需要保留的分支
git branch -vv
git ls-remote --heads origin
```

**注意点**：
- ✅ 用 `-d`（safe）不用 `-D`（force）：未合并的分支会报错保护你免于丢工作
- ⚠️ 删除时若 git 报 `error: could not write config file .git/config: Operation not permitted` —— sandbox 限制，分支已成功删除，不影响结果
- 残留的 tracking 配置可手动清：`git config --unset branch.<name>.remote 2>/dev/null; git config --unset branch.<name>.merge 2>/dev/null`
- **不要删 main / master / 默认分支** —— GitHub 默认分支删了之后页面会变成空仓库样子，要在 Settings 里改默认分支才能恢复

#### 8.2 打 tag（标记版本节点）

| Tag 类型 | 命令 | 何时用 |
|---|---|---|
| **轻量 tag** | `git tag <name>` | 个人临时标记，无 message |
| **annotated tag**（推荐） | `git tag -a <name> -m "..."` | 正式发布，带作者 / 时间 / 说明 |

```bash
# 例：给当前 main HEAD 打 v0.12.0 annotated tag
git tag -a v0.12.0 -m "Sync to upstream NousResearch/hermes-agent v0.12.0 + fork docs"

# 推单个 tag 到 origin
git push origin v0.12.0

# 推所有本地 tag（小心：会把所有本地 tag 一次性推上去，包括 fetch upstream 时拉来的上游 tag）
# 不推荐除非确定要这么做
# git push origin --tags
```

**tag 指向哪个 commit 的 3 个常见决策**：

| 选项 | 指向 | 含义 | 适用 |
|---|---|---|---|
| **A. main HEAD**（最常用） | `git tag v<X.Y.Z>` 默认指向 HEAD | "fork 升级到 v<X.Y.Z> 的最终状态（含本地 docs/特性）" | 90% 的场景 |
| B. merge commit | `git tag v<X.Y.Z>-merge <merge-sha>` | "纯粹合上游 v<X.Y.Z> 的瞬间，不含 docs" | docs 想单独发 |
| C. 上游原始 release commit | `git tag v<X.Y.Z>-upstream <upstream-sha>` | "完全等同于上游官方 tag" | 想保留上游精确发布点 |

> **fork 仓库的 v<X.Y.Z> tag 与上游 v<X.Y.Z> tag 不会自动同步冲突** —— tag 不会跨 remote 自动 fetch（除非显式 `git fetch upstream --tags`），所以同名 tag 可放心用。

#### 8.3 创建 GitHub Release（可选 — Releases 页面正式发布记录）

> Release = Tag + 版本说明 + 可选附件。仅在 tag 已 push 后才能创建。

需要先装 [`gh` CLI](https://cli.github.com/) 并登录：

```bash
gh auth login                              # 首次配置（按提示选 SSH/HTTPS + 浏览器登录）
gh auth status                             # 验证登录态
```

创建 release 的 3 种方式（任选一）：

```bash
# 方式 A：用现有的升级笔记作 release notes（推荐 — 笔记内容已结构化）
gh release create v0.12.0 \
    --title "v0.12.0 — sync upstream + fork docs" \
    --notes-file .cursor/prompts/v0.12.0_upgrade_notes.md

# 方式 B：手写简短 notes
gh release create v0.12.0 \
    --title "v0.12.0" \
    --notes "Sync to upstream NousResearch/hermes-agent v0.12.0; conflicts resolved (11), V3 deepseek-chat regression guard added."

# 方式 C：让 GitHub 自动从 commit 列表生成 notes
gh release create v0.12.0 --generate-notes
```

或者完全在 GitHub 网页操作：
- 打开 https://github.com/<owner>/<repo>/releases/new
- 选择已 push 的 tag → 填写标题 + 内容 → Publish release

#### 8.4 删除已发布的 tag / release（误操作回滚）

```bash
# 1. 删本地 tag
git tag -d v0.12.0

# 2. 删远端 tag
git push origin --delete v0.12.0
# 等价：git push origin :refs/tags/v0.12.0

# 3. 删 GitHub Release（默认会保留 tag，加 --cleanup-tag 一并删 tag）
gh release delete v0.12.0 --yes --cleanup-tag
```

> **不可逆警告**：release 删了不可恢复（GitHub 不存历史快照）；tag 删了本地可重打，但远端如果有人已经 clone/fetch 过这个 tag，他们本地仍存在，会造成版本号混乱。**正式发出的 tag 不要删，错了就发新 tag（如 v0.12.1）替换**。

---

## 安全检查清单（push 前必过）

**push 前**：
- [ ] `git check-ignore .env` 显示被忽略 ✓
- [ ] `git diff --cached | grep -E "sk-[A-Za-z0-9]{20,}|sk-ant-|ghp_|AIza"` **无任何匹配** ✓
- [ ] `git status --porcelain | grep -E "\.cursor/docs/|WORK_IN_PROGRESS"` 无输出 ✓
- [ ] `.gitignore` 已包含本地隐私规则 ✓
- [ ] README.md 顶部已加 fork 定位说明（如适用）✓
- [ ] 远端有 `upstream` 和 `origin` 两个，分别指向上游和你的 fork ✓
- [ ] `.git/shallow` 不存在（或已经 `git fetch --unshallow upstream`）✓
- [ ] commit message 写明 baseline + 改动主题 ✓

**push 后收尾（Step 8）**：
- [ ] 已合并的临时同步分支已删（本地 `git branch -d` + 远端 `git push origin --delete`）✓
- [ ] 版本节点 tag 已打 + 推送到 origin（如需）✓
- [ ] GitHub Release 已创建（如需正式发布记录）✓
- [ ] `git ls-remote --heads origin` 只显示需要保留的分支 ✓

---

## 常见错误 → 应急处理

| 错误信息 | 根因 | 解法 |
|---|---|---|
| `did not receive expected object <hash>` `remote unpack failed` | shallow clone 缺少 object | `git fetch --unshallow upstream` 后重试 push |
| `! [rejected] main -> main (fetch first)` | 远端有你本地没有的 commit（如刚才的 Initial commit）| `git fetch origin && git merge origin/main --allow-unrelated-histories` |
| `Updates were rejected because the tip of your current branch is behind` | 同上 | 同上 |
| `error: src refspec main does not match any` | 本地不在 main 分支 / 没 commit | `git switch -c main` + `git commit` |
| `Permission denied (publickey)` | SSH key 没配到 GitHub | `gh auth login` 或 `ssh-keygen + 加到 GitHub` |
| `Could not access submodule '<name>' at commit <hash>` (warning) | submodule 仓库无访问权限 | 警告无害，可忽略；不影响 push |
| `error: The branch '<x>' is not fully merged` （删分支时）| 分支还没合并到 main，`-d` 拒绝删 | 先 `git merge` 或确认能丢，再 `git branch -D <x>`（force） |
| `error: could not write config file .git/config: Operation not permitted` （删分支/push 时）| sandbox 限制写 .git/config | 不影响实际删除/push 结果，残留 tracking 配置可手动 unset |
| `! [rejected] v<X.Y.Z> -> v<X.Y.Z> (already exists)` | 远端已有同名 tag | 决定是 force（`git push origin v<X.Y.Z> --force`，**慎用**）还是用新版本号 |
| `gh: command not found` | 未安装 GitHub CLI | `brew install gh`（macOS）或 https://cli.github.com/ ；或在 GitHub 网页手动建 release |
---

## 以后同步上游新版本（标准动作）

> **重大版本同步走 `/sp_sync_upstream_release`**：如果上游打了正式 release tag（如 v0.12.0 / v0.13.0）且 fork 已经分叉很远（>5 个文件冲突），优先用 `.cursor/commands/sp_sync_upstream_release.md` —— 它有完整的 worktree 隔离、防 `hermes update` 抢资源、11 类冲突决策树、回归测试等防御。下面这一节只适合**本地与上游差异很小**（<3 文件冲突）的快速增量同步。

push 完成后，以后每次上游 hermes-agent 出新版本，按这套节奏：

```bash
# 1. 拉上游新版本到本地（只下载，不动代码）
git fetch upstream

# 2. 看上游有什么新 commit
git log --oneline main..upstream/main | head -20

# 3. 选一种 merge 策略：
git merge upstream/main           # A. 标准 merge（保留分叉历史）
# 或
git rebase upstream/main          # B. rebase（线性历史，会改 commit hash）

# 4. 处理冲突（如有）：
#    - 用户改过的文件 vs 上游也改过 → 手动 merge
#    - 只有用户改过 → 保留用户的（自动）
#    - 只有上游改过 → 接受上游的（自动）

# 5. 测试 + push
scripts/run_tests.sh
git push origin main

# 6. （可选）按 Step 8 收尾：清理临时分支 + 打 tag
#    git branch -d <临时分支> && git push origin --delete <临时分支>
#    git tag -a v<X.Y.Z> -m "..." && git push origin v<X.Y.Z>
```

> **冲突处理铁律**：先 `git diff --check` 看冲突边界，再决定 ours/theirs/手动 merge；**不确定时先 `git merge --abort` 回退**，问清楚再来。

---

## 一图看清：本地 vs 上游 vs 你的 fork

```
┌──────────────────────────────────┐
│  upstream (NousResearch/...)     │  ← 只 fetch，不 push
│  ┌──────────────────────────┐    │
│  │ main                     │    │
│  │ ●──●──●──●──● bf196a3    │    │
│  └─────────────────────┬────┘    │
└────────────────────────┼─────────┘
                         │ git fetch upstream
                         ▼
              ┌─────────────────┐
              │  本地 main      │
              │ ●──●──●──●──●──●─●  ← 你的 wip commit
              │              ↑     │
              │       上游基线 bf196a3
              └─────┬───────────┘
                    │ git push origin main
                    ▼
┌──────────────────────────────────┐
│  origin (你的 fork)              │  ← push 目标，备份
└──────────────────────────────────┘
```

---

## 参考

- 本流程的真实执行记录：见 commit `cf13a89` (snapshot: local WIP before upstream v0.11.0+ sync)
- `.cursor/commands/README.md` — Cursor commands 三件套总览（设计 → 计划 → 执行）
- `AGENTS.md` — hermes-agent 工程结构 / 硬规则（动核心代码前必读）
