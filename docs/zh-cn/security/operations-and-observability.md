---
title: 部署、运维与排障
description: 线上发布 ./deploy、优雅停机与探针、SQLite WAL 一致性备份与恢复、INDETERMINATE 人工对账、三层预算控制、OTLP 遥测与排障。
---

本页覆盖 PageHelm 的线上部署与日常运维：发布脚本、健康探针与停机排空、SQLite 一致性备份与隔离恢复、未知状态写入（`INDETERMINATE`）对账、三层费用上限、无内容泄露遥测及常见故障处理。

## 一、线上发布（`./deploy`）

云端环境默认以 `read-only` 模式运行（关闭写入、编辑预填与用户态源码检索），静态资源与代理层额外配置只读 HTTP 方法围栏。

```bash
cp deploy.example.toml deploy.local.toml   # 填写 ssh_target 与 public_origin（已忽略）
./deploy check                             # 只读检查本机产物与远程主机前置条件
./deploy                                   # 构建前端与 wheel、上传发布、健康检查并切换 current
./deploy status                            # 查看远程 release.json 并比对线上 assistant.js 摘要
```

## 二、优雅停机与健康探针（`/healthz` & `/readyz`）

网关进程在 `gateway.py` 内置免鉴权探针，可直接接入容器编排或反向代理：

| 端点 | 正常返回 | 收到 `SIGTERM` 进入排空后的表现 |
| --- | --- | --- |
| `GET /healthz` | `200` `{"ok": true, "protocol": 1, "schema_version": 1}` | 进程存活探针 |
| `GET /readyz` | `200` `{"ready": true, "accepting_sessions": true, "session_count": N}` | 返回 HTTP `503`（`ready: false`, `accepting_sessions: false`），停止接收新回合，等待在途写入派发与回读完成（上限 `shutdown_drain_timeout_s`）后退出 |

## 三、SQLite 一致性备份与隔离恢复

有状态账本（会话、写事务日志、费用账本）保存在开启 `WAL` 的 SQLite 数据库中。**切勿直接用 `cp` 复制运行中的 `-wal` 文件**。请使用 `web_operation_agent.composition.operations`（`src/web_operation_agent/composition/operations.py`）：

- **备份包结构**：输出 `0o700` 权限目录与 `manifest.json`（`BACKUP_SCHEMA_VERSION = 1`），记录全部库文件的 SHA-256 摘要并执行 `PRAGMA quick_check`。
- **凭据排除**：备份仅包含运行态账本与校验摘要，不包含模型密钥、Cookie 或可重放凭据。
- **隔离恢复**：`restore` 只能还原到全新目标目录，不能覆盖正在运行的服务目录，且必须显式传入 `--writer-isolated` 确认写入进程已隔离（历史 `mutation_journal` 仅作对账线索，不用于重放写入）。

```bash
.venv/bin/python -m web_operation_agent.composition.operations inspect --help
.venv/bin/python -m web_operation_agent.composition.operations backup --help
.venv/bin/python -m web_operation_agent.composition.operations verify --help
.venv/bin/python -m web_operation_agent.composition.operations restore --writer-isolated --help
```

## 四、`INDETERMINATE` 未知写入的人工对账

单飞写入在 `SQLiteMutationJournal` 记下 `mutation_claimed`（状态为 `dispatching` 或 `indeterminate`）后，若外部请求超时或连接中断，系统**不自动重试**——因为单次只读查询无法证明前一次后台事务是否仍在提交或已回滚。

通过 `web_operation_agent.business.operations`（`src/web_operation_agent/business/operations.py`）CLI 或在线接口完成对账：

1. **查看待对账记录（`list`）**：输出每条未决记录的 `proposal_digest`、停滞阶段、已捕获的 `result_digest` 与原操作员身份摘要。
2. **只读核对（`reconcile`）**：以原操作员身份发起纯只读核对（`dispatches: 0`、`journal_changed: False`），返回核对结果与是否仍需人工介入（`requires_human`）。
3. **登记最终裁决**：
   - **停机/离线运维 CLI**（默认 dry-run 预览，显式传入 `--apply` 与 `--writer-stopped` 才修改账本）：
     ```bash
     .venv/bin/python -m web_operation_agent.business.operations list --help
     .venv/bin/python -m web_operation_agent.business.operations resolve --writer-stopped --help
     ```
   - **在线会话 API**：由授权操作员向 `POST /v1/sessions/<id>/reconciliations` 提交核验结论，将结果记入同一会话时间线。

## 五、三层费用与配额控制

`src/web_operation_agent/modeling/spend.py` 与 `src/web_operation_agent/execution/budget.py` 提供三层独立限额：

| 层级 | 组件 | 作用域 | 持久化与边界行为 |
| --- | --- | --- | --- |
| 单轮预算 | `TurnBudget` | 单次提问（`steps`、`seconds`） | 跨 API 读、SQL、浏览器阶段共享原子步数与单调时钟；重试计入步数 |
| 操作员日限额 | `DailyAllowance` | 按日 + 按操作员主体 | 派发前校验；进程重启后从当日持久化账本重建累计值 |
| 账本预留底线 | `SQLiteSpendCap` + `SpendMeter` | 部署/运行级总账（默认 `turn_reserve_cny = 0.02`） | 调用模型前原子预留最低额度，结束后按实际 Token 记账 |

## 六、无内容泄露遥测与链路诊断

### OTLP 遥测（`RuntimeObservability`）

`src/web_operation_agent/modeling/observability.py` 通过后台守护线程（`woa-otel-export`，队列深度上限 `256`）导出结构化遥测数据，不附带用户输入原文、Cookie 或业务表格正文：

- 每个 `run_id` 生成稳定的 SHA-256 Trace/Span ID 与阶段耗时跨度。
- 内置计数器：`turns`、`failed_turns`、`provider_failures`、`provider_deadlines`（含连续失败/超时计数）、`indeterminate_writes`、`stale_runs_reaped`、`maintenance_failures`、`exports_failed`、`alerts_failed`。
- 支持配置 `otlp_endpoint`、`alert_webhook` 与 `cost_alert_cny`（正浮点数，上限 `1_000_000`）。

### 单轮诊断（`TurnTrace` 与 `trace_replay.html`）

- **`TurnTrace`（`execution/turn_trace.py`）**：记录 `stage_timings`、`tool_timings`、首跳路由类型（`first_route_kind`）、终止原因（`stop_reason`）与程序自检结果（`checks`），落盘前执行出站审计与 `url_decode_depth=3` 脱敏复检。
- **`trace_replay.html`（`state/traceview.py`）**：从追加写 `SessionLedger` 生成静态脱敏回放页，仅投影 `VISIBLE_EVENTS` 白名单事件（如 `approval_requested`、`approval_decided`、`tool_call`、`application_entered`、`action_receipt`、`write_permitted`、`success_claim`、`run_finished`、`product_verification`）。

## 七、常见故障排查

| 现象 | 常见原因 | 排查方式 | 处理建议 |
| --- | --- | --- | --- |
| `/bootstrap` 返回 `401` / `403` | 登录态过期，或反代未透传认证头 | 核对 `start.final_statuses` 与反代请求头 | 重新登录宿主系统，检查反代透传配置 |
| `/v1/runtime/assistant.js` 返回 `404`（`runtime_unavailable`） | 缺少构建后的前端运行时产物 | 查看响应 JSON 中的 `code` | 启动前执行 `python scripts/build_runtime.py` |
| 动作或审批提交返回 `409` | 复制标签页触发 Web Locks 轮换，或重复提交同一 `approval_id` | 检查 `woa.assistant.tab.v1` 与 `/timeline` 最新事件 | 刷新面板对话，或基于最新页面重新确认审批卡 |
| 任务在 `step_budget` / `wall_budget` 提前终止 | 查询范围过宽反复分页，或 UI 探索步数过多 | 查看 `TurnTrace` 的 `stage_timings` 与 `stop_reason` | 缩小筛选条件、使用汇总/SQL 工具，或调大预算 |
| 账本出现 `INDETERMINATE` 记录 | `mutation_claimed` 后外部调用超时或断连 | 运行 `business.operations list` | 在上游业务系统核实结果后执行 `resolve --writer-stopped --apply` 或调用 `/reconciliations` |
| `/readyz` 返回 `503` | 进程正在停机排空，或上游依赖不可用 | 执行 `curl -i /readyz` 查看字段 | 等待在途请求完成，或排查模型与业务网关连通性 |
