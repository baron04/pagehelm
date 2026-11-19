---
title: 运维与可观测性
description: SQLite 在线一致性备份与隔离恢复、未知写入（INDETERMINATE）对账处置、三层费用治理、无内容泄露 OTLP 遥测与故障排查手册。
---

> **TL;DR：** 生产部署将有状态账本持久化在开启 `WAL` 的 SQLite 中，提供带 SHA-256 校验与 `--writer-isolated` 门禁的离线/在线备份恢复工具；对于超时失联的 `INDETERMINATE` 写入，坚持人工对账绝不自动重放；配合 `TurnBudget` / `DailyAllowance` / `SQLiteSpendCap` 三级费用控制与无业务明文泄露的 OpenTelemetry 导出，保障可运维性与成本边界。

[English](../../security/operations-and-observability.md)

## 一、SQLite 一致性备份、校验与隔离恢复

`web_operation_agent.composition.operations` (`src/web_operation_agent/composition/operations.py`) 负责对会话、写事务日志及费用账本等本地运行态 SQLite 库执行一致性打包与检验：

- **备份包结构**：生成 `0o700` 权限目录与 `manifest.json`（`BACKUP_SCHEMA_VERSION = 1`），记录全部库文件的 SHA-256 摘要，并执行 `PRAGMA quick_check` 完整性检查。
- **不包含凭据与令牌**：备份包只存运行态数据与摘要，绝不包含模型密钥、Cookie 或可重放凭据。
- **强制隔离恢复**：`restore` 只能还原到全新目标目录，严禁就地覆盖正在运行的服务目录，且必须显式传入 `--writer-isolated` 声明写入进程已隔离（历史 `mutation_journal` 只作为对账线索，不作为重放授权）。

```bash
# 检查运行态数据库健康度与结构完整性
.venv/bin/python -m web_operation_agent.composition.operations inspect --help

# 执行一致性快照备份并校验 manifest SHA-256
.venv/bin/python -m web_operation_agent.composition.operations backup --help
.venv/bin/python -m web_operation_agent.composition.operations verify --help

# 在确认写进程已停止后恢复至全新 0o700 目录
.venv/bin/python -m web_operation_agent.composition.operations restore --writer-isolated --help
```

*(对任意子命令追加 `--help` 即可查看适配当前部署路径的参数说明。)*

---

## 二、`INDETERMINATE` 未知写入的排查与人工对账

当单飞写入在执行前向 `SQLiteMutationJournal` 记下 `mutation_claimed`（状态为 `"dispatching"` 或 `"indeterminate"`），而随后的网络请求发生超时或断连时，系统**绝不自动重试**——因为单次只读查询无法证明前一个后台事务是否已经彻底回滚或仍在排他提交。

可通过 `web_operation_agent.business.operations` (`src/web_operation_agent/business/operations.py`) CLI 或在线对账接口安全处理：

1. **查看待对账清单（`list`）**：输出每条未决意图的 `proposal_digest`、当时停留在哪一阶段、已捕获的 `result_digest` 以及原始操作员身份摘要，免去深夜人工拼装协议参数之苦。
2. **按原操作员身份只读核对（`reconcile`）**：发起纯只读核对（保证 `dispatches: 0`、`journal_changed: False`），返回核查结论与是否仍需人工介入（`requires_human`）。
3. **落盘最终人工裁决**：
   - **停机/离线运维 CLI**：
     ```bash
     .venv/bin/python -m web_operation_agent.business.operations list --help
     .venv/bin/python -m web_operation_agent.business.operations resolve --writer-stopped --help
     ```
     *(`resolve` 默认以 dry-run 预览模式执行，只有显式附加 `--apply` 且声明 `--writer-stopped` 时才会修改账本。)*
   - **在线会话 API**：经由授权操作员向 `POST /v1/sessions/<id>/reconciliations` 提交核验结论，将对账结果与原审批记录串联入同一时间线。

---

## 三、优雅停机与健康探针（`/healthz` & `/readyz`）

网关进程在 `gateway.py` 内置免鉴权运维探针，可直接对接容器调度与反向代理健康检查：

| 端点 | 健康返回 | 优雅摘流（`SIGTERM` / 停机排水）表现 |
| --- | --- | --- |
| `GET /healthz` | `200` `{"ok": true, "protocol": 1, "schema_version": 1}` | 进程存活探针 |
| `GET /readyz` | `200` `{"ready": true, "accepting_sessions": true, "session_count": N}` | 进入排空阶段后切换为 HTTP `503`（`"ready": false`, `"accepting_sessions": false`），通知负载均衡停止调度新会话，同时等待在途请求完成或记入干净取消 |

---

## 四、三层成本与配额治理

`src/web_operation_agent/modeling/spend.py` 与 `src/web_operation_agent/execution/budget.py` 构成纵深成本防线：

| 层级 | 组件 | 作用域 | 持久化与边界语义 |
| --- | --- | --- | --- |
| **第一层：单轮预算** | `TurnBudget` | 单次提问（`steps`, `seconds`） | 跨 API 读 / SQL / 浏览器阶段共享原子步数与单调时钟；失败重试同样计入步数 |
| **第二层：操作员日限额** | `DailyAllowance` | 按日 + 按操作员主体现有键 | 派发前检查；**进程重启后从当日持久化账本重建累加值**，防止重启进程变相刷新额度 |
| **第三层：账本预留底线** | `SQLiteSpendCap` + `SpendMeter` | 部署 / 运行级费用总账（默认 `turn_reserve_cny=0.02`） | 调用模型前原子校验最低预留额度，完成后精确记账 Token 与费用 |

---

## 五、无内容泄露 OTLP 遥测（`RuntimeObservability`）

`RuntimeObservability` (`src/web_operation_agent/modeling/observability.py`) 通过后台守护线程（`woa-otel-export`，有界队列深度 `256`）上报纯结构化遥测数据，**绝不附带用户输入原文、Cookie 或业务表格正文**：

- 每个 `run_id` 生成稳定的 SHA-256 Trace/Span ID 与阶段耗时跨度。
- 内置导出结构化指标计数器：
  - `turns`、`failed_turns`
  - `provider_failures`、`provider_deadlines`（同时跟踪连续失败/超时计数）
  - `indeterminate_writes`、`stale_runs_reaped`、`maintenance_failures`
  - `exports_failed`、`alerts_failed`
- 支持配置 `otlp_endpoint`、可选 `alert_webhook` 与 `cost_alert_cny`（正浮点数，上限 `1_000_000`），及时向值守通道触发告警。

---

## 六、单轮链路诊断：`TurnTrace` 与 `trace_replay.html`

- **`TurnTrace`（`execution/turn_trace.py`）**：精确记录单轮 `stage_timings`（`record_stage`）、每个工具耗时与错误码（`record_tool_timing`）、首跳路由类型（`first_route_kind`）、终止原因（`stop_reason`）以及程序化自检结论（`checks`）。落盘前经出站审计与 `url_decode_depth=3` 脱敏复检。
- **`trace_replay.html`（`state/traceview.py`）**：从追加写 `SessionLedger` 生成脱敏静态回放页面，仅投影 `VISIBLE_EVENTS` 白名单事件（含 `approval_requested`、`approval_decided`、`tool_call`、`application_entered`、`action_receipt`、`write_permitted`、`success_claim`、`run_finished`、`product_verification` 等）。

---

## 七、常见故障速查矩阵

| 现象 | 常见根因 | 诊断方法 | 处理方式 |
| --- | --- | --- | --- |
| 初始化 `/bootstrap` 返回 `401` / `403` | 后台登录态过期或反向代理未透传认证头 | 核对 `start.final_statuses` 与网关注入请求头 | 引导操作员重新登录业务系统，并检查反代透传配置 |
| 访问 `/woa/assistant.js` 返回 `404` `runtime_unavailable` | 安装包中缺少构建后的前端运行时产物 | 检查该接口返回的 JSON `code` | 启动网关前先执行 `python scripts/build_runtime.py` 构建前端产物 |
| 标签页操作或审批提交返回 `409` | 发生“复制标签页”触发 Web Locks 会话轮换，或同一 `approval_id` 被重复提交 | 查看 `woa.assistant.tab.v1` 与 `/timeline` 最新事件 | 刷新当前面板对话，或在页面变化后重新确认新的审批卡片 |
| 任务在 `step_budget` / `wall_budget` 提前结束 | 查询条件过宽导致反复分页，或 UI 探索步数过多 | 检查 `TurnTrace` 的 `stage_timings` 与 `stop_reason` | 缩小筛选范围、改用汇总/SQL 工具，或适当调整 `TurnBudget` 配额 |
| 面板/账本出现 `INDETERMINATE` 写记录 | 单飞派发 `mutation_claimed` 后发生超时或网络中断 | 运行 `business.operations list` | 在上游系统中核实实际结果，再执行 `resolve --writer-stopped --apply` 或调用 `POST /reconciliations` |
| `/readyz` 返回 `503` | 进程处于停机排空阶段，或就绪探针判定上游不可用 | 执行 `curl -i /readyz` 查看字段 | 等待在途任务平滑排空，或排查所依赖的模型与业务网关连通性 |

---

## 下一步阅读

- 掌握本地启动参数、环境变量与 CI 分层自检流程：[5 分钟快速启动与运维](../integration/quickstart.md)。
- 查阅完整的配置键、`panel_contract.json` 状态模型与 HTTP/SSE 接口明细：[协议与配置参考](../reference/protocol-and-configuration.md)。
