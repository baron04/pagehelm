---
title: 执行引擎与运行时
description: 深入解析 ModelDrivenTurnExecutor、TurnBudget、并发读取有序提交、Decimal 汇总计算、TurnTables 临时 SQL 与有界 DOM 稳定等待机制。
---

> **TL;DR：** 每次用户请求均在共享预算的单轮循环（`ModelDrivenTurnExecutor` + `TurnHarness` + `TurnBudget`）内运行：只读请求可并发预取并按确定性顺序提交至主循环；数值汇总由确定性 `Decimal` 引擎（`read_summary`）与进程内只读临时 SQL（`TurnTables`）完成；浏览器操作则受 200 元素快照上限、噪声过滤 `PageSettleWatch` 以及可撤销表单预填保护。

[English](../../architecture/execution-and-runtime.md)

## 一、单轮编排与共享预算（TurnBudget）

```text
用户输入目标
   |
   v
TurnHarness（会话绑定、动作围栏、审批监视器、链路追踪）
   |
   +--> TurnBudget(steps, seconds)  <-- 单一共享步数计数与单调时钟
   |       - 每次模型请求（含重试）消耗 1 步
   |       - API 读取 -> SQL 分析 -> 页面跳转，阶段切换绝不重置预算
   |
   v
ModelDrivenTurnExecutor 主循环
   |---> 并发纯读 Worker ---------> 主线程有序提交 (t1, ev_*)
   |---> 受控浏览器 UI 动作 ------> PageSettleWatch（80ms 静默 / 最长 2s）
   |---> finish(table_ref=...) ---> Shadow DOM 面板渲染脱敏 <DataTable>
   |---> finish(status="need_input", options=[...]) ---> 一键追问选项胶囊
```

不同于子代理各自持有独立超时的多代理链路，`TurnBudget` (`src/web_operation_agent/execution/budget.py`) 对整轮交互施加单一原子步数与挂钟超时约束：

- 每次模型调用前通过 `TurnBudget.reserve()` 检查取消信号、剩余秒数与步数上限 `steps`。
- 读取工具的检查点（`data_tools.py` 的 `stopped_read`）实时读取同一 `TurnBudget`，触发 `"user_cancelled"`、`"step_budget"`、`"token_budget"` 或 `"wall_clock_budget"` 时立即有序收口。

---

## 二、并发读取与主线程确定性提交

当模型在同一步并发发起多路只读查询（或适配器通过类似 `_ConcurrentReadProcedure` 的过程并发分页拉取切片）时，架构坚持“并发预取、单线程提交”：

- Worker 仅负责发起网络请求、校验响应边界并做脱敏准备，**绝不直接写入**共享的 `TurnTables` 实例或知识/回执表。
- 全部 Worker 返回后，由主执行线程严格按照调用顺序注册表格句柄（`t1`、`t2`…）并签发只读凭证（`ev_<hex12>_<n>`），确保回放与回归测试结果完全可复现。

---

## 三、服务端确定性汇总引擎（`read_summary.py`）

跨数百条记录进行计数、求和或区间筛选绝不能依赖大模型在自然语言中做心算，也不能依靠浏览器逐页翻页累加。`src/web_operation_agent/execution/read_summary.py` 在同主体读取结果上执行闭集 `Decimal` 聚合：

| 约束或能力 | 已核验实现行为 |
| --- | --- |
| 行数与输出上限 | `MAX_ROWS = 5000`，单次摘要最大 `MAX_RESULT_CHARS = 100_000` 字符 |
| 支持的列类型 | `text`、`number`、`boolean`、`date`、`datetime`、`relation` |
| 闭集筛选算子 | `=`、`!=`、`contains`、`prefix`、`in`、`>`、`>=`、`<=`、`<`、`between`、`empty`、`not_empty` |
| 精确度量原语 | `_MEASURES = ("count", "count_distinct", "sum", "min", "max")`（采用 `Decimal` 精确计算；**不包含 `avg`**——求均值或多表关联请使用 `TurnTables` SQL） |
| 货币与单位防伪造 | 若金额列未经约定审定，标记 `SummaryColumn.unverified_unit=True`，系统仅按原数值展示，绝不私自猜测单位或做汇率换算 |
| 分页截断防误报 | 当上游读取标记 `truncated=True` 时，结果核验禁止将局部切片直接冒充全集“总数”，必须继续拉全或明确注明局部覆盖范围 |

---

## 四、单轮临时 SQL（`TurnTables`）与 `<DataTable>`

针对多接口关联、分组排名、均值统计（`AVG`）或基于当前轮已读取行表的二次切片分析，适配器可绑定单轮内存 SQLite 引擎 `TurnTables`：

- **强隔离与自动销毁**：使用 `sqlite3.connect(":memory:", cached_statements=0)` 且 `SQLITE_LIMIT_ATTACHED = 0`，本轮结束调用 `TurnTables.close()` 立即释放。
- **硬容量与时间上限**：
  - 单轮最多建表 `MAX_TABLES = 36` 张（表名 `t1`, `t2`…；保存的查询结果引用 `q1`, `q2`…）
  - 单表上限 `MAX_ROWS = 5000` 行、`MAX_COLUMNS = 128` 列
  - SQL 返给模型的预览上限 `MAX_SQL_ROWS = 200` 行 / `MAX_SQL_CHARS = 14000` 字符，单条 SQL 执行限时 `1.5` 秒
- **严格 SQL 白名单**：仅放行单条 `SELECT` 或 `WITH` 语句（否则报 `sql_refused`），在保留字符串字面量的前提下剥离注释，仅白名单允许纯函数（如 `abs`、`avg`、`coalesce`、`count`、`date`、`datetime`、`dense_rank`、`group_concat`、`json_extract`、`lag`、`lead`、`rank`、`round`、`row_number`、`strftime`、`sum` 等）。
- **默认脱敏入表与导出**：建表时自动跳过敏感命名列并用 `SessionHandoffPolicy` 清洗内容。

### `finish(table_ref=...)` 零转抄结构化表格直出

当查询结果包含大量明细行时，模型只需向 `finish` 传入 `table_ref="t1"` 或 `"q1"`，无需把表格重抄进回复正文中：

1. 服务端通过 `TurnTables.export_table(ref)` 导出已脱敏数据集（导出上限：`EXPORT_MAX_COLS = 24`、`EXPORT_MAX_ROWS = 2000`、`EXPORT_CELL_CHARS = 300`、`EXPORT_TITLE_CHARS = 120`）。
2. 客户端 Shadow DOM 面板使用原生 `<DataTable>` 组件直接渲染，支持即时筛选过滤，并与服务端签发的证据回执（`ev_*`）绑定。

---

## 五、浏览器 Runtime：DOM 快照、稳定等待与组件集成

### 有界页面观测（`MAX_OBSERVATION_ELEMENTS = 200`）

`src/web_operation_agent/web_runtime/browser_contract.json` 约定单帧观测最多采集 `200` 个交互元素。当页面弹出激活的对话框或抽屉（Dialog / Drawer）时，`PageController` 优先采集弹窗内部控件，再补充底层背景元素，保证弹窗表单字段完整可见。

### 去噪页面稳定监测（`pageSettle.ts`）

现代单页应用持续存在心跳请求、SSE 流或无实质变化的属性刷新。`frontend/src/runtime/pageSettle.ts` 中的 `PageSettleWatch` 以确定性上限屏蔽干扰：

- `DEFAULT_SETTLE_QUIET_MS = 80` ms：无实质 DOM 变更即认定稳定
- `DEFAULT_SETTLE_MAX_WAIT_MS = 2000` ms：单步动作最长等待时间
- `DEFAULT_LONG_TAIL_REQUEST_MS = 1500` ms：超过 1.5 秒的历史长尾网络请求不再阻塞观测
- **自动忽略项**：新旧值相同的空属性变更（`oldValue === currentValue`）、助手面板 Shadow DOM 内部变更、`#playwright-highlight-container` 高亮容器、`/woa/` 内部端点以及 `/events` SSE 长连接。

此外，当点击、选择或路由跳转刚稳定完成时，`src/web_operation_agent/execution/loop_guard.py` 会按 `obs.epoch` 与 `page_fingerprint(obs)` 缓存 `settled_action_observation`。若通道状态尚未陈旧，执行器直接复用该稳定观测，省去一次多余的模型 `observe` 往返。

### UI 组件库适配与安全预填交接

通用 Runtime 内核保持零组件库耦合；针对 Vue / Element Plus 的可复用辅助函数统一抽离在 `frontend/src/integrations/element-plus/` 下：

- `date-picker.js`（如 `isElDateRangeInput` 日期区间控件识别）
- `form.js`（标签提取，限制表单遍历边界 `depth: 8`, `nodes: 6000`, `bytes: 200000`）
- `select.js`（`readElSelectVisibleCandidates` 可见下拉选项采样与内部输入防重入）
- `table.js`（表头与单元格读取）
- `vue.js`（组件实例绑定辅助）

表单预填完成后，浏览器向服务端返回的 `ActionReceipt` 精确区分字段状态：

- `applied_fields` 与 `previous_fields`（支持用户点击面板内由 `undoFormFill(turnId)` 驱动的一键撤销）
- `rejected_fields` 与 `candidate_labels`（枚举项无法匹配时如实报告可用选项）
- `restricted_fields`（遇到密码或密钥等受限控件时主动跳过并聚焦该输入框，由操作员亲手键入）

若遇到多个同名记录等歧义情境，模型调用 `finish(status="need_input", options=[...])` 在输入区上方渲染已验证的可点选胶囊（`input_options`），不盲目代作决定。
