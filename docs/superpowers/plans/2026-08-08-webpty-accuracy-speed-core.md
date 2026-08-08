# WebPty 迭代升级计划 v2(傻瓜级详细版)— 预算计量准确 / 流畅快速 / 核心体验

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. 每个 Task 的每个 Step 已拆成 ≥3 个可直接照做的子步骤(1.1 / 1.2 / 1.3 …),每步子步骤都标注了:文件锚点、改前现状、具体改法、验证命令与期望结果。做完一个 Task 就勾掉它的验收清单。

**Goal:** 依据核心法则(单用户 · 按项目监督常驻 agent · 移动端优先 · 轻量零依赖)做一轮迭代升级,三线并进:
1. **优化预算** — 修掉"计量不准"的 6 个根因(3 项已实测复现:codex 字段解析丢失、`cache_read_input_tokens` 漏计、`parse_usage` 崩溃;3 项静态确认:realtime 重复记账、budget 不落盘、不用工具实际费用)
2. **更加流畅快速** — 消除 reconciler 同步扫描阻塞事件循环、agent 输出跨块乱码、项目列表重复 scandir
3. **更符合核心要求** — 会话自动重启/挂起检测、移动端输入体验、单用户安全默认值

**Architecture:** 全部改动落在业务层与 `session_manager.py`(事件总线侧),**`src/pty_host.py` 内核保持冻结**(其输出管线 Outbox/merge 已完成,不动);数据库沿用 `src/db.py`(`token_usage` 表,新增 `source='actual'` 值,不加表);前端仅动 `public/app.js` 输入路径与软键盘。

**Tech Stack:** Python ≥3.10 标准库,零第三方依赖;测试 `unittest`;基准 `bench/ws_throughput.py`(记录用,非 gate)。

## Global Constraints(所有 Task 都必须遵守)

- 测试命令:`python3 -m unittest discover -s test`(当前 254 个测试必须保持全绿,skipped=2 平台项)
- 项目里 Python 解释器是 `.venv/bin/python`;跑测试统一用 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
- 现有代码风格:PEP8、类型注解;裸 `except` 仅在有 `# noqa: BLE001` 注释处
- 禁止修改 `src/pty_host.py`(内核冻结);`src/session_manager.py` 仅新增 emit/输入解码/重启逻辑(不承载业务逻辑,业务留在 cost_tracker/reconciler/notifier)
- 每任务独立可验证、独立 commit;提交前缀 `fix(cost):` / `perf(server):` / `feat(ui):` 等
- 成本语义:realtime 与 posthoc 都记 `token_usage`;新增 `source='actual'` 记录工具自报费用(如 `total_cost_usd`),`cost` 列存实际金额,`usage_summary` 汇总时 actual 优先于估算
- 验证脚本一律放 `test/`;临时脚本用完即删,不得留在仓库
- 安全:输入端点/WS 不做破坏性变更;`bindHost` 默认值变更属行为变更,需同步 README

---

## ⚠️ 工作区现状(2026-08-08,执行本 plan 前必读)

工作区存在一批**未提交**的安全修复改动(14 个文件,+249/-25,与上轮 issue 草稿对应,代码注释中标注了 Issue 编号)。**执行本 plan 前,先 `cd /root/webpty && git diff` 审阅并提交这批改动(或确认其去向),再开始 Task 1**,否则行号锚点会漂移、改动会重复。

已覆盖、与本 plan 重叠(执行时跳过实现,只补测试/收尾):
- `src/usage_parser.py`:model 崩溃防护 → 对应本 plan **Task 1 Step 3**。执行 Task 1 时先 `git diff src/usage_parser.py` 确认,已实现则只补 Step 1 的回归测试(1.3 用例仍应添加)
- `src/config.py`(bindHost 默认 127.0.0.1)+ `src/server.py`(空 gate + 非回环 → 启动拒绝)→ 对应本 plan **Task 10 的核心**,已实现;Task 10 剩余:install.sh 非 root 默认、README 迁移指引、测试补全
- `public/app.js`:escapeHtml 引号转义 + 链接 URL 字符排除(存储型 XSS)、WS token 改走 cookie(SameSite=strict,不再进 URL)
- `src/ws.py`(WS 帧上限 16MB)、`src/pty_host.py`(socket chmod 0700 + SO_PEERCRED)、`src/backup.py`(restore basename + 配置净化)、`src/migrator.py`(sanitize_import_config)、`src/agent_config.py`(密钥脱敏)、`src/session_manager.py`(日志 5MB 轮转)

与本 plan **无重叠、仍需实施**:Task 2(增量去重)、Task 3(budget 落盘)、Task 4(actual 费用)、Task 5(reconcile 异步化)、Task 6(增量解码)、Task 7(mtime 缓存)、Task 8(自动重启/挂起)、Task 9(大粘贴分段)。

---

### Task 1: usage_parser 字段兼容 + 崩溃防护 + cache 计价修正(A1/A2/A3)

**Files:**
- Edit: `src/usage_parser.py`
- Edit: `src/price_table.py`
- Test: `test/test_usage_parser.py`(现有 9 个用例,见 `UsageParserTest` 类)

**Interfaces(本 Task 产出,后续 Task 依赖):**
- `parse_usage(line, tool) -> dict | None` 签名不变,**永不抛异常**(任何输入都返回 dict 或 None)
- `_extract(usage)` 返回值扩展为 `{"tokens_in", "tokens_out", "cached_in", "cached_write"}`
  - `cached_in` = cache **read**(按 `cache_hit` 价计)
  - `cached_write` = cache **write**(按 `input` 价计,是新增字段)
- `cost_for(model, tokens_in, tokens_out, config, cached_in=0, cached_write=0)` 新增 `cached_write` 参数,默认 0

**实测确认的现状问题(改之前先看明白):**
- `src/usage_parser.py:19-25` 的 `_extract` 只认 `input_tokens` / `output_tokens` / `input_tokens_cached` / `cache_creation_input_tokens`。codex/OpenAI 的 `prompt_tokens`/`completion_tokens` **实测解析为 None**(计量 100% 丢失)
- `cache_read_input_tokens`(Anthropic 缓存读)**实测漏计**,cached_in=0(缓存命中被按全价 input 计,高估约 10 倍价差)
- `cache_creation_input_tokens`(缓存写)被当作 cache_hit 低价计 → 应改为按 input 价计
- `src/usage_parser.py:45` `obj.get("message", {}).get("model")` 在 message 非 dict 且无顶层 model 时**实测抛 AttributeError**(实时计量丢条、reconciler 整轮中断)

**Steps:**

- [x] **Step 1: 写失败测试(红)** — 在 `test/test_usage_parser.py` 的 `UsageParserTest` 类里追加三个回归用例
  - [x] 1.1 打开 `test/test_usage_parser.py`,在文件末尾(类内)追加 `test_codex_prompt_completion_fields`:调用 `parse_usage('{"type":"usage","usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}', "codex")`,断言返回 dict 且 `tokens_in == 100`、`tokens_out == 50`。运行 `cd /root/webpty && .venv/bin/python -m unittest test.test_usage_parser.UsageParserTest.test_codex_prompt_completion_fields -v`,期望:**FAIL**(现状返回 None)——失败即证明测试有效
  - [x] 1.2 追加 `test_cache_read_and_write_split`:调用 `parse_usage('{"usage":{"input_tokens":100,"output_tokens":10,"cache_read_input_tokens":90,"cache_creation_input_tokens":5}}', "claude")`,断言 `cached_in == 90` 且 `cached_write == 5`。运行同上,期望:**FAIL**(现状 cached_in=0 且无 cached_write 键)
  - [x] 1.3 追加 `test_message_array_no_crash`:调用 `parse_usage('{"message":["x"],"usage":{"input_tokens":1,"output_tokens":1}}', "claude")`,断言**不抛任何异常**(返回 dict 或 None 均可)。运行,期望:**FAIL**(现状抛 AttributeError)
  - [x] 1.4 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`,确认只有这 3 个新用例失败、既有 254 个仍全绿(防止测试写错)

- [x] **Step 2: `_extract` 三套字段归一** — 编辑 `src/usage_parser.py`
  - [x] 2.1 在 `_extract`(现 19-25 行)内,`tokens_in` 改为:`_to_int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)`;`tokens_out` 改为:`_to_int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)`(OpenAI/codex 字段兜底)
  - [x] 2.2 `cached_in`(缓存读)改为按优先级取:`usage.get("cache_read_input_tokens")` → `usage.get("input_tokens_cached")` → `usage.get("cached_tokens")` → `(usage.get("prompt_tokens_details") or {}).get("cached_tokens")`,最后 `_to_int(...)` 兜 0
  - [x] 2.3 新增 `cached_write`:`_to_int(usage.get("cache_creation_input_tokens") or 0)`
  - [x] 2.4 返回值 dict 增加 `"cached_write"` 键;**同时**确认 `parse_usage` 内两处返回 dict 的地方(39-47 行 usage 分支、48-57 行 stats/usage_event 分支)都带上 `cached_write`(stats 分支可填 0)

- [x] **Step 3: `parse_usage` 崩溃防护** — 编辑 `src/usage_parser.py`
  - [x] 3.1 **先查工作区**:`git diff src/usage_parser.py`——2026-08-08 工作区已含 model 安全取值修复(`msg = obj.get("message"); model = msg.get("model") if isinstance(msg, dict) else None`)。若已存在:**跳过实现**,只确保 Step 1 的 1.3 回归测试已添加;若不存在,定位 45 行 `"model": obj.get("model") or obj.get("message", {}).get("model")`,改为:先取 `msg = obj.get("message")`,model 写成 `obj.get("model") or (msg.get("model") if isinstance(msg, dict) else None)`
  - [x] 3.2 全函数检查一遍所有 `.get` 链:`message = obj.get("message"); usage = message.get("usage") if isinstance(message, dict) else None`(37-38 行)已安全,确认 45 行是唯一裸链
  - [x] 3.3 给 `parse_usage` 函数体最外层加防御性 try/except(除 json.loads 外再包一层):`except Exception:` 返回 None——保证"永不抛异常"成为接口契约(注释说明:解析失败返回 None,由 reconciler 事后兜底)

- [x] **Step 4: `price_table.cost_for` 区分 cache 读写价** — 编辑 `src/price_table.py`
  - [x] 4.1 定位 `cost_for`(74-81 行),签名改为 `def cost_for(model, tokens_in, tokens_out, config, cached_in=0, cached_write=0):`
  - [x] 4.2 计算改为:`fresh = max(tokens_in - max(cached_in,0) - max(cached_write,0), 0)`;`total = (fresh * input价 + cached_in * cache_hit价 + cached_write * input价 + tokens_out * output价) / 1_000_000.0`;每项用 `float(p.get(...))` 且各自 `max(..., 0)`(与现 77-80 行结构一致,只把 cached_write 按 input 价加入)
  - [x] 4.3 保持 `get_price`(66-71 行)不动;检查 `src/cost_tracker.py:60-61` 与 `src/reconciler.py:53-54` 的 `cost_for` 调用处,确认不传 `cached_write` 时默认 0 行为不变(暂不改调用处,Task 1 只动签名)

- [x] **Step 5: 全量验证(绿)**
  - [x] 5.1 跑 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`,期望:全部通过(254 + 3 新增)
  - [x] 5.2 手动核对:`cd /root/webpty && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); from usage_parser import parse_usage; print(parse_usage('{\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":50}}','codex'))"` 期望输出含 `tokens_in: 100`(此项为人工核对,不必留脚本)
  - [x] 5.3 提交:`git add src/usage_parser.py src/price_table.py test/test_usage_parser.py && git commit -m "fix(cost): parse codex/OpenAI usage fields, count cache read/write separately, never raise"`

- [x] **Task 1 验收清单**
  - [x] 三个新测试全绿,旧测试无回归
  - [x] `parse_usage` 对任意输入不抛异常(契约成立)
  - [x] `cost_for` 支持 `cached_write` 且默认行为不变

---

### Task 2: realtime 累计值去重(A4)

**Files:**
- Edit: `src/cost_tracker.py`
- Test: `test/test_cost_tracker.py`(现有 `CostTrackerTest`,含 `test_realtime_skips_posthoc_duplicate`)

**Interfaces:**
- `CostTracker` 增加实例状态 `self._last_usage: dict[str, dict]`(key=`session_id`,value=该会话最近一次记账的 `{"tokens_in","tokens_out","cached_in","cached_write"}`)
- `_record` 语义变更:新事件与上次值逐字段求**增量**(`delta = max(cur - last, 0)`);delta 全 0 → 跳过不落库;首次事件(无 last)按全量记
- 会话结束时(`session_event` type ∈ {completed, failed, crashed, terminated})清除 `_last_usage[sid]`,防止新会话串台

**背景(为什么做):** codex/reasonix 每次 usage 事件发的是**累计值**(整轮对话累计)。现状 `cost_tracker._record`(38-77 行)对每个事件都 `add_usage` 一条,同一会话 N 个事件就记 N 条重复 → 成本高估 N 倍。dedup 查询(63-70 行)只防 posthoc 重复,防不了 realtime 内部重复。

**Steps:**

- [x] **Step 1: 写失败测试(红)** — 在 `test/test_cost_tracker.py` 追加 `test_realtime_cumulative_dedup`
  - [x] 1.1 参照既有 `test_records_from_usage_agent_event`(75 行起)的起法:构造 `CostTracker(db, config)`,手动调用 `await tracker._record({"usage": {...}, "tool": "codex", "project": "p"}, "sid-1")` 三次,usage 分别为累计值 `{"prompt_tokens":100,"completion_tokens":50}` → `{"prompt_tokens":150,"completion_tokens":80}` → `{"prompt_tokens":150,"completion_tokens":80}`(第三次与第二次相同)
  - [x] 1.2 断言:DB 中该 session 共 2 行(第 1 次全量 100/50,第 2 次增量 50/30,第 3 次跳过);`SUM(tokens_in)=150`、`SUM(tokens_out)=80`(等于最终累计值,不重复)
  - [x] 1.3 运行 `cd /root/webpty && .venv/bin/python -m unittest test.test_cost_tracker.CostTrackerTest.test_realtime_cumulative_dedup -v`,期望:**FAIL**(现状落 3 行)
  - [x] 1.4 追加 `test_session_end_clears_cumulative`:记 sid-1 一条,发 `session_event` completed,再用 sid-1 记新会话首条 200/100 → 断言落库(说明状态已清、没按上次值算增量)。运行,期望:**FAIL**(现状无清理逻辑,但此用例在 Step 2 完成后才绿)

- [x] **Step 2: 实现增量记账** — 编辑 `src/cost_tracker.py`
  - [x] 2.1 在 `__init__`(17-23 行)加 `self._last_usage: dict[str, dict] = {}`
  - [x] 2.2 在 `_record`(38-77 行)中,`usage` 解析成功、`model`/`cost` 确定之后、`add_usage`(71-77 行)之前插入增量逻辑:`last = self._last_usage.get(session_id)`;若 last 存在,`delta_in = max(usage["tokens_in"] - last["tokens_in"], 0)`,out/cached_in/cached_write 同理;若 `delta_in == 0 and delta_out == 0 and cached 增量均为 0` → `return`(不落库,但**仍更新 last**);否则用 delta 值构造落库 dict,并 `self._last_usage[session_id] = {"tokens_in": usage["tokens_in"], ...}`(存原始累计值,不是 delta)
  - [x] 2.3 注意:落库行的 `cost` 也要按 delta 比例折算——简化方案:cost 由 `cost_for(model, delta_in, delta_out, config, cached_in=delta_cached_in, cached_write=delta_cached_write)` 重算(不用 `usage.get("cost")`,因为那是累计值)。在代码注释写明这个折算
  - [x] 2.4 保持既有 posthoc dedup 查询(63-70 行)不动,顺序在增量判断之后(先增量判断,再查 posthoc,再落库)

- [x] **Step 3: 会话结束清理状态**
  - [x] 3.1 查看 `cost_tracker` 是否有订阅 `session_event` 的入口:现有 `handle_agent_event`(25-28 行)只收 `agentEvent`。在 `server.py` 找 `sessions.on("session_event", ...)` 的既有订阅点(`notifier` 有),仿照 `Notifier` 的注册方式,在 `server.py` 启动段(`main()`)给 `CostTracker` 增加一个 `on_session_event(event)` 方法并注册:`sessions.on("session_event", self.cost.on_session_event)`
  - [x] 3.2 `on_session_event(self, event: dict)` 实现:`if event.get("type") in ("completed","failed","crashed","terminated"): self._last_usage.pop(event.get("session_id"), None)`
  - [x] 3.3 检查 `server.py` 中 `CostTracker` 实例化与事件注册的位置(与 `Notifier` 相邻),保持注册顺序一致、不重复注册

- [x] **Step 4: 验证(绿)**
  - [x] 4.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_cost_tracker -v`,期望:全部通过(8 旧 + 2 新)
  - [x] 4.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`,期望全绿
  - [x] 4.3 提交:`git add src/cost_tracker.py src/server.py test/test_cost_tracker.py && git commit -m "fix(cost): dedup cumulative realtime usage per session, clear state on session end"`

- [x] **Task 2 验收清单**
  - [x] 同一会话累计事件只按增量落库,总额=最终累计值
  - [x] 重复同值事件不落库(幂等)
  - [x] 会话结束后状态清除,新会话首条按全量记

---

### Task 3: budget 持久化(A5)

**Files:**
- Edit: `src/cost_tracker.py`
- Edit: `src/server.py`(`/api/cost/budget` PUT,`server.py:573-581` 附近)
- Test: `test/test_cost_tracker.py`、`test/test_server.py`

**背景(为什么做):** `set_budget`(cost_tracker.py:91-96)只改内存 `self._budget` 和 `self.config["budget"]`,**从不 `save_config`** → 服务重启后预算设置丢失(用户配的限额"不准"的又一来源)。

**Steps:**

- [x] **Step 1: 写失败测试(红)**
  - [x] 1.1 在 `test/test_cost_tracker.py` 追加 `test_budget_persists_to_config`:构造临时 data_dir + `config_path`(参照 `test_config.py` 里 env 注入 `WEBPTY_DATA_DIR` 的写法),`set_budget(12.5)` 后,重新 `load_config()` 读取,断言 `config["budget"]["limit"] == 12.5`
  - [x] 1.2 运行,期望:**FAIL**(现状 config.json 无 budget 键)
  - [x] 1.3 在 `test/test_server.py` 追加集成用例 `test_budget_put_persists`(用既有 `ServerIntegrationTest` 样板:env 注入 `WEBPTY_DATA_DIR` + `_pick_port()` + 轮询就绪):`PUT /api/cost/budget {"limit": 5}` → 读 data_dir/config.json,断言 `budget.limit == 5`。运行,期望:**FAIL**

- [x] **Step 2: 实现落盘**
  - [x] 2.1 `cost_tracker.py` 顶部 `from config import save_config`(检查现有 import,`save_config` 在 `config.py:268`)
  - [x] 2.2 `set_budget`(91-96 行)末尾加 `save_config(self.config)`
  - [x] 2.3 确认 `server.py:573-581` 的 PUT 处理不重复落盘(调 `set_budget` 即可,不加额外逻辑)

- [x] **Step 3: 验证(绿)**
  - [x] 3.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_cost_tracker test.test_server -v`,期望全绿
  - [x] 3.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 3.3 提交:`git add src/cost_tracker.py test/test_cost_tracker.py test/test_server.py && git commit -m "fix(cost): persist budget limit to config.json"`

- [x] **Task 3 验收清单**
  - [x] `PUT /api/cost/budget` 后 config.json 含 `budget.limit`
  - [x] 重启(重建 CostTracker)后 `over_budget` 使用持久化的 limit

---

### Task 4: 采用工具实际费用(A6)

**Files:**
- Edit: `src/cost_tracker.py`
- Edit: `src/db.py`(`usage_summary`/`usage_grouped`)
- Edit: `public/app.js`(成本面板展示)
- Test: `test/test_cost_tracker.py`、`test/test_db.py`

**背景(为什么做):** reasonix 的 `result` 事件自带 `total_cost_usd`(真实账单金额,`session_manager.py:560-565` 已把它放进 `_push_agent` 的 `costUsd` 字段)。现状 cost_tracker 只按价格表**估算**(price_table 是 DeepSeek 网关预设价),与真实账单有偏差。应优先采用工具自报的实际费用。

**Steps:**

- [x] **Step 1: 写失败测试(红)**
  - [x] 1.1 在 `test/test_cost_tracker.py` 追加 `test_records_actual_cost_from_result`:构造 agentEvent `{"t": "result", "costUsd": 1.23, "session_id": "s1", "model": "deepseek-v3"}` 调 `handle_agent_event`(25-28 行,注意它是 sync 方法内部 create_task,测试里需 `await asyncio.sleep(0.05)` 等 task 完成,参照既有 `test_records_from_raw_stream_json` 的等待写法),断言 DB 落一行 `source == 'actual'` 且 `cost == 1.23`
  - [x] 1.2 运行,期望:**FAIL**(现状 result 事件被忽略)
  - [x] 1.3 追加 `test_actual_cost_not_double_counted`:`add_usage` 先落一行估算(同 session,source=realtime),再注入 result 事件,`usage_summary("month")` 断言 `cost` 只含 actual 值 1.23 而估算行不计入。运行,期望:**FAIL**(现状 SQL 全加)

- [x] **Step 2: cost_tracker 消费 result 事件**
  - [x] 2.1 `handle_agent_event`(25-28 行)入口处加分支:`if event.get("t") == "result": return await self._record_actual(event, sid)`(直接走 async 路径;`handle_agent_event` 是 sync 包装,内部已 create_task,参照现有写法)
  - [x] 2.2 新增 `async def _record_actual(self, event, sid=None) -> None`:`costUsd = event.get("costUsd")`;若非数字(`not isinstance(costUsd, (int, float))`)则 return;`session_id = event.get("session_id") or sid`;`add_usage({"project": event.get("project"), "tool": event.get("tool"), "model": event.get("model") or event.get("tool") or "unknown", "session_id": session_id, "tokens_in": 0, "tokens_out": 0, "cost": float(costUsd), "source": "actual"})`
  - [x] 2.3 防重复:落 actual 前查 `SELECT 1 FROM token_usage WHERE session_id=? AND source='actual' LIMIT 1`,已有则 return(一个会话只记一条实际费用)

- [x] **Step 3: db 汇总 actual 优先** — 编辑 `src/db.py`
  - [x] 3.1 定位 `usage_summary`(217-226 行)。改法(保持 SQL 全参数化):`source='actual'` 的行按 cost 计入,估算行(`source != 'actual'`)的 **cost 不计入**、tokens 仍计入——即 SQL 里 `COALESCE(SUM(CASE WHEN source='actual' THEN cost ELSE 0 END),0) AS cost`,tokens 的 SUM 不变
  - [x] 3.2 定位 `usage_grouped`(228-238 行):同样把 `COALESCE(SUM(cost),0)` 改为 `COALESCE(SUM(CASE WHEN source='actual' THEN cost ELSE 0 END),0)`;tokens 聚合不变
  - [x] 3.3 在 `test/test_db.py` 追加 `test_summary_actual_preferred`:落估算行(cost=9.9)+ actual 行(cost=1.23),断言 summary.cost == 1.23 且 tokens 为两者之和

- [x] **Step 4: 前端展示区分估算/实际** — 编辑 `public/app.js`
  - [x] 4.1 定位成本面板 `refreshCostPanel`(约 2401 行)与 `api('/api/cost/summary?...')` 调用。响应已是新口径(actual 优先),前端无需改计算;在面板标题/数值旁标注来源:`估算成本` 改为 `实际成本(工具上报,无上报时回退估算)`——若希望区分,给 summary 增加 `estimated` 字段(见 4.2 可选)
  - [x] 4.2(可选)在 `usage_summary` 返回 dict 增加 `estimated` 键(估算行 cost 之和,另一条 CASE SQL 或内存差),前端展示"实际 $x / 估算 $y"。若做,同步更新 `test_db` 断言
  - [x] 4.3 手工冒烟:起服务,打开成本面板,确认数字有来源标注、无 JS 报错

- [x] **Step 5: 验证(绿)**
  - [x] 5.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_cost_tracker test.test_db -v`,期望全绿
  - [x] 5.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 5.3 提交:`git add src/cost_tracker.py src/db.py public/app.js test/ && git commit -m "feat(cost): prefer tool-reported actual cost over estimates"`

- [x] **Task 4 验收清单**
  - [x] result 事件落 `source='actual'` 行,同会话只落一条
  - [x] summary/grouped 的 cost 口径 = actual 优先,tokens 仍全量
  - [x] 前端有估算/实际来源标注

---

### Task 5: reconcile 异步化 + 单文件上限(B1)

**Files:**
- Edit: `src/reconciler.py`
- Edit: `src/server.py`(`/api/cost/reconcile`,`server.py:584-588`)
- Test: `test/test_reconciler.py`、`test/test_server.py`

**背景(为什么做):** `scan_claude_logs`(reconciler.py:12-32)同步逐行扫描 `~/.claude/projects` 下全部 .jsonl(百 MB 级),`server.py:584-588` 在 async handler 里直接 `await rec.reconcile(...)` → **阻塞事件循环数秒**,期间所有 WS/HTTP 卡顿。且无文件大小上限;`usage_parser` 崩溃(已由 Task 1 修复)曾使整轮中断。

**Steps:**

- [x] **Step 1: 写失败测试(红)**
  - [x] 1.1 在 `test/test_reconciler.py` 追加 `test_scan_skips_oversize_file`:临时目录写两个文件——`big.jsonl`(内容 ≥ 上限 20MB,用 `"x" * (21*1024*1024)` 一行)与 `small.jsonl`(一行合法 usage JSON);调 `scan_claude_logs(tmpdir)`,断言返回含 small 的解析结果、不含 big,且返回值是 `(entries, skipped)` 或带 `skipped` 计数(按 2.1 定的返回形态写断言)。运行,期望:**FAIL**(现状无上限,big 会被读爆内存/超时)
  - [x] 1.2 在 `test/test_server.py` 追加 `test_reconcile_nonblocking`:起服后先 `POST /api/cost/reconcile`,再立即 `GET /api/config`(带 2s 超时),断言 config 请求在 reconcile 期间能快速返回(若实现为线程池,此测试在 Step 2 后绿;红阶段断言"当前实现会阻塞"较难测,此用例可标记为 Step 2 完成后验收,红阶段先跑 1.1)
  - [x] 1.3 跑 1.1 确认红:`cd /root/webpty && .venv/bin/python -m unittest test.test_reconciler.ReconcilerTest.test_scan_skips_oversize_file -v`

- [x] **Step 2: 实现** — 编辑 `src/reconciler.py`
  - [x] 2.1 `scan_claude_logs(projects_dir, max_bytes=20*1024*1024) -> tuple[list[dict], int]`:签名改为返回 `(entries, skipped_count)`(或保留 list 但给 `reconcile` 加内部计数——选前者,改动可控);`os.path.getsize(path) > max_bytes` 的文件直接 `skipped += 1; continue`;注释说明上限常量
  - [x] 2.2 `reconcile`(40-61 行)改为:`entries, skipped = await asyncio.to_thread(scan_claude_logs, projects_dir)`(顶部 `import asyncio`);`skipped` 透出返回:`return {"added": added, "skipped_files": skipped}`(注意既有调用方 `server.py:588` 用 `added`,改为读 `res["added"]`)
  - [x] 2.3 既有测试 `test_reconcile_persists_posthoc` 等断言 `added` 数字——同步更新调用处解包方式(`res = await rec.reconcile(...); self.assertEqual(res["added"], n)`)

- [x] **Step 3: server 端点适配** — 编辑 `src/server.py:584-588`
  - [x] 3.1 改:`res = await rec.reconcile(claude_dir)` → `res = await rec.reconcile(claude_dir)` 不变,但响应改回 `{"added": res.get("added", 0), "skipped_files": res.get("skipped_files", 0)}`(若 2.2 已返回 dict)
  - [x] 3.2 确认 `claude_dir` 传参不变;前端(`public/app.js` 若有 reconcile 按钮)响应读取 `added` 不变
  - [x] 3.3 检查 `server.py` 其他调用 `reconcile` 的地方(全仓库 grep `rec.reconcile`),统一适配

- [x] **Step 4: 验证(绿)**
  - [x] 4.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_reconciler test.test_server -v`,期望全绿
  - [x] 4.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 4.3 手动:`POST /api/cost/reconcile` 期间开另一个页面滑动会话,无卡顿
  - [x] 4.4 提交:`git add src/reconciler.py src/server.py test/ && git commit -m "perf(cost): run reconcile scan in thread, cap per-file size"`

- [x] **Task 5 验收清单**
  - [x] 扫描在线程执行,事件循环不阻塞
  - [x] >20MB 文件跳过并计数,响应含 `skipped_files`
  - [x] 既有 reconcile 测试语义不变(added 正确)

---

### Task 6: agent 输出增量解码(B2)

**Files:**
- Edit: `src/session_manager.py`(`read_stdout`/`read_stderr`,约 448-470 行)
- Test: `test/test_session_manager.py`

**背景(为什么做):** `read_stdout`(448-455 行)按 4096 字节 `chunk.decode("utf-8", "replace")` 解码。多字节 UTF-8 字符(如中文)被 4096 边界切开时,后半字节解码失败 → 输出出现 `�` 乱码,且会污染后续行的解析(`_handle_agent_line` 收到的行含替换符)。

**Steps:**

- [x] **Step 1: 写失败测试(红)**
  - [x] 1.1 在 `test/test_session_manager.py` 追加 `test_utf8_split_across_chunks`:模拟"流式喂入"——构造一个字节串 `("你" * 2000).encode()`(6000 字节),按 4096 边界切成两段 `chunk1, chunk2`;用一个 `codecs.getincrementaldecoder("utf-8")` 实例依次 decode 两段(这是实现后的语义),断言两段解码拼接结果 == 原文、且**无 `\ufffd` 替换符**。红阶段:直接测现有 `chunk.decode("utf-8","replace")` 两段拼接,断言失败(含 `\ufffd`)——把现有行为固化成失败用例
  - [x] 1.2 追加集成形态用例(可选):若 `session_manager` 有现成注入流数据的测试钩子则用之;没有就先只做 1.1 的纯解码器用例(实现细节级,足以防回归)
  - [x] 1.3 运行 `cd /root/webpty && .venv/bin/python -m unittest test.test_session_manager.SessionManagerTest.test_utf8_split_across_chunks -v`,期望:**FAIL**(现 decode 方式产生替换符)

- [x] **Step 2: 实现增量解码** — 编辑 `src/session_manager.py`
  - [x] 2.1 顶部 `import codecs`(检查现有 import 区)
  - [x] 2.2 `read_stdout`(448 行起)内、`while True` 前创建解码器:`decoder = codecs.getincrementaldecoder("utf-8")()`(用默认 errors="strict" 即可,增量解码器不会因跨块报错;若担心极端损坏输入,用 `codecs.getincrementaldecoder("utf-8")("replace")`)
  - [x] 2.3 循环内把 `text = chunk.decode("utf-8", "replace")` 改为 `text = decoder.decode(chunk)`;循环结束后 `text += decoder.decode(b"", final=True)`(冲刷残留字节,不丢数据)
  - [x] 2.4 同一模式应用到 `read_stderr`(464-470 行);顺带把 `proc.stdout.read(4096)` 与 `proc.stderr.read(4096)` 改为 `read(16384)`(减少 syscall,收益小但零风险)
  - [x] 2.5 确认 `state["buf"]` 按行切分逻辑(456-462 行)不受影响(解码器已保证 text 是完整字符流)

- [x] **Step 3: 验证(绿)**
  - [x] 3.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_session_manager -v`,期望全绿
  - [x] 3.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 3.3 手动:起一个含中文输出的 agent 会话,长输出滚动无 `�`(人工冒烟)
  - [x] 3.4 提交:`git add src/session_manager.py test/test_session_manager.py && git commit -m "fix(agent): incremental UTF-8 decoding across chunk boundaries"`

- [x] **Task 6 验收清单**
  - [x] 跨块多字节字符无替换符
  - [x] stderr 同样处理
  - [x] 行切分/解析逻辑无回归(全量测试绿)

---

### Task 7: 项目列表 mtime 缓存(B3)

**Files:**
- Edit: `src/server.py`(`_claude_history_mtime`,154-168 行;`_list_projects`,170-199 行)
- Test: `test/test_server.py`

**背景(为什么做):** `_list_projects`(170-199 行)对每个项目调 `_claude_history_mtime`(154-168 行),后者对 `~/.claude/projects/<proj>` 做 `os.listdir` + 逐个 `getmtime`。项目多/历史目录大时,每次 `GET /api/projects` 都重复全量 scandir → 页面打开与刷新变慢(与"流畅"目标相悖)。

**Steps:**

- [x] **Step 1: 写失败测试(红)**
  - [x] 1.1 在 `test/test_server.py` 追加 `test_projects_mtime_cached`:起服(既有 `ServerIntegrationTest` 样板),用 `unittest.mock.patch` 包住 `server.os.listdir`(或 `_claude_history_mtime`),调用两次 `GET /api/projects`,断言 `listdir` 对同一项目目录只被调用 **1 次**(缓存生效);第二次调用前把 mock 计数清零
  - [x] 1.2 运行,期望:**FAIL**(现状每次请求都 listdir)
  - [x] 1.3 追加 `test_mtime_cache_invalidated_on_roots_change`:`PUT /api/config/roots` 后,下一次 `GET /api/projects` 重新扫描(断言 listdir 计数增加)——保护"改 roots 必须清缓存"的语义

- [x] **Step 2: 实现 TTL 缓存** — 编辑 `src/server.py`
  - [x] 2.1 在 `WebPtyServer` 类 `__init__` 加 `self._claude_mtime_cache: dict[str, tuple[float, float]] = {}`(key=项目绝对路径,value=(mtime, 缓存时间戳));TTL 常量 `_CLAUDE_MTIME_TTL = 30.0` 放模块级
  - [x] 2.2 改 `_claude_history_mtime`(154-168 行):入口先查 `self._claude_mtime_cache`,命中且 `time.time() - ts < TTL` 直接返回缓存值;未命中走原逻辑,结果写缓存
  - [x] 2.3 `_list_projects`(170-199 行)的 roots/extraFolders 变化路径(`/api/config/roots` PUT、`/api/projects` POST 增 extraFolders)后清缓存:`self._claude_mtime_cache.clear()`(在这两处 handler 里加一行)
  - [x] 2.4 注意:缓存只影响 mtime 展示,不影响会话逻辑;mtime 精度损失 ≤30s 可接受(注释说明)

- [x] **Step 3: 验证(绿)**
  - [x] 3.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_server -v`,期望全绿
  - [x] 3.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 3.3 手动:项目列表页反复刷新,响应时间稳定(人工观察)
  - [x] 3.4 提交:`git add src/server.py test/test_server.py && git commit -m "perf(server): cache claude history mtimes with 30s TTL"`

- [x] **Task 7 验收清单**
  - [x] 30s 内重复请求只 scandir 一次
  - [x] roots/extraFolders 变更后缓存失效
  - [x] 会话相关逻辑不受影响

---

### Task 8: 会话自动重启 + 挂起检测(C1)

**Files:**
- Edit: `src/session_manager.py`(`wait_exit` 472-498 行、`_schedule_auto_resume` 834 行附近)
- Edit: `src/config.py`(默认配置加 `restart` 段)
- Test: `test/test_session_manager.py`

**Interfaces:**
- 新配置(默认值,`config.py` `default_config()` 内):`"restart": {"max_restarts": 3, "backoff_s": 10, "stall_timeout_s": 900}`
- 自动重启:**仅对 `autostart=true` 的会话**,非 0 退出码时自动 `start`;同一会话连续失败(非 0 退出)达 `max_restarts` 次后停止,并发 `session_event`(type=`failed`,带 `restart_exhausted: true`)
- 挂起检测:后台监控 task,每 60s 扫描 running 会话;`turn_active` 为真且距 `last_output_at` 超过 `stall_timeout_s`(默认 15min)→ 发 `session_event` type=`stalled`(只通知,不杀进程);同一会话只报一次(记录已报时间戳)

**Steps:**

- [x] **Step 1: 写失败测试(红)**
  - [x] 1.1 在 `test/test_session_manager.py` 追加 `test_autostart_restarts_on_nonzero_exit`:构造 autostart=true 的 pty 会话(参照既有测试的起会话样板,可用 fake host 或真 pty-host),让进程以 exit_code=1 退出,断言 `start` 被再次调用(用 mock 包 `SessionManager.start` 计数)
  - [x] 1.2 追加 `test_restart_exhausted_stops`:连续 3 次非 0 退出,断言第 4 次不再重启,且收到 `session_event` type=failed 带 `restart_exhausted=True`
  - [x] 1.3 追加 `test_non_autostart_no_restart`:`autostart=false` 会话非 0 退出,断言不重启
  - [x] 1.4 运行 1.1-1.3,期望:**FAIL**(现状无重启逻辑)
  - [x] 1.5 追加 `test_stall_detection`:`_stall_monitor` 逻辑抽成纯函数(如 `_stalled_sessions(now) -> list[sid]`,基于会话状态计算),构造 last_output_at 超时且 turn_active=True 的会话,断言被报;turn_active=False 的不报。运行,期望:**FAIL**(无此函数)

- [x] **Step 2: 实现自动重启** — 编辑 `src/session_manager.py`
  - [x] 2.1 在 `__init__` 增加 `self._restart_counts: dict[str, int] = {}` 与 `self._restart_config = config.get("restart") or {}`(读默认 `{"max_restarts": 3, "backoff_s": 10}`)
  - [x] 2.2 `wait_exit`(472-498 行)内,`session["state"] = "stopped"` 之后、`_push_agent({"t": "exit", ...})` 之前插入:`code = session.get("exit_code"); if code not in (0, None) and session.get("autostart"):` 走 `self._maybe_restart(session, code)`
  - [x] 2.3 新增 `def _maybe_restart(self, session, code) -> None`:`key = session["id"]`;`n = self._restart_counts.get(key, 0) + 1`;若 `n > max_restarts` → 发 `session_event`(type=failed, restart_exhausted=True)、`self._restart_counts.pop(key, None)`、返回;否则 `self._restart_counts[key] = n`,`asyncio.get_event_loop().call_later(backoff_s, lambda: asyncio.create_task(self.start(key)))`(注意:**先清理该会话挂起的 auto-resume 定时器**,见 2.4)
  - [x] 2.4 互斥处理:重启前调用现有清理逻辑——查看 `_schedule_auto_resume`(834 行)及其定时器存储(`self._resume_timers` 之类,若存在则 `cancel`);没有独立存储就确保 `start()` 幂等(现有 `_start_pty` 已有 `state=="running"` 检查)。注释说明与 auto-resume 的关系
  - [x] 2.5 会话被用户手动 `remove`(删除)时清 `_restart_counts.pop(sid, None)`(在 `remove` 方法内加一行),防止 id 复用后计数残留

- [x] **Step 3: 实现挂起检测**
  - [x] 3.1 新增 `async def _stall_monitor(self) -> None`:`while True: await asyncio.sleep(60); for sid, s in self.sessions.items():` 计算 `stalled = s.get("turn_active") and (time.time()*1000 - (s.get("last_output_at") or 0) > stall_timeout_s*1000)`;满足且 `self._stall_reported.get(sid) != 当前分钟` → 发 `session_event`(type=stalled, session_id=sid, name=s.get("name"))并记 `self._stall_reported[sid] = 时间戳`
  - [x] 3.2 在会话 start 时重置 `self._stall_reported.pop(sid, None)`(新一次运行重新计时)
  - [x] 3.3 `main` 启动处(server.py 里 `SessionManager` 初始化后)注册:`asyncio.create_task(sm._stall_monitor())`(或 SessionManager 内部 `start()` 方法统一启动,看现有代码哪里起 `_host_monitor` 类似 task,保持同一位置)

- [x] **Step 4: 验证(绿)**
  - [x] 4.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_session_manager -v`,期望全绿(含新 4 用例)
  - [x] 4.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 4.3 手动:起 autostart 会话,`kill -9` 其进程,观察自动重启;改配置 max_restarts=1 验证停止(人工冒烟)
  - [x] 4.4 提交:`git add src/session_manager.py src/config.py test/test_session_manager.py && git commit -m "feat(session): auto-restart autostart sessions with backoff, stall detection"`

- [x] **Task 8 验收清单**
  - [x] autostart 会话非 0 退出自动重启,达上限停止并报事件
  - [x] 非 autostart 不重启;删除会话清计数
  - [x] 挂起会话 15min 无输出被报 stalled(每会话一次)

---

### Task 9: 移动端输入体验(C2)

**Files:**
- Edit: `public/app.js`(输入路径:大粘贴分段;软键盘已有 `wireComposerSend`,812 行起)
- Test: 前端无测试框架 → 以手工冒烟为主;`test/test_server.py` 的 WS 输入路径可加一个分片合流用例

**背景(现状盘点,先看清楚再动手):**
- `wireComposerSend`(app.js:812 起)已处理:Enter 提交、IME 合成、blur 提交(iOS 键盘发送键)、Shift/Alt+Enter 换行——**软键盘发送已有较好覆盖**,本 Task 不再重复造轮子
- 614 行附近已有"多行粘贴 → bracketed paste"处理(把含换行的粘贴包装为 `\x1b[200~...\x1b[201~` 让 TUI 保留消息内容)
- 缺口:大文本(>32KB)粘贴/拖入时单次 `ws.send` 一帧过大,可能超帧限制或被截断;需分段发送

**Steps:**

- [x] **Step 1: 读代码确认输入路径**
  - [x] 1.1 定位 app.js 中 PTY 输入的发送函数(搜 `ws.send`,约 399 行 resize、502 行日志;输入发送在 paste 与键盘路径),确认二进制/文本帧怎么发、`entry.term` 的输入 hook 在哪(`term.onData` 或 `attachCustomKeyEventHandler`)
  - [x] 1.2 确认 `pasteToSession(entry)`(746 行附近)的完整流程:取剪贴板 → 是否多行 → bracketed paste → `ws.send`
  - [x] 1.3 把结论写进本 Task 注释:分段改造点 = paste 路径的发送处(不要动键盘逐键路径,逐键天然小帧)

- [x] **Step 2: 实现大文本分段发送**
  - [x] 2.1 在 paste 发送处加常量 `const PASTE_CHUNK = 32 * 1024;`(注释:与 server 端 WS 帧处理兼容,32KB 远低于 64-bit 上限,避免单帧过大)
  - [x] 2.2 若 payload 字节数(用 `TextEncoder` 计算 UTF-8 长度,中文 3 字节)≤ PASTE_CHUNK → 原路径直接发送;否则循环:`for (let i = 0; i < bytes.length; i += PASTE_CHUNK) { ws.send(bytes.subarray(i, i + PASTE_CHUNK)); await new Promise(r => setTimeout(r, 50)); }`(50ms 节流,防止瞬间灌满对端缓冲;PTY 会话发二进制帧,agent 会话发文本帧——保持原路径的帧类型)
  - [x] 2.3 发送前/中给 UI 一个提示(如状态栏"正在粘贴 xx KB…"),完成后提示消失;失败(ws 关闭)中断循环
  - [x] 2.4 保持 bracketed paste 包装逻辑不变(只对包装后的完整 payload 分段)

- [x] **Step 3: 服务端合流测试(可选但推荐)** — `test/test_server.py`
  - [x] 3.1 追加 WS 集成用例 `test_ws_input_chunked_reassembly`:连 WS,把一段 64KB 文本按 32KB 分两帧(中间夹一个 resize 帧)发送,断言服务端按序把两段拼成完整文本写入会话输入(参照现有 WS 测试的收帧/断言样板)
  - [x] 3.2 运行 `cd /root/webpty && .venv/bin/python -m unittest test.test_server.ServerIntegrationTest.test_ws_input_chunked_reassembly -v`,期望绿(若服务端本来就能合流,此用例是保护性回归;若不能,先修 server 侧:检查 `_ws_session` 的 recv 循环 877-900 行是否逐帧 write,分片合流应已天然成立)
  - [x] 3.3 若 3.2 失败:在 `_ws_session` 的 recv 分支(877-900 行)确认二进制帧直接 `self.sessions.write(sid, payload)`、文本帧走 `json.loads` 失败后同样 `write`——补一个"帧间合流"的纯函数测试(如把 `(opcode, payload)` 列表喂给一个提取输入的 helper,断言输出拼接正确),把合流语义固化为单元测试再重跑 3.2

- [x] **Step 4: 手工冒烟**
  - [x] 4.1 手机浏览器(或 DevTools 手机模拟)打开 webpty:软键盘 Enter 发送消息、Shift+Enter 换行、粘贴中文多行文本 → 无截断无乱码(人工)
  - [x] 4.2 粘贴 >32KB 文本(如大 log),观察分段提示、终端完整接收(人工)
  - [x] 4.3 全量测试 `cd /root/webpty && .venv/bin/python -m unittest discover -s test` 全绿后提交:`git add public/app.js test/test_server.py && git commit -m "feat(ui): chunked large paste over WS for mobile"`

- [x] **Task 9 验收清单**
  - [x] >32KB 粘贴分段发送,无截断无乱码
  - [x] 软键盘 Enter/blur 提交行为无回归(既有逻辑未破坏)
  - [x] WS 分片合流有测试保护

---

### Task 10: 单用户安全默认值(C3)

**Files:**
- Edit: `src/config.py`(默认 `bindHost`,124 行附近)
- Edit: `src/server.py`(启动硬失败,`main()` 1045 行起、WARNING 1052-1053 行)
- Edit: `install.sh`(49 行 `RUN_USER` 默认 root;170-186 行用户检查)
- Edit: `README.md`(安全节)
- Test: `test/test_config.py`、`test/test_gate_boundary.py`(或 test_server 启动样板)

**背景(为什么做):** 默认 `bindHost: "0.0.0.0"` + 空 `authToken` + 空 `allowedLogins` 时,`auth.py:124-126` 的 `gate-disabled` 放行**所有**请求;`install.sh:49` 默认以 root 运行。单用户个人部署若直接暴露局域网/公网,即零凭据远程 root RCE(上轮审查结论)。本 Task 把"默认不安全"改成"默认安全、显式放开"。

**决策(实施前与用户确认,默认方案):** 空 gate(authToken 与 allowedLogins 均为空)时,若 `bindHost` 不是回环地址(`127.0.0.1`/`::1`/`localhost`),启动**硬失败**并给出明确报错与两种解法;用户显式设了 token 或绑回环,则照常启动。**不做**静默改绑 127.0.0.1(避免行为突变难排查)。

**Steps:**

- [x] **Step 1: 决策确认与失败测试(红)**
  - [x] 1.1 **现状核查**:2026-08-08 工作区未提交改动**已实现**"默认 bindHost=127.0.0.1"(config.py)与"空 gate + 非回环 → 启动拒绝"(server.py main 段)。与用户确认:保留工作区方案(推荐,即本 Task 目标),本 Task 剩余工作收敛为——install.sh 非 root 默认、README 迁移指引、`validate_boot` 测试补全(工作区是内联实现,可抽出为可测函数或直接对启动行为写集成测试)。确认后在本 Task 注释记录结论与日期
  - [x] 1.2 在 `test/test_gate_boundary.py`(或新建 `test/test_boot_gate.py`)追加 `test_boot_refuses_nonloopback_without_gate`:构造 config(authToken="", allowedLogins=[], bindHost="0.0.0.0"),调用启动校验函数(按 2.1 抽出的 `validate_boot(config) -> str | None`,返回错误消息),断言返回非空
  - [x] 1.3 追加 `test_boot_ok_with_token_or_loopback`:同一函数,`authToken="x"` + bindHost="0.0.0.0" → 返回 None;authToken="" + bindHost="127.0.0.1" → 返回 None。运行 1.2/1.3,期望:**FAIL**(现状无校验函数)

- [x] **Step 2: 实现启动校验** — 编辑 `src/server.py`
  - [x] 2.1 新增模块级函数 `def validate_boot(config: dict) -> str | None:`:`gate = config.get("authToken") or config.get("allowedLogins")`;`host = str(config.get("bindHost", "0.0.0.0"))`;若 `not gate and host not in ("127.0.0.1", "::1", "localhost")` → 返回错误消息字符串(内容:`webpty 无访问门禁(authToken/allowedLogins 均为空)却绑定非回环地址 {host}。解法:1) 设置 WEBPTY_AUTH_TOKEN 或 config.authToken;2) 改用 --bind=127.0.0.1 仅本机访问;3) 确认网络环境可信后显式保留 0.0.0.0`);否则返回 None
  - [x] 2.2 `main()`(1045 行起)在 `config = load_config()` 之后立刻:`err = validate_boot(config); if err: print(f"[webpty] ERROR: {err}", flush=True); sys.exit(1)`(顶部确认 `import sys`)
  - [x] 2.3 保留现有 WARNING(1052-1053 行)仅覆盖"有 gate 但提示"的温和场景,或删除由新硬校验替代——**保留**更稳妥(有 token 时的提示仍有价值),注释说明两者分工

- [x] **Step 3: install.sh 非 root 默认**
  - [x] 3.1 编辑 `install.sh:49`:`RUN_USER="${WEBPTY_USER:-root}"` → 交互式安装时默认改为 `webpty` 并自动创建:`if [ "$RUN_USER" = "root" ] && [ -t 0 ]; then read -p "以非 root 用户运行 webpty(默认 webpty,回车确认)? [Y/n]" ...; fi`;非交互(`-t 0` 为假)时维持 root(兼容 CI/现有自动化),在输出中打印黄色提示建议 `--user`
  - [x] 3.2 若选 `webpty` 用户且不存在:`id webpty || useradd --system --home "$SRC_DIR" --shell /usr/sbin/nologin webpty`(先 `chown` 再启动,170-186 行已有 chown 逻辑,确认用户创建在其之前)
  - [x] 3.3 更新 install.sh 顶部的帮助文本(71 行 `--user=U` 说明)与 README 安装节

- [x] **Step 4: README 更新**
  - [x] 4.1 README 安全节(约 275-300 行):新增"默认行为"说明:空 gate + 非回环 bind 启动被拒;给出三种解法(设 token / 绑回环 / 显式 0.0.0.0)
  - [x] 4.2 迁移指引:已部署用户升级后启动失败时的排查步骤(看启动日志 ERROR 行)
  - [x] 4.3 双语段落保持中英对照风格(README 是双语)

- [x] **Step 5: 验证(绿)**
  - [x] 5.1 跑 `cd /root/webpty && .venv/bin/python -m unittest test.test_config test.test_gate_boundary -v`,期望全绿
  - [x] 5.2 跑全量 `cd /root/webpty && .venv/bin/python -m unittest discover -s test`
  - [x] 5.3 手动冒烟:①`WEBPTY_BIND_HOST=0.0.0.0` 且无 token 启动 → 进程退出并打印 ERROR;②`WEBPTY_BIND_HOST=127.0.0.1` 无 token → 正常启动;③设 `WEBPTY_AUTH_TOKEN=test` + 0.0.0.0 → 正常启动。注意用临时 `WEBPTY_DATA_DIR` 避免污染真实配置
  - [x] 5.4 提交:`git add src/server.py src/config.py install.sh README.md test/ && git commit -m "fix(security): refuse non-loopback bind when gate is empty, non-root install default"`

- [x] **Task 10 验收清单**
  - [x] 空 gate + 非回环 bind → 启动拒绝,报错含两种解法
  - [x] 设 token 或回环绑定 → 正常启动(无行为回归)
  - [x] install.sh 交互默认非 root 用户,README 双语说明

---

## 里程碑

1. **M1 预算准确**(Task 1-4):usage_parser 三套格式归一 + cache 读写分价 + 崩溃防护;realtime 增量去重;budget 落盘;actual 费用优先。
   - 验收:`python3 -m unittest discover -s test` 全绿;构造三套真实 agent 日志样本跑 `reconcile`,金额与手算一致;预算重启不丢
2. **M2 流畅快速**(Task 5-7):reconcile 不阻塞事件循环(>20MB 文件跳过);agent 输出无跨块乱码;项目列表 mtime 有 30s 缓存。
   - 验收:全量测试 + reconcile 期间界面不卡顿 + 中文长输出无 `�`
3. **M3 核心体验**(Task 8-10):autostart 会话自动重启(有上限)/挂起检测;移动端大粘贴分段;安全默认值(空 gate 非回环拒启)。
   - 验收:全量测试 + 手机 UA 冒烟 + 启动三场景冒烟

## 风险与取舍

- **增量去重语义依赖工具行为**:若某工具发的是增量而非累计值,Task 2 会少计。对策:按 tool 配置 `usage_mode: "cumulative"|"incremental"`(默认 cumulative,与 codex/reasonix 行为一致);`_last_usage` 状态在进程重启后丢失,首事件按全量记(可接受,重启后首次事件即全量)
- **actual 优先改变汇总口径**:与历史估算数据并存,摘要页标注来源(实际/估算),避免用户困惑
- **Task 10 硬失败是行为变更**:已运行的部署升级后若未设 token 且绑 0.0.0.0 会启动失败——README 迁移指引 + 报错给出两种解法,不做静默降级
- **自动重启可能掩盖配置错误**:连续失败达 max_restarts 即停,且只对 autostart 会话生效,不无限重试
- **不做**:uvloop、跨进程热迁移、多用户鉴权(单用户定位)、预算实时推送(保持轮询)

## 验证总纲(每个 Task 通用)

- 每 Task 结束:`cd /root/webpty && .venv/bin/python -m unittest discover -s test` 全绿(254 + 各 Task 新增)
- 提交粒度:每 Task 一个 commit,消息按 Task 标注
- M1 结束:手工构造日志样本对账(金额与手算一致)
- M2 结束:`bench/ws_throughput.py` 记录对比(reconcile 期间 p95 不劣化)
- M3 结束:真机/模拟器手机浏览器冒烟 + 启动三场景冒烟

