# /sp_sync_upstream_release — 安全把上游新发布版合并到本地 fork

> **基于一次真实合并的经验沉淀**：2026-05-07 把 NousResearch/hermes-agent **v0.12.0** 合并到本地 fork
> 共 11 个冲突，全程在保留本地改动的前提下完成；过程中遭遇 **`hermes update` 自动 stash 导致工作丢失**事故并补救成功。
>
> 适用场景：本地 fork 长期维护的工程（hermes-agent-main / xxx-agent / 其他长 fork），上游打了新版本 tag 想拉下来同步，但本地有大量自定义改动不能丢。

---

## 使用方式

```
/sp_sync_upstream_release                                              # 走默认流程（拉最新 main）
/sp_sync_upstream_release 把 NousResearch/hermes-agent v0.12.0 合到本地  # 指定 tag
/sp_sync_upstream_release 同步上游 main 到 sync/<branch>                 # 指定目标分支
```

## 何时使用

- 维护一个长期 fork，上游每隔几周/几个月发布新版本（v0.10 → v0.11 → v0.12 ...）
- 本地代码已严重分叉（>= 5 个文件的本地特性），普通 `git pull` 必产生大量冲突
- 想保留**全部本地改动 + 全部上游新代码**，按"上游为基线、本地适配"的策略融合
- 担心合并到一半被某个自动化工具（hermes update / IDE 后台同步 / cron）打断

> **不适用**：本地与上游差异极小（<3 个文件） → 直接 `git pull` 解几个冲突即可，不必走本流程。

---

## 核心铁律（**违反必踩坑**）

| 铁律 | 说明 |
|---|---|
| **隔离工作区**：用 `git worktree` 单独开一个目录做合并 | 避免主仓库的 `hermes update` / IDE 后台触碰；worktree 完成后只把 commit 推回 |
| **关掉所有 auto-update**：`HERMES_DISABLE_AUTO_UPDATE=1` + 杀掉 hermes 守护进程 | 2026-05-07 事故根因：`_stash_local_changes_if_needed` 在合并中调用 `git reset` 清空 index |
| **小步快跑 + 立刻 commit**：每解 3-5 个冲突就 `git commit --no-verify -m "wip:..."` | commit 之后 `git reset` 才不会再丢工作树（reset 走 HEAD-stable 路径） |
| **冲突解决前先做 dry-run merge**：`git merge --no-commit <ref>`，确认冲突清单后立即 `git merge --abort` | 不要直接进入解决态——先看清全貌、列决策、再上手 |
| **每个冲突都要列"差异 + 影响 + 决策"三栏**：让用户拍板，不要自作主张 | 11 个冲突里有 6 个属于"两者都保"，3 个"取本地"，2 个"取上游"，没有通用答案 |
| **本地隐私不要随上游漂出去**：硬编码路径、个人 venv 等必须脱敏 | 测试文件里的 `~/PyCharmMiscProject/...venv/bin/python` 漂入上游会暴露本机结构 |
| **不要拒绝写 change-detector 测试的本地版**：上游加的「列出 27 个工具名」类测试脆弱 | 本地 anchor 模式（只断言关键工具子集）符合 `AGENTS.md` 反 change-detector 准则 |

---

## 执行流程（10 步）

### Step 0：环境隔离（**事故防御**）

```bash
# 1. 关掉 hermes 后台守护（防止 hermes update 在合并中触发）
pkill -f "hermes.*--gateway" 2>/dev/null
pkill -f "hermes.*dashboard" 2>/dev/null
export HERMES_DISABLE_AUTO_UPDATE=1

# 2. 推荐：用 worktree 隔离合并环境
cd /path/to/main/repo
git worktree add ../sync-v0.12.0-worktree main
cd ../sync-v0.12.0-worktree
# 在这里完成全部合并 → 完成后 git push 回原 origin → 主仓库 git fetch + merge --ff-only

# 3. 标记合并状态（让其他自动化工具能识别并退避）
touch .git/MERGE_IN_PROGRESS
```

> **事故还原（必读）**：上次合并时，主仓库的 `hermes update` 守护进程检测到工作树 dirty + 当前不在 main 分支，
> 自动调用 `_stash_local_changes_if_needed`（`hermes_cli/main.py:5667`）→ 内部执行裸 `git reset`（默认 `--mixed`）
> 清掉了所有 conflict 解决进度。reflog 文案 `reset: moving to HEAD` 是其唯一指纹。

---

### Step 1：现状诊断

```bash
git status                                  # 工作区必须干净
git remote -v                               # 应该有 origin（自己 fork）+ upstream（NousResearch/hermes-agent）
git branch --show-current                   # 记录起点分支（一般是 main）
git log --oneline -5                        # 记录最新 commit hash（万一回滚要用）
git tag --list 'v*' | tail -5               # 看本地有哪些 upstream tag
```

如果没配 `upstream` remote：

```bash
git remote add upstream git@github.com:NousResearch/hermes-agent.git
git config remote.upstream.fetch '+refs/heads/*:refs/remotes/upstream/*'
git config --add remote.upstream.fetch '+refs/tags/*:refs/tags/*'
git fetch upstream --tags
```

> **如果 `git config` 报 `Operation not permitted`**：在 sandbox 模式下需要 `required_permissions: ["all"]` 写 `.git/config`。

---

### Step 2：拉上游 + 定位目标 tag/commit

```bash
git fetch upstream --tags
git log --oneline upstream/main -5              # 上游最新提交
git show v0.12.0 --stat | head -20              # 确认目标 tag 存在
git rev-parse v0.12.0                           # 拿到目标 commit
git merge-base HEAD v0.12.0                     # 看分叉点
```

**统计差异规模**：

```bash
git diff --stat HEAD..v0.12.0 | tail -3         # 文件数 / 总变更行数
git rev-list --count HEAD..v0.12.0              # 上游领先的 commit 数
git rev-list --count v0.12.0..HEAD              # 本地领先的 commit 数（仅本地 fork 的改动量）
```

---

### Step 3：开新分支 + 干跑合并（dry-run）

```bash
git checkout -b sync/v0.12.0
git merge --no-commit --no-ff v0.12.0           # 干跑：让 git 把可自动合并的标好，列出冲突
git diff --name-only --diff-filter=U            # 列出所有真冲突文件
git status | grep -E "(both modified|deleted by|added by)"
git merge --abort                               # 立刻 abort，进入下一步分析
```

记录冲突文件清单到笔记里（10 个左右是常见量），后续逐一处理。

---

### Step 4：列冲突决策表（**必须找用户拍板**）

对每个冲突文件做：

```bash
# 看冲突区域
git diff HEAD..v0.12.0 -- <file>

# 看本地版本（HEAD 这边的改动是从分叉点起走了多远）
git log --oneline $(git merge-base HEAD v0.12.0)..HEAD -- <file>

# 看上游这边的改动
git log --oneline $(git merge-base HEAD v0.12.0)..v0.12.0 -- <file>
```

**给用户的决策表必须包含 4 列**：

| 文件 | 冲突点（这两个改动具体差啥） | 影响（本地/上游各自加了啥能力） | 推荐方案 |
|---|---|---|---|

**4 类常见决策模式**（占 90%+）：

| 模式 | 何时选 | 怎么做 |
|---|---|---|
| **两者都保** | 改动相互独立（如 cli.py 命令分发：本地加了 `/rules /memory /learn`，上游加了 `/busy`） | 手工删冲突标记，把两段合并 |
| **取本地** (`git checkout --ours`) | 上游改动会回退本地特性（如 AGENTS.md 上游改回长版，本地是精简的 load-bearing 版） | 取本地 + 把上游有价值的部分手工搬到 `docs/agents/*.md` |
| **取上游** (`git checkout --theirs`) | 上游修了关键 bug，本地版只是过期注释（如 plugins.py 上游加了 `repo_plugins` 关键变量） | 直接取上游 |
| **融合**（最难） | 上游做了大重构 + 本地有依赖该重构的本地特性 | 取上游骨架 → 把本地特性方法/分支条件单独 patch 上去 |

> 决策时**一定要 quote 上游的 PR 号 / commit hash**，方便日后理解。

---

### Step 5：开始解冲突（**每解 3-5 个就 commit 一次**）

实际进入合并：

```bash
git merge --no-commit --no-ff v0.12.0
```

**逐个解决**：
- 简单冲突：用编辑器手工删 `<<<<<<<` `=======` `>>>>>>>` 标记
- 复杂冲突（如 `run_agent.py` 大文件 6 处冲突）：
  - 先用 `grep -nE "^<<<<<<< |^=======$|^>>>>>>> "` 列出所有冲突行号
  - 一处一处处理，每处都用 `Read`/`StrReplace` 工具
  - 处理完一处立刻验证：`grep -nE "^<<<<<<< " run_agent.py` 应该越来越少

**关键防御措施**：

```bash
# 每解几个冲突，立刻 add + 中间 commit（防止意外丢失）
git add <已解决的文件>
git commit --no-verify -m "wip: resolve conflicts in <area> (mid-merge)" || \
  git commit -m "wip: resolve conflicts in <area> (mid-merge)"
# 注：mid-merge 状态下，git 会自动把这个 commit 当成 merge 的中间产物
# 实际上会创建普通 commit + 保留 MERGE_HEAD，最终用 git commit 完成 merge
```

> **遇到任何 `reset: moving to HEAD` 时立刻停手 + 跑 `git stash list`**：很可能是 hermes update / IDE 自动 stash 抢了进度。

---

### Step 6：python 语法兜底 + 关键路径验证

```bash
# 所有改过的 .py 文件都要过 ast.parse
for f in $(git diff --name-only HEAD~1 | grep '\.py$'); do
    python3 -c "import ast; ast.parse(open('$f').read())" 2>&1 | head -1
done

# grep 兜底所有冲突标记
grep -rnE "^<<<<<<< |^=======$|^>>>>>>> " --include="*.py" --include="*.md" .

# 关键功能 anchor 检查（按本次合并经验）
grep -c "def flush_memories\|def _compact_with_progress" run_agent.py    # 本地核心方法是否还在
grep -c "_needs_thinking_reasoning_pad\|_needs_kimi_tool_reasoning" run_agent.py  # 上游重构是否进来
grep -c "Local override.*regression guard" run_agent.py                  # 本次新增的回归防御注释
```

---

### Step 7：最终 commit（**冲突全部 resolved 后立即提交**）

```bash
# 1. 确认无 conflict marker
grep -rnE "^<<<<<<< |^=======$|^>>>>>>> " --include="*.py" --include="*.md" . | grep -v ".git/"

# 2. 确认所有冲突文件都 staged
git diff --name-only --diff-filter=U  # 应该输出空

# 3. 立刻 commit（避免再被 reset 抢走）
git commit -m "merge: sync upstream NousResearch/hermes-agent v0.12.0

Conflicts resolved (11 total):
  - .gitignore                         : 两者都保
  - AGENTS.md                          : 取本地 + 上游内容搬到 docs/agents/
  - agent/context_engine.py            : 融合 docstring
  - cli.py                             : 两段都保（命令分发 + handler）
  - hermes_cli/plugins.py              : 取上游
  - run_agent.py                       : 6 处分别处理
  - tests/cli/test_cli_approval_ui.py  : 两测试都保 + 路径脱敏
  - tests/tools/test_registry.py       : 取本地 anchor 模式
  - tools/delegate_tool.py             : 融合 initializer + 全集传播
  - tools/skills_tool.py               : 融合 preprocess/session_id + record_view
  - tools/terminal_tool.py             : 两套函数都保

Post-merge fix:
  - run_agent.py::_needs_deepseek_tool_reasoning 加入 V3 chat 排除分支
"

# 4. 立刻验证 merge 完整性
git log -1 --format='%H %P'        # 必须有 2 个 parent（说明是 merge commit）
git log --merges --oneline -1
```

---

### Step 8：测试与回归验证

```bash
# 1. 快速点火（必跑）
bash scripts/run_tests.sh tests/run_agent/ -k "smoke or critical or reasoning_content"

# 2. 模块化白盒
bash scripts/run_tests.sh tests/cli/test_cli_approval_ui.py
bash scripts/run_tests.sh tests/tools/test_registry.py
bash scripts/run_tests.sh tests/tools/test_skills_tool.py
bash scripts/run_tests.sh tests/run_agent/test_reasoning_content_replay.py

# 3. 全量（如果时间允许）
bash scripts/run_tests.sh
```

**已知非阻塞失败**（合并产生的 false-positive 不计入回归）：
- macOS 上的 `tests/tools/test_file_*.py` 因上游"sensitive system path"误判 `/var/folders/...` 失败
  - 这是上游 fix 311dac197 / 560245879 的 macOS 特定 bug，**不属于本次合并引入**
  - 验证方法：`git stash` 这次合并 → 再跑测试 → 同样失败，说明早就坏的

---

### Step 9：推送回上游分支 + 合到 main

```bash
# 1. 先 push 同步分支
git push -u origin sync/v0.12.0

# 2. 切回 main 做 fast-forward 合并（前提：main 没在合并期间被推过）
git checkout main
git pull --ff-only origin main          # 确认 main 没漂走
git merge --ff-only sync/v0.12.0        # 把 sync/v0.12.0 merge 进来（fast-forward）
git push origin main

# 3. 清理同步分支（可选）
git branch -d sync/v0.12.0
git push origin --delete sync/v0.12.0
```

> 如果用了 worktree，最后还要 `git worktree remove ../sync-v0.12.0-worktree`。

---

### Step 10：清理 + 写笔记

```bash
# 1. 清理标记文件
rm -f .git/MERGE_IN_PROGRESS

# 2. 把这次的决策 + 注意事项落到 .cursor/prompts/ 下
# 见 .cursor/prompts/v<version>_upgrade_notes.md

# 3. 重启 hermes 守护进程（如果之前杀了的话）
unset HERMES_DISABLE_AUTO_UPDATE
```

---

## 故障恢复手册

### Case A：合并到一半发现 `git status` 显示干净，HEAD 也没有 merge 状态

**诊断**：

```bash
git reflog --date=iso | head -10               # 看最近 reflog
git stash list                                 # 看是否被自动 stash 走了
ls .git/MERGE_HEAD 2>/dev/null && echo "still in merge" || echo "merge state lost"
```

**指纹**：reflog 里出现 `reset: moving to HEAD` → 99% 是 `hermes update` 抢了

**恢复步骤**：

```bash
# 1. 如果 stash 里有 hermes-update-autostash-*，先恢复
git stash list | grep hermes-update-autostash
git stash pop "stash@{0}"   # 或对应的 stash ref

# 2. 如果 stash 里没有，从 reflog 找之前的状态
git reflog | head -20       # 找到 commit/checkout 那一行的 hash
git reset --hard <hash>     # 谨慎，先确认 hash 没错

# 3. 重新走 Step 4-7（这次先 export HERMES_DISABLE_AUTO_UPDATE=1 + touch .git/MERGE_IN_PROGRESS）
```

---

### Case B：commit 完了发现某个文件冲突解错了

```bash
# 查看上游本来想加什么
git show v0.12.0 -- <file>

# 查看本地原本的版本
git show HEAD~1 -- <file>            # HEAD~1 是 merge 之前的版本

# 单文件回滚到上游版（不影响其他文件）
git checkout v0.12.0 -- <file>
git commit --amend --no-edit          # 仅当此 commit 没 push 时才能 amend
```

---

### Case C：测试失败且不确定是合并引入还是上游 bug

```bash
# 1. 先确认是否上游本来就有这个 bug
git stash                              # 临时撤销当前合并
bash scripts/run_tests.sh <failing_test>
# 如果还是失败 → 上游本来就有的 bug，不属于本次合并的回归

# 2. 恢复合并
git stash pop
```

---

## 检查清单（合并完成前过一遍）

- [ ] `grep -rnE "^<<<<<<< |^=======$|^>>>>>>> "` 输出为空
- [ ] 所有改过的 `.py` 文件都过了 `ast.parse`
- [ ] `git log -1 --format='%H %P'` 显示有 2 个 parent
- [ ] 关键本地特性 anchor 检查全过（按 Step 6 列表）
- [ ] 关键上游新功能 anchor 检查全过
- [ ] 测试通过（或失败均为上游已有 bug，已 stash 验证过）
- [ ] 没有把硬编码本地路径泄露到上游
- [ ] `.cursor/prompts/v<version>_upgrade_notes.md` 已写
- [ ] 已 push origin sync/v<version>
- [ ] 已 ff-only 合到 main + push origin main

---

## 与 `/sp_sync_github` 的区别

| 项 | `/sp_sync_github`（同步到 GitHub） | `/sp_sync_upstream_release`（合上游版本） |
|---|---|---|
| **方向** | 本地 → GitHub（push） | GitHub upstream → 本地（pull/merge） |
| **风险** | 密钥泄露、shallow clone push 失败 | 冲突解错、丢本地改动、被自动 stash 抢走进度 |
| **频率** | 不定期（攒一波改动） | 每次上游打 tag |
| **核心铁律** | push 前必扫密钥 | 合并前必关 hermes update |

两个 skill 互补，重大改动建议先 `/sp_sync_upstream_release`（上游 → 本地），稳定后再 `/sp_sync_github`（本地 → GitHub 备份）。
