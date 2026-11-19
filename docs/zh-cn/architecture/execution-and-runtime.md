---
title: 执行引擎与浏览器运行时
description: 单轮预算 TurnBudget、并发读取有序提交、Decimal 汇总引擎、TurnTables 临时 SQL 与浏览器稳定等待机制。
---

每次用户请求在共享预算的单轮循环（`ModelDrivenTurnExecutor` + `TurnHarness` + `TurnBudget`）内运行。只读请求可并发预取并按确定性顺序提交到主循环，数值统计由服务端 `Decimal` 引擎或临时只读 SQL 完成，页面交互受有界快照与去噪稳定监测约束。

## 一、单轮编排与共享预算（`TurnBudget`）

```text
用户输入目标
   |
   v
TurnHarness（会话绑定、动作围栏、审批监视器、链路追踪）
   |
   +--> TurnBudget(steps, seconds)  <-- 单一共享步数计数与单调时钟
   |       - 每次模型请求（含重试）消耗 1 步
   |       - API 读取 -> SQL 分析 -> 页面跳转，阶段切换不重置预算
   |
   v
ModelDrivenTurnExecutor 主循环
   |---> 并发纯读 Worker ---------> 主线程按序提交 (t1, ev_*)
   |---> 受控浏览器动作 ----------> PageSettleWatch（80ms 静默 / 最长 2s）
   |---> finish(table_ref=...) ---> Shadow DOM 面板渲染 <DataTable>
   |---> finish(status="need_input", options=[...]) ---> 可点选消歧选项
```

`TurnBudget`（`src/web_operation_agent/execution/budget.py`）对整轮交互施加单一原子步数与挂钟时间上限（不计人工等待审批的时间）：

- 每次模型调用前通过 `TurnBudget.reserve()` 检查取消信号、剩余时间与剩余步数。
- 读取工具检查点（`data_tools.py` 的 `stopped_read`）读取同一 `TurnBudget`，触发 `user_cancelled`、`step_budget`、`token_budget` 或 `wall_clock_budget` 时有序终止。

## 二、并发读取与主线程确定性提交

当模型在同一步发起多路只读查询，或适配器通过并发分页拉取切片时，执行引擎采用“并发预取、主线程按序提交”：

- Worker 仅负责发起只读请求、校验响应边界并做脱敏准备，不直接写入共享的 `TurnTables` 或回执表。
- 所有 Worker 返回后，主线程严格按调用顺序注册表句柄（`t1`、`t2`…）并签发只读凭证（`ev_<hex12>_<n>`），确保回放与回归测试结果可复现。

## 三、服务端确定性汇总（`read_summary.py`）

`src/web_operation_agent/execution/read_summary.py` 在同账号读取结果上执行闭集 `Decimal` 聚合，避免让模型在文本中自行累加或通过翻页统计：

| 约束或能力 | 实现行为 |
| --- | --- |
| 行数与输出上限 | `MAX_ROWS = 5000`，单次摘要上限 `MAX_RESULT_CHARS = 100_000` 字符 |
| 支持列类型 | `text`、`number`、`boolean`、`date`、`datetime`、`relation` |
| 筛选算子 | `=`、`!=`、`contains`、`prefix`、`in`、`>`、`>=`、`<=`、`<`、`between`、`empty`、`not_empty` |
| 度量原语 | `count`、`count_distinct`、`sum`、`min`、`max`（`Decimal` 精确计算；均值或多表关联请使用 `TurnTables` SQL） |
| 单位保护 | 金额列未经约定审定时标记 `SummaryColumn.unverified_unit=True`，按原值展示，不猜测单位或换算汇率 |
| 截断保护 | 上游返回 `truncated=True` 时，禁止将局部切片报告为全集总数，须继续拉全或明确注明局部范围 |

## 四、单轮临时 SQL（`TurnTables`）与 `<DataTable>`

需要多表关联、分组排名、均值统计（`AVG`）或二次切片分析时，适配器可绑定单轮内存 SQLite 引擎 `TurnTables`：

- **隔离与生命周期**：使用 `sqlite3.connect(":memory:", cached_statements=0)` 且设置 `SQLITE_LIMIT_ATTACHED = 0`，本轮结束后调用 `TurnTables.close()` 立即释放。
- **容量与超时上限**：详见 [配置与通信协议](../reference/protocol-and-configuration.md)（单轮最多 36 张表、单表 5000 行 / 128 列，预览上限 200 行 / 14000 字符，单条 SQL 超时 1.5 秒）。
- **只读白名单**：仅允许单条 `SELECT` 或 `WITH` 语句（其他语句返回 `sql_refused`）；保留字符串字面量的前提下剥离注释，仅白名单放行纯函数（`abs`、`avg`、`coalesce`、`count`、`date`、`datetime`、`dense_rank`、`group_concat`、`json_extract`、`lag`、`lead`、`rank`、`round`、`row_number`、`strftime`、`sum` 等）。
- **脱敏入表与导出**：建表时自动跳过敏感命名列，并用 `SessionHandoffPolicy` 清洗内容。

### `finish(table_ref=...)` 结构化表格直出

明细行数较多时，模型在 `finish` 中传入 `table_ref="t1"` 或 `"q1"`，不必在回复正文中手抄表格：

1. 服务端通过 `TurnTables.export_table(ref)` 导出已脱敏数据集（上限见参考手册）。
2. 客户端 Shadow DOM 面板直接用 `<DataTable>` 渲染，支持即时过滤，并与服务端证据回执（`ev_*`）绑定。

## 五、浏览器 Runtime：快照、稳定等待与表单交接

### 有界页面观测

`src/web_operation_agent/web_runtime/browser_contract.json` 限定单帧最多采集 `200` 个交互元素。当页面存在激活的对话框或抽屉时，`PageController` 优先采集弹窗内部控件，再补充背景元素。

### 去噪页面稳定监测（`pageSettle.ts`）

单页应用通常伴随心跳请求、SSE 连接或无实质变化的属性刷新。`frontend/src/runtime/pageSettle.ts` 中的 `PageSettleWatch` 通过固定阈值过滤噪声：

- `DEFAULT_SETTLE_QUIET_MS = 80` ms：连续无实质 DOM 变更即视为稳定。
- `DEFAULT_SETTLE_MAX_WAIT_MS = 2000` ms：单步动作最长等待时间。
- `DEFAULT_LONG_TAIL_REQUEST_MS = 1500` ms：超过 1.5 秒的长尾请求不再阻塞观测。
- **自动忽略项**：新旧值相同的空属性变更、助手面板 Shadow DOM 内部变更、`#playwright-highlight-container` 高亮容器、`/woa/` 内部端点与 `/events` SSE 连接。

当点击、选择或路由跳转刚稳定完成时，`src/web_operation_agent/execution/loop_guard.py` 会按 `obs.epoch` 和 `page_fingerprint(obs)` 缓存 `settled_action_observation`。通道未陈旧时直接复用，减少一次多余的 `observe` 往返。

### 表单预填回执与撤销

表单预填完成后，浏览器回传的 `ActionReceipt` 区分三类字段状态：

- `applied_fields` 与 `previous_fields`：记录已填值与原值，供面板内 `undoFormFill(turnId)` 一键还原。
- `rejected_fields` 与 `candidate_labels`：枚举项未匹配时返回实际可用选项。
- `restricted_fields`：密码、密钥或文件上传控件跳过填写并将焦点移至该控件，由操作员手动输入。
