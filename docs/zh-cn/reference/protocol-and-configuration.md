---
title: API 与协议配置参考
description: pagehelm.toml 与 .env 完整键约束、三档 mode 开关映射、网关 HTTP/SSE 端点、panel_contract.json v2 与核心运行上限。
---

> **概要：** 汇总 PageHelm 全部对外配置与协议参考：`pagehelm.toml` 类型约束与 `.env` 变量、三种 `mode` 的底层布尔展开表、`/v1/sessions/<id>/` 网关 HTTP/SSE 端点、`panel_contract.json` v2 状态与载荷定义，以及各组件运行上限。

[English](../../reference/protocol-and-configuration.md)

## `pagehelm.toml` 字段规范（`resolve_pagehelm_environment`）

`parse_pagehelm_toml()` 基于 Python 标准库 `tomllib` 做强类型检查，拒绝未知键、布尔冒充数值、非 HTTPS 远程地址以及任何疑似凭据的字段名（`*api_key*`、`*secret*`、`*token*`、`*password*`）：

| 字段 | 类型与约束 | 默认值 / 行为 |
| --- | --- | --- |
| `provider` | `"deepseek"` \| `"qwen"` \| `"openai"` | 自动推导官方 `base_url`、已登记计价的默认 `model` 与 `.env` 密钥查找顺序 |
| `mode` | `"read-only"` \| `"assisted-draft"` \| `"verified-operate"` | `./local` 默认 `"verified-operate"`；云端生产宿主默认 `"read-only"` |
| `model` | 非空字符串（≤ 120 字符），须已在 `policies/prices.json` 登记 | 随 `provider` 默认为 `deepseek-flash`、`qwen3.8-flash` 或 `gpt-6.1-sol` |
| `base_url` | `https://...`（或回环开发地址 `http://localhost|127.0.0.1`），禁带账号/查询参数 | 随 `provider` 默认为对应官方端点 |
| `daily_allowance_cny` | 有限正数 `> 0` 且 `<= 1,000,000` | 单用户每日 CNY 消费上限（云端默认 `5.0`，`./local` 默认 `20.0`） |
| `daily_total_cap_cny` | 有限正数 `> 0` 且 `<= 1,000,000` | 全进程每日原子预留消费上限（未设则不开启） |
| `monthly_total_cap_cny` | 有限正数 `> 0` 且 `<= 1,000,000` | 全进程每月原子预留消费上限（未设则不开启） |
| `max_sessions` | 整数 `1 .. 100,000` | 单宿主默认最多 `300` 个活跃会话 |
| `turn_timeout_s` | 整数 `1 .. 3,600` | 单回合执行墙钟预算（默认 `120` 秒，不含人工等待审批时间） |

### `.env` 环境变量

| 变量名 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY` / `QWEN_API_KEY`、`OPENAI_API_KEY` | 按 `pagehelm.toml` 的 `provider` 自动选用对应模型密钥（推荐 1–3 行） |
| `AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL`、`AGENT_PROVIDER` | 显式覆盖或未启用 `pagehelm.toml` 时的兼容配置写法 |
| `WOA_MODE` / `WOA_NATIVE_WRITES` | 环境变量覆盖（`read-only` / `assisted-draft` / `verified-operate`，或 `true` / `false`；同时设置且矛盾时拒绝启动） |

### 模式展开对照表

| `mode` 取值 | `native_page_admission` | `native_writes` | `native_submit` | `native_write_preparation` | `native_edit_prefill` |
| --- | --- | --- | --- | --- | --- |
| `read-only` | `read-only-window` | `false` | `false` | `false` | `false` |
| `assisted-draft` | `read-only-window` | `false` | `false` | `true` | `true` |
| `verified-operate` | `read-only-window` | `true` | `true` | `false` | `true` |

## 网关 HTTP 与 SSE 端点（`EmbeddedGateway`）

浏览器在通过宿主 `/bootstrap` 获取会话令牌后，所有通信经网关基地址（默认 `/woa-agent`）传输：

| 端点 | 方法 | 作用 |
| --- | --- | --- |
| `/healthz`、`/readyz` | `GET` | 存活探测与排空感知就绪检查（SIGTERM 排空期间返回 `503`） |
| `/v1/runtime/assistant.js` | `GET` | 带 `ETag` / `304` / `.gz` 的前后端同构 IIFE 包（未构建返回 `404 runtime_unavailable`） |
| `/v1/sessions/<id>/hello` | `POST` | 校验 `ClientHello` 来源与身份摘要，签发租约 `lease_id` 与面板配置 |
| `/v1/sessions/<id>/observations` | `POST` | 上报结构化 `PageObservation`（视口最多 200 控件、优先活跃弹窗、附上下可滚余量） |
| `/v1/sessions/<id>/messages` | `POST` | 提交幂等用户任务（`message_id` 去重，繁忙返回 `429`） |
| `/v1/sessions/<id>/claims`、`results` | `POST` | 认领待执行浏览器动作并上报 `ActionReceipt`（含落定观察摘要） |
| `/v1/sessions/<id>/approvals` | `POST` | 提交鲜活审批结果（确认、取消，或支持时“只填到表单，我自己提交”） |
| `/v1/sessions/<id>/cancellations` | `POST` | 取消运行中或迟到预取消任务（支持提交准备期竞态预取消） |
| `/v1/sessions/<id>/reconciliations` | `POST` | 对 `INDETERMINATE` 未决写入发起同账号只读对账 |
| `/v1/sessions/<id>/events`、`timeline` | `GET` | 基于 `Last-Event-ID` 的 SSE 事件流与面板 JSON 时间线投影 |

## `panel_contract.json`（Protocol v2）与运行上限

服务端与前端共用 `src/web_operation_agent/web_runtime/panel_contract.json` 校验所有状态与上限：

- **默认品牌文案（`label_keys`）：** `product: "PageHelm"`、`emptyIntro: "你好，我是 PageHelm"`；DOM 类名与变量前缀继续保持 `woa-*`。
- **进度与终态枚举：** 非终态包括 `waiting_for_runtime`、`session_recovered`、`message_accepted`、`new_conversation`、`agent_started`、`step`、`approval_recorded`、`cancelling`；终态包括 `needs_input`、`finished`、`refused`、`cancelled`、`indeterminate`、`failed`、`task_failed`。未登记状态严格拒绝投影。
- **v2 结构化载荷：** `table`（`StructuredTable` 供 `<DataTable>` 渲染）、`input_options`（2–4 个 `ClarificationOption` 消歧胶囊）、`handoff`（自动脱敏 `completed_fields`、`pending_fields`、`restricted_controls`、`can_undo`）以及本地 `local-dev` 专属 `debug_trace`。
- **硬性上限：** `composer_max_chars = 2000`、`history_page_size = 100`、`result_max_chars = 20000`、单页观察至多 `200` 个交互控件、单次行表投影至多 `5,000` 行（SQL 输出至多 `200` 行），芋道行表执行器 `model_steps = 33`（基础执行器为 `24`）。
