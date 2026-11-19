---
title: API 与协议配置参考
description: pagehelm.toml 与 .env 完整约束、三档 mode 映射、/v1/sessions/* HTTP/SSE 线协议 JSON 示例、panel_contract.json v2 与硬性运行上限。
---

> **概要：** 本文汇总 PageHelm 对外配置规范、HTTP/SSE 传输协议示例、`panel_contract.json`（`protocol: 2`）、`presentation.json` UI 安全常数以及核心与适配器的硬性运行边界。

[English](../../reference/protocol-and-configuration.md)

## 一、配置规范（`pagehelm.toml` 与 `.env`）

`resolve_pagehelm_environment()` 将非敏感运行配置与密钥彻底隔离。`parse_pagehelm_toml()` 基于 Python 标准库 `tomllib` 做强类型检查，拒绝未知字段、布尔冒充数字、非 HTTPS 远程地址，以及任何类似凭据的键名（`*api_key*`、`*secret*`、`*token*`、`*password*`）。

### `pagehelm.toml` 字段约束

| 字段 | 类型与约束 | 默认值 / 行为 |
| --- | --- | --- |
| `provider` | `"deepseek"` \| `"qwen"` \| `"openai"` | 自动推导官方 `base_url`、已登记计价的默认 `model` 与 `.env` 密钥查找顺序 |
| `mode` | `"read-only"` \| `"assisted-draft"` \| `"verified-operate"` | `./local` 默认 `"verified-operate"`；云端默认 `"read-only"` |
| `model` | 非空字符串（≤ 120 字符），须已登记在 `policies/prices.json` | 随 `provider` 默认取 `deepseek-flash`、`qwen3.8-flash` 或 `gpt-6.1-sol` |
| `base_url` | `https://...`（或回环开发地址 `http://localhost` / `127.0.0.1`），禁带账号或 query | 随 `provider` 默认取对应官方端点 |
| `daily_allowance_cny` | 有限正数 `> 0` 且 `<= 1,000,000` | 单用户每日 CNY 消费上限（云端默认 `5.0`，`./local` 默认 `20.0`） |
| `daily_total_cap_cny` | 有限正数 `> 0` 且 `<= 1,000,000` | 全进程每日原子预留消费上限（未设不启用） |
| `monthly_total_cap_cny` | 有限正数 `> 0` 且 `<= 1,000,000` | 全进程每月原子预留消费上限（未设不启用） |
| `max_sessions` | 整数 `1 .. 100,000` | 单宿主默认最多 `300` 个活跃会话 |
| `turn_timeout_s` | 整数 `1 .. 3,600` | 单回合执行墙钟预算（默认 `120` 秒，不含人工等待审批时间） |

### `.env` 环境变量

| 变量名 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY` / `QWEN_API_KEY`、`OPENAI_API_KEY` | 按 `pagehelm.toml` 中的 `provider` 自动匹配密钥（推荐仅填 1–3 行） |
| `AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL`、`AGENT_PROVIDER` | 显式覆盖或未启用 `pagehelm.toml` 时的兼容写法 |
| `WOA_MODE`、`WOA_NATIVE_WRITES` | 环境变量覆盖（`read-only` / `assisted-draft` / `verified-operate` 或 `true` / `false`；冲突时拒绝启动） |

### 三档模式展开表（`expand_operation_mode`）

| `mode` | `native_page_admission` | `native_writes` | `native_submit` | `native_write_preparation` | `native_edit_prefill` |
| --- | --- | --- | --- | --- | --- |
| `read-only` | `read-only-window` | `false` | `false` | `false` | `false` |
| `assisted-draft` | `read-only-window` | `false` | `false` | `true` | `true` |
| `verified-operate` | `read-only-window` | `true` | `true` | `false` | `true` |

---

## 二、网关 HTTP 与 SSE 端点

浏览器通过宿主 `/bootstrap` 换取短期会话能力后，所有通信经网关前缀（默认 `/woa-agent`）传输：

| 端点 | 方法 | 鉴权与请求头 | 用途 |
| --- | --- | --- | --- |
| `/healthz`、`/readyz` | `GET` | 免鉴权 | 存活探针与排空感知就绪探针（停机排空期间 `/readyz` 返回 `503`） |
| `/v1/runtime/assistant.js` | `GET` | 同源静态资源 | 支持 `ETag`、`304` 与 `.gz` 压缩的嵌入式前端 IIFE 包（未构建返回 `404 runtime_unavailable`） |
| `/v1/sessions/<id>/hello` | `POST` | `Authorization: Bearer`、`Origin` | 校验 `ClientHello` 来源与身份摘要，签发唯一活跃租约 `lease_id` |
| `/v1/sessions/<id>/observations` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 上报脱敏 `ObservationEnvelope` 与 `PageObservation` |
| `/v1/sessions/<id>/messages` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 提交幂等用户任务（繁忙时返回 `429`） |
| `/v1/sessions/<id>/claims`、`results` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 认领待执行页面动作，并回传 `ActionResultEnvelope` |
| `/v1/sessions/<id>/approvals` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 上报一次性 `ApprovalDecision`（`approved`、`denied` 或 `prefill`） |
| `/v1/sessions/<id>/cancellations` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 取消运行中任务或处理迟到预取消竞态 |
| `/v1/sessions/<id>/reconciliations` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 对 `INDETERMINATE` 未决写入发起同账号只读对账 |
| `/v1/sessions/<id>/conversation` | `POST` | Bearer、`Origin`、`X-WOA-Lease` | 接收 `{"action": "new"}`，清空上下文记忆而不删账本 |
| `/v1/sessions/<id>/events`、`timeline` | `GET` | Bearer、`Origin`、`X-WOA-Lease` | 基于 `Last-Event-ID` 的单向 SSE 事件推送与 v2 时间线投影 |

所有 `POST` 请求必须声明 `Content-Type: application/json` 与合法 `Content-Length`（`<= MAX_BODY_BYTES = 1 MiB`），并通过强类型对象校验（`SCHEMA_VERSIONS` = `1`）；载荷若夹带未脱敏接力参数或密钥，直接返回 HTTP `400`（`code: "privacy_boundary"`）。

---

## 三、JSON 线协议示例

### `POST /v1/sessions/<id>/hello`（`ClientHello`）

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "client_id": "tab_7d92b41f",
  "runtime_instance_id": "rt_9f18c30a",
  "runtime": "woa-embedded-runtime",
  "capabilities": ["observe", "click", "fill", "select", "scroll", "handoff_form"],
  "page_origin": "https://admin.example.com",
  "application_id": "yudao-admin",
  "principal_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "last_event_id": "e00000012"
}
```

### `POST /v1/sessions/<id>/observations`（`ObservationEnvelope`）

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "observation_id": "obs_0004",
  "page_epoch": 4,
  "caused_by_action_id": "act_0003",
  "events": [],
  "observation": {
    "schema_version": 1,
    "epoch": 4,
    "url": "https://admin.example.com/crm/clue",
    "title": "CRM Clue Management",
    "landmarks": ["main", "dialog"],
    "elements": [
      {
        "ref": "e1",
        "role": "textbox",
        "name": "线索名称",
        "tag": "input",
        "epoch": 4,
        "attrs": {"type": "text"}
      }
    ],
    "selected_controls": ["有效"],
    "render_state": "ready",
    "truncated": false
  }
}
```

### `POST /v1/sessions/<id>/results`（`ActionResultEnvelope`）

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "turn_id": "turn_0002",
  "action_id": "act_0003",
  "based_on_observation_id": "obs_0003",
  "page_epoch": 3,
  "command_digest": "9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab",
  "status": "completed",
  "observation_after": { "...": "page_epoch 4 时的完整 ObservationEnvelope" },
  "receipt": { "...": "ActionReceipt（含 applied_fields, rejected_fields, previous_fields, restricted_fields）" },
  "error_code": "",
  "error_detail": ""
}
```

### `POST /v1/sessions/<id>/approvals`（`ApprovalDecision`）

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "approval_id": "apr_92c17f08",
  "decision": "approved",
  "principal_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "turn_id": "turn_0002",
  "operation_id": "op_update_clue_81"
}
```

*（`decision` 合法取值：`"approved"`、`"denied"` 或 `"prefill"`。）*

---

## 四、`panel_contract.json`（`protocol: 2`）与 `presentation.json`

服务端与 Shadow DOM 面板共用 `src/web_operation_agent/web_runtime/panel_contract.json` 校验所有投影：

- **默认品牌文案（`label_keys`）：** `product: "PageHelm"`、`emptyIntro: "你好，我是 PageHelm"`；DOM 类名与样式前缀维持 `woa-*`。
- **进度与终态枚举：** 非终态为 `waiting_for_runtime`、`session_recovered`、`message_accepted`、`new_conversation`、`agent_started`、`step`、`approval_recorded`、`cancelling`；终态为 `needs_input`、`finished`、`refused`、`cancelled`、`indeterminate`、`failed`、`task_failed`。未登记状态严格拒绝。
- **静默失活码与终态映射：**
  - `lapse_codes`：`principal_expired`、`session_expired`、`browser_inactive`、`host_stopped`、`server_stopped`、`bootstrap_replaced`、`native_bootstrap_replaced`、`unknown_session`、`not_found`。
  - `terminal_panel_state_by_code`：`session_closed` / `unknown_session` / `not_found` / `principal_expired` / `session_expired` -> `"ended"`；`runtime_replaced` -> `"replaced"`；`unauthorized` / `invalid_lease` / `principal_mismatch` / `origin_refused` -> `"error"`。
- **v2 结构化载荷：** `table`（`StructuredTable` -> `<DataTable>`）、`input_options`（2–4 个 `ClarificationOption` 胶囊）、`handoff`（自动脱敏的已填/待补/受限字段与 `can_undo`），以及本地开发专属 `debug_trace`（含 `first_route_kind`、`stop_reason`、`stage_timings`、`tool_timings`、`checks`）。

前端安全交互与悬浮层几何常数集中定义在 `frontend/src/presentation.json`：

| 常数 | 数值 | 作用 |
| --- | --- | --- |
| `overlay_z_index` | `2147483646` | 确保 Shadow DOM 面板不被后台抽屉或弹窗遮挡 |
| `launcher_inset` | `20` | 悬浮球边缘留白（px） |
| `launcher_size` | `48` | 悬浮启动器宽高（px） |
| `approval_visibility.min_visible_ratio` | `0.9` | 允许点击审批前，卡片在裁剪区内的最低可见面积比例 |
| `approval_visibility.min_visible_ms` | `400` | 审批卡片连续稳定露出最短时间（ms） |
| `start.final_statuses` | `[401, 403]` | 表示宿主登录失效、不再重试拉取的终端 HTTP 状态码 |

---

## 五、系统硬性上限一览

| 边界项 | 硬上限 | 来源模块 |
| --- | --- | --- |
| 输入框单次提问字符数 | `2000` 字符 | `panel_contract.json` / 网关校验 |
| 单次 POST 请求体（`MAX_BODY_BYTES`） | `1 MiB`（`1,048,576` 字节） | `transport/gateway.py` |
| 单帧事件上限（`_MAX_EVENT_BYTES`） | `256 KiB` | `transport/protocol.py` |
| 单页 DOM 观测控件数 | `200`（`max_elements`） | `browser_contract.json` |
| 单轮临时 `TurnTables` SQLite 容量 | `36` 张表、`5000` 行/表、`128` 列、`1.5s` 查询超时 | 适配器 `turn_tables.py` |
| `<DataTable>` 结果导出上限 | `24` 列、`2000` 行、`300` 字符/单元格 | 适配器 `turn_tables.py` / `panel_view.py` |
| 回答契约（Answer Protocol）边界 | 最多 `50` 条结论、`2` 次修复、`50` 行展示记录 | `answer/answer_protocol.py` |
| 单轮模型步数上限 | `24`（核心默认）/ `33`（`YudaoTurnExecutor`） | `panel_contract.json` / `page_prompts.py` |

---

## 下一步阅读

- 查阅模型可见工具清单一览、`GoalPort` 能力契约与核心程序化自检规则：[内置工具与能力参考](./tools-and-capabilities.md)。
- 回顾单轮调度与前端降噪原理：[执行引擎与运行时](../architecture/execution-and-runtime.md)。
