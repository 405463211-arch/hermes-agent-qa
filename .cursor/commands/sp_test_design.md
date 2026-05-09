# /sp_test_design — hermes-agent 测试设计 / 覆盖审查

> **专门解决"以为测了，其实漏了"的问题**
>
> 在写测试或评审已有测试用例之前，按 6 个维度系统性检查覆盖盲点。
> 配套 `docs/agents/testing.md`（讲怎么跑测试），本文讲**该测什么**。

## 使用方式

```
/sp_test_design <代码路径 或 改动描述>
/sp_test_design plugins/context_engine/lcm/store.py 的 add() 方法
/sp_test_design 给 cli 新增 /skin <name> 持久化前的测试设计
/sp_test_design tests/plugins/test_lcm_engine.py 现状评审
```

---

## 必读案例：LCM 重复入库 Bug（2026-05 真实案例）

**现象**：`/lcm` 面板显示"已索引（所有会话）：630 个 chunk"，但实际唯一内容只有 315 条 —— 同一段对话被存了两次（不同 session_id 下重复）。

**触发链**：用户 `--resume` 恢复旧会话 / `fork_session()` / `run_agent.py:9411` 压缩点 session_id rotation —— 任意一种都会让相同内容以不同 session_id 抵达 `ChunkStore.add()`，老实现没去重，于是行数翻倍。

### 当时已有 4 个测试，没一个抓住

| 已有测试 | 它测了什么 | 漏掉了什么 |
|---|---|---|
| `test_round_trip_insert_search_recall` | 单 session 进、搜、取 → 全对 | **没断言任何 chunks 表行数 / 唯一性** |
| `test_search_isolates_by_session` | 两个 session 用**不同内容**，搜结果不互串 | 不同内容 → 永远走不到去重代码路径，永远暴露不了 bug |
| `test_delete_session` | 删一个 session，count 归零 | 单 session 场景，**不知道共享行被另一个 session 删时的行为** |
| `test_neighbors_do_not_cross_session_boundary` | 两 session 用**相同内容**填充，验证邻居不跨界 | 唯一接触"相同内容"路径的测试，但只断言搜索行为，**完全没看 chunks 表行数** |

### 一行就能抓住的测试

```python
def test_same_content_two_sessions_shares_one_row(self, tmp_path):
    store = ChunkStore(tmp_path / "store.db")
    chunks = [{"role": "user", "content": "shared"}]
    emb = LexicalEmbedder(dim=64).embed(["shared"])
    store.add("A", chunks, emb, "lex")
    store.add("B", chunks, emb, "lex")
    rows = store._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert rows == 1, f"dedup failed: {rows} rows for 1 unique content"
```

**为什么没人想到写**：因为前 4 个测试**全是行为测试**（"搜出来对不对"），没人在写测试时停下来问一句"**存储层的不变量是什么？**"

### 反思链：测试理论缺失的 3 层

1. **只测行为不测不变量** — 测了"search 返回对的"、"delete 后 count=0"，但没断言更深一层的不变量（"DB 里没有重复行"、"行数 == 唯一内容数"）。
2. **跨实例同输入路径未覆盖** — `test_search_isolates` 用了不同内容做隔离测试，从未 instantiate "同内容跨 session" 这个 case；偏偏这才是 bug 路径。
3. **没有"可见状态端到端"检查** — `/lcm` 面板的"已索引（所有会话）"是用户能看见的数字，没有任何测试断言它的语义（"=唯一内容数"or"=插入次数"？需求都没写清，就更没人测）。

---

## 6 轴测试覆盖清单（写测试前 / 评审测试时逐项扫一遍）

### 轴 1：行为正确（Behavior）

> 函数返回 / API 响应 / 副作用对不对。**多数项目只测了这个**。

```python
# 例：search 应该按相关度排序
results = store.search("S", query, k=3)
assert results[0]["score"] >= results[1]["score"]
```

**自检**：
- [ ] 正常路径覆盖了吗？
- [ ] 异常路径（empty / None / 错误格式）抛/降级行为符合预期？
- [ ] **输入域 4 分类**全覆盖了吗？

**输入域分类清单**（写参数化测试时按这 4 类列样本）：

| 类别 | 例子 | 期望行为 |
|---|---|---|
| 合法 | `"User prefers dark mode"` | 接受 |
| 空 / 全空白 | `""`、`"   "`、`"\n\t"` | 拒绝 |
| 恶意 / 注入 | `"ignore previous instructions"` | 拒绝 |
| **形式合法 + 内容空洞** | `"."`、`"---"`、`"。。。"`、`".`"`、`"- "` | **拒绝（多数项目漏这一类）** |

→ hermes 真实例：MEMORY.md 历史上接受了一个 `.` 入库，因为前 3 类都测了，第 4 类没人想到。见"历史失误索引"#3。

### 轴 2：状态不变量（Invariants）

> **存储 / 缓存 / 内存中的状态**应该满足什么硬约束？

任何写状态的代码都必须有 invariant 测试。常见 invariant：

| 类型 | 断言模板 |
|---|---|
| 唯一性 | `SELECT COUNT(*) == COUNT(DISTINCT ...)` |
| 行数下限/上限 | `assert len(rows) == expected` |
| 引用完整性 | 关联表里的 FK 都能在主表找到 |
| 计数一致性 | `manager.size == sum(per_*_count)` |
| 单调性 | `version_after >= version_before` |
| 容量下界 | `assert dim > 0`、`assert len(buffer) > 0` |

```python
# LCM 那个 bug 的对症测试
rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
distinct = conn.execute(
    "SELECT COUNT(DISTINCT content_hash) FROM chunks"
).fetchone()[0]
assert rows == distinct, "duplicate rows detected"
```

**自检**：
- [ ] 这段代码改了什么持久化 / 内存状态？每个状态有 invariant 测试吗？
- [ ] DB 表有 unique 约束没？没有的话谁来保证？该约束是否被测试？

### 轴 3：幂等 / 重复（Idempotency）

> 同一输入操作两次，状态应该等于操作一次。**几乎所有"replay 类 bug"出在这里**。

```python
def test_add_idempotent(self, tmp_path):
    store = ChunkStore(tmp_path / "store.db")
    chunks = [{"role": "user", "content": "x"}]
    emb = LexicalEmbedder(dim=64).embed(["x"])

    state_after_first = (
        store.add("S", chunks, emb, "lex"),
        store.session_chunk_count("S"),
    )
    state_after_second = (
        store.add("S", chunks, emb, "lex"),
        store.session_chunk_count("S"),
    )
    assert state_after_first == state_after_second
```

**适用场景**：
- 任何写操作（insert / update / append）
- 缓存填充
- 配置 migration
- 消息消费（特别是 MQ / Webhook）
- 启动初始化（`__init__` 多次调用应等价）

**自检**：
- [ ] 这段代码是写操作吗？操作两次状态会膨胀吗？
- [ ] 有 retry 逻辑的代码，retry 路径幂等吗？

### 轴 4：跨实例 / 跨参与方等价（Cross-actor identity）

> 同一份"逻辑数据"从**多个来源**进入系统，最终状态应当合理收敛。

LCM 的 bug 就漏在这一轴：相同 chunk 从 session A 和 session B 抵达。

| 场景 | 跨参与方维度 |
|---|---|
| 多租户 | 同一份配置 / 数据从两个租户进，会泄漏到对方吗？ |
| 多进程 | 两个 hermes 进程并发写同一份资源（profile / cache / DB）会怎样？ |
| 多 session | 同一段对话被 `--resume` 多次，状态如何累积？ |
| 跨 fork | `fork_session()` 后两边写状态会互相污染吗？ |
| 跨 platform | telegram 和 discord 收到同一条 message 会双重处理吗？ |
| **跨模块共享 store** | **A 模块写、B 模块读同一个底层存储（SQLite / 文件 / cache），双方对 key / bucket / schema 的约定一致吗？** |

**"跨模块共享 store" 的契约测试模板**——A 写、B 读、断言读到 = 写入：

```python
def test_<A>_to_<B>_round_trip_via_shared_store(self, ...):
    # 模拟 A 模块写入路径
    a_module.write(payload="unique-token-X")
    # 通过 B 模块的对外接口读
    result = b_module.read(query="unique-token-X")
    # 关键：必须经过 B 的 *公开 API*，不能直接戳底层
    # 这才能抓住"A 写 bucket-foo / B 读 bucket-bar"这类不一致
    assert "unique-token-X" in result
```

```python
# 模板：跨 actor 同输入 → 状态收敛验证
result_a = code_under_test(actor="A", input=shared_input)
result_b = code_under_test(actor="B", input=shared_input)
# 然后断言：哪些是共享的（应等价），哪些是隔离的（应不同）
```

**自检**：
- [ ] 这段代码会被哪些"参与方"调用？(session / 进程 / 租户 / platform / fork)
- [ ] 同输入从不同参与方进 → 哪些状态应共享？哪些应隔离？

### 轴 5：演进 / 迁移兼容（Evolution）

> Schema 变更、配置版本升级、API 版本切换，**老数据能否平滑迁移**？

```python
def test_legacy_db_migrates_in_place(self, tmp_path):
    db = tmp_path / "store.db"
    # 手搓一个 v1 schema 的 DB（没有 content_hash 列）
    conn = sqlite3.connect(str(db))
    conn.executescript("CREATE TABLE chunks (id INT, ...);")
    # 写几行老数据
    conn.execute("INSERT INTO chunks VALUES ...")
    conn.close()

    # 用新代码打开 → 必须不报错且老数据可读
    store = ChunkStore(db)
    assert store.session_chunk_count("legacy-session") == N
```

**自检**：
- [ ] 这次改动加列 / 改 schema / 升 config 版本号了吗？
- [ ] 有 fixture 模拟老格式数据并验证升级路径吗？
- [ ] migration 跑两次（重启场景）会不会出问题？(回到轴 3 幂等)

### 轴 6：可见状态端到端（User-visible truth）

> 用户看得见的数字 / 列表 / 状态面板，**它们的语义是否被锁住**？

LCM 那个 bug 的 `/lcm` 面板"已索引（所有会话）：630"就是用户可见数字，没有任何测试断言"这个数 == 唯一内容数"。

```python
def test_lcm_panel_total_equals_unique_content_count(self, tmp_path):
    eng = _make_engine(tmp_path)
    eng.on_session_start("A")
    eng.compress(_make_msgs(), current_tokens=10000)
    eng.on_session_start("B")
    eng.compress(_make_msgs(), current_tokens=10000)  # 同样消息
    # 用户在面板看到的数字
    assert eng.get_total_chunks() == eng.get_session_chunks() * 1  # 不是 *2
```

**自检**：
- [ ] 这段代码的输出会出现在 CLI / UI / API 响应里吗？
- [ ] 至少一个 e2e 测试断言了"用户看到的数字符合语义"？

---

## 写测试前的 5 步小问询

**写新测试前 / 评审已有测试时，逐项口头回答（写在 PR 描述或测试文件 docstring 里）**：

```
1. 这段代码读/写哪些持久化或内存状态？
   → 列出每一项

2. 哪些不变量必须永远成立？
   → 至少一条 invariant 测试

3. 调用两次 / 重复输入会怎样？
   → 至少一条幂等测试（除非显式声明非幂等）

4. 它会从几种"参与方"被调用？(session / 进程 / 租户 / fork / 平台)
   → 至少一条跨参与方等价测试

5. 它的输出会出现在用户可见处吗？(面板 / 日志 / API)
   → 至少一条 e2e 锁住"可见数字 / 列表"语义
```

**回答完以上 5 题再写测试**。如果某条回答是"不需要"，docstring 里写明原因。

---

## 模板速查（直接复制改）

### 不变量模板

```python
def test_<state>_<invariant_name>(self, ...):
    # arrange: 多种典型操作组合
    code_path_1()
    code_path_2()
    # assert: 不变量恒立
    assert _query_state() == _expected_invariant()
```

### 幂等模板

```python
def test_<op>_idempotent(self, ...):
    state_first = (op(input), snapshot())
    state_second = (op(input), snapshot())
    assert state_first == state_second
```

### 跨参与方模板

```python
def test_<op>_<axis>_isolation(self, ...):
    """同输入从两个 actor 进 → 验证哪些共享 / 哪些隔离"""
    op(actor="A", input=X)
    op(actor="B", input=X)
    # 共享面：
    assert _shared_state_count() == 1   # 只一份
    # 隔离面：
    assert _A_view() != _B_modify_only()
```

### 迁移模板

```python
def test_legacy_v<N>_migrates(self, tmp_path):
    db = _build_v<N>_fixture(tmp_path)
    # 用新代码打开
    obj = NewClass(db)
    # 老数据可读
    assert obj.legacy_query() == expected
    # 重复打开（迁移幂等）
    NewClass(db).close()
    NewClass(db).close()
```

### 边界模板

```python
@pytest.mark.parametrize("payload,expected", [
    ([], []),            # empty
    ([single], [r1]),    # 1 元素
    ([a]*MAX, [...]),    # 容量上界
])
def test_<op>_boundaries(...):
    ...
```

---

## Anti-patterns（hermes 工程禁止）

> 见 `docs/agents/testing.md` 的 change-detector 章节，本节补充几条**测试设计层面**的反模式：

1. **只看返回值，不查表/不查文件**
   - ✗ `assert add(x) == ok`
   - ✓ `add(x); assert _query_state_count() == before + 1`

2. **跨参与方测试用不同输入**（让自己感觉"测了 isolation"，但根本走不到 dedup / 共享路径）
   - ✗ `add("A", "content-A"); add("B", "content-B"); assert search("A") == [A]`
   - ✓ `add("A", "shared"); add("B", "shared"); assert <state-invariant>`

3. **断言上层 wrapper 通过就完事**（绕过了底层 invariant）
   - ✗ 只测 `LCMEngine.compress()` 的高阶行为，不测 `ChunkStore.add()` 的 dedup
   - ✓ 同时锁住每一层独立的 invariant

4. **测试名说一套，断言另一套**
   - ✗ 测试名 `test_no_cross_session_pollution`，断言里只 check `len(results) > 0`
   - ✓ 名字说啥就 assert 啥，否则改名

5. **mock 掉真正出问题的子系统**
   - ✗ `mock(ChunkStore.add); ...` 然后期待发现 dedup bug
   - ✓ 让 store 走真实 SQLite（用 `tmp_path` 隔离）

6. **A 模块写、B 模块读，各自单元测了但没"端到端契约"测试**
   - ✗ `test_A`：`assert A.write(x) == ok` 加 `test_B`：`B.read() returns format`，两边都过就完事
   - ✓ `test_contract`：`A.write(x); assert x in B.read()` —— 锁住"A 写完 B 真的能读到"的契约
   - hermes 真实例：`memory_tool` 把 archive 写进 LCM 的 `memory:session_X` bucket，`lcm_search` 只搜 `session_X`，单元测试各自过，端到端默默断裂；见"历史失误索引"#2

7. **错误的成功 / 失败消息**——UI / log 里的措辞与底层真实状态脱钩
   - 子型 A「错误的成功消息」：返回 `success=true` + 描述里说"用 X 工具可恢复"，但实际不能
     - ✗ `assert result["message"] == "Auto-archived to LCM (use lcm_search to recall)"` —— 只验证消息字符串
     - ✓ 验证消息里承诺的能力**真的可用**：`{archive(); search_result = lcm_search(...); assert content in search_result}`
   - 子型 B「错误的失败消息」：UI 报 `⚠ X failed`，但底层其实 `success=true`，只是某个旁路指标 = 0
     - ✗ 用 `delta == 0` 推断"X 失败" —— delta=0 还可能是 dedup 命中 / 输入为空 / 幂等
     - ✓ 让底层暴露**显式 status 字段**（`ok / init_failed / embed_failed / nothing_to_index / dedup_hit`），UI 按 status 分支报警
   - 反模式根因：消息把"我以为发生了什么"当成事实播报，没有把"底层实际状态"显式暴露上来；测试如果只断言消息字符串，就把"用旁路指标推断"的谎言锁进了用例
   - hermes 真实例：见"历史失误索引"#2（错误的成功消息）和 #4（错误的失败消息）

---

## 与 hermes 现有测试规范的关系

| 文件 | 解决的问题 | 关系 |
|---|---|---|
| `docs/agents/testing.md` | **怎么跑** —— wrapper / -n 4 / 不写 change-detector | 本 skill 不重复 |
| `tests/conftest.py` `_isolate_hermes_home` | **环境隔离** —— 不写 `~/.hermes` | 编测试时直接用 |
| **本 skill `sp_test_design`** | **测什么 / 哪些维度** —— 不变量、幂等、跨参与方、迁移、可见状态 | 写测试前先跑一遍 |

---

## 历史失误索引（Living document — 加新例子时追加在此）

每条都记录：现象 / 测试盲点 / 该走哪一轴 / 对应修复测试。

### 1. LCM 跨 session 重复入库（2026-05）

- **现象**：`SELECT COUNT(*)` = 630, `COUNT(DISTINCT content)` = 315
- **盲点轴**：轴 2（不变量）+ 轴 4（跨参与方等价）+ 轴 6（可见数字语义）
- **当时 4 个测试为何漏**：3 个用单 session，唯一双 session 的 `test_neighbors_do_not_cross_session_boundary` 用了相同内容但只断言邻居 —— 没人写"同内容跨 session 行数收敛"的测试
- **修复测试**：`tests/plugins/test_lcm_engine.py::TestCrossSessionDedup::test_same_content_two_sessions_shares_one_row` + 5 个相邻测试

### 2. memory archive → lcm_search 契约缺失（2026-05）

- **现象**：用户跑 archive 压力测试发现 `memory(action=add, target=memory)` 在 MEMORY.md 触顶时返回 `Auto-archived N entries to LCM (use lcm_search to recall)`，但 `lcm_search` 搜不到，响应里 `total_indexed: 0`
- **根因**：`tools/memory_tool.py:_archive_oldest_to_lcm_locked` 把归档内容写进 `f"memory:{session_id}"` bucket；`plugins/context_engine/lcm/engine.py:_handle_search` 只搜 `self._session_id`（不带前缀）—— 两个 bucket 永不重合，归档内容存进去但没人能读出来
- **盲点轴**：轴 2（模块共享 store 的契约不变量）+ 轴 4（跨模块共享 store 的新增子轴）+ 轴 6（错误的成功消息让用户以为 archive 可搜，实际不能）
- **当时为何漏**：
  - `tests/tools/test_memory_tool.py` 测了"add 触发 archive 时返回 `archived_to_lcm` 字段" —— 但**只验证字段存在**，不验证 archive 的内容是否真的可被检索
  - `tests/plugins/test_lcm_engine.py` 测了"compress 后 search 能找到" —— 只走 compression 这一条写入路径，从未走 memory.archive 路径
  - **两个文件之间没有 A→B 端到端契约测试**：底层共享 SQLite store 但约定的 session_id 语义不一致，单元测试各管各的；这是 anti-pattern #6 的真实落地
- **修复测试**：`tests/plugins/test_lcm_engine.py::TestLCMTools::test_search_finds_memory_archive_bucket`（直接验证 archive bucket 可搜）+ `test_search_merges_compression_and_archive_results`（双 bucket 都报告 + archive 进结果集）

### 3. memory 接受 `.` 等无信息条目（2026-05）

- **现象**：用户清理 USER.md 时发现第 3 条 entry 只有一个 `.`（一个英文句号），无人记得何时写入；它每次会话都被注入到系统提示里
- **根因**：`MemoryStore.add()` 只校验"非空"+"非恶意注入"两种输入，**没有"实质内容"过滤**——LLM 极少数情况输出空兜底成 `.`，校验通过写入成功
- **盲点轴**：轴 1（异常路径输入域 4 分类不全）+ 轴 6（垃圾内容静默进系统提示，agent 行为被毫无意义地污染却没有可观测信号）
- **当时为何漏**：
  - `test_add_empty_rejected` 测了**空字符串 / 全空白**（输入域第 2 类）
  - `test_add_injection_blocked` 测了**恶意模式 / 注入**（输入域第 3 类）
  - **"形式合法 + 内容空洞"**（第 4 类）：`"."`、`"---"`、`".`"`、`"。。。"` 没人列入输入清单，全部漏过
- **修复测试**：`tests/tools/test_memory_tool.py::TestMemoryStoreAdd::test_add_pure_punctuation_rejected`（参数化 8 种垃圾）+ `test_add_short_but_valid_passes`（5 种合法短文本不误伤）+ `test_replace_pure_punctuation_rejected`（同样护栏覆盖 `replace`）

### 4. LCM compression 把 dedup 命中误报成 embedder 失败（2026-05）

- **现象**：用户压力测试后看见 `⚠ LCM: 0 new chunks — embedder likely failed and fell back to passthrough.`；但 `~/.hermes/lcm/store.db` 实际有 91 条 chunks 用 `sentence-transformers / dim=1024` 正常嵌入，`agent.log` 里 0 条 `LCM embedding failed` / `LCM init failed`——embedder 根本没失败，是误报
- **根因**：`run_agent.py:9300-9328` 用 `lcm_indexed_chunks` 的 delta 推断"是否成功"——`delta == 0` 直接归因为 embedder failure。但 `delta == 0` 是**充分不必要条件**：
  - 真失败（init/embed/store.add 抛异常）→ ✓ delta=0
  - 同一 session 第二次 compression，chunks 全部命中 `INSERT OR IGNORE` → ✗ 也是 delta=0
  - 跨 session dedup 全部命中 `chunks` 表 → ✗ 也是 delta=0
  - middle 全被 redact 抹空 → ✗ 也是 delta=0
- **盲点轴**：轴 6（可见状态语义错乱：把"成功 + 0 新增"翻译成"失败"）+ anti-pattern #7 的对偶面（不仅有"错误的成功消息"，还有"错误的失败消息"——把正常 dedup 喊成 alarm）
- **当时为何漏**：
  - `test_compress_falls_back_to_passthrough_when_embedder_init_fails` 只测了"真失败时 compress 返回 passthrough"，没测"UI 警告判定"
  - `ChunkStore.add()` 内部已经统计了 `new_count / reused_count / already_attached_count`，但只 log 了，没 surface 给 engine / run_agent 用——**信息已具备，链路没打通**
  - 没人写"端到端契约：第二次 compress 同样消息时 UI 不应报警告"——典型 anti-pattern #6（A 模块写 stats、B 模块读 chunk_count，缺契约）
- **修复测试**：
  - `tests/plugins/test_lcm_engine.py::TestChunkStoreAddStats`（5 个：first-time / 同 session 再 add / 跨 session reuse / 空输入清零 / 混合）
  - `tests/plugins/test_lcm_engine.py::TestLCMEngineCompressStatus`（8 个：ok+new / ok+dedup / init_failed / embed_failed / store_add_failed / nothing_to_index / get_status 暴露字段 / 首次未压缩前不暴露字段）

> **追加新条目时，按"现象 / 根因 / 盲点轴 / 当时为何漏 / 修复测试"五段式记录**（在 #1 三段式基础上扩充，把"根因"和"为何漏"独立出来更利于以后定位类似问题）。

---

## 触发条件

下列改动**必须**先跑一遍这个 skill 后再开 PR：

- 任何写持久化状态的代码（DB / 文件 / 缓存 / SQLite session DB / cache）
- 任何 schema / config 版本变更
- 任何与 session_id / tenant / profile 相关的逻辑
- 任何 `__init__` / migration / 启动序列代码
- 修复一个 bug 的同时（测试 RED → 修复 GREEN → 回归保障三步走，第一步必须先用本 skill 想清楚 RED 测试该锁哪一轴）

## 何时跳过

- 改一行注释 / format
- 纯文档变更
- 已有测试**完整覆盖 6 轴**的微调（注意：很少真正满足）

---

## 参考

- `docs/agents/testing.md` — 怎么跑 / change-detector 反模式
- `tests/conftest.py` — `_isolate_hermes_home` 的用法
- `AGENTS.md` Testing 节 — 项目级铁律
- `tests/plugins/test_lcm_engine.py::TestCrossSessionDedup` 与 `TestLegacyMigration` — 6 轴测试的真实样板
