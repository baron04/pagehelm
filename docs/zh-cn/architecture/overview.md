---
title: 架构总览
description: PageHelm 如何将服务端规划、浏览器 Runtime 与业务适配器清晰解耦，实现同主体混合读取与安全 UI 协同。
---

> **TL;DR：** PageHelm 将系统严格划分为三层：**第一方浏览器 Runtime**（`assistant.js`）负责当前标签页 DOM 观测与固定 UI 原语执行；**应用无关的服务端核心**负责模型规划、策略裁决、写前审批与审计账本；薄薄一层**业务适配器（Application Adapter）**负责身份校验、路由、类型化读取与已验证写入。

[English](../../architecture/overview.md)

## 一、三层架构图

```text
+-----------------------------------------------------------------------+
| 第一方业务标签页（Same Origin）
|  - 嵌入式 Runtime + Shadow DOM 面板（assistant.js）
|  - PageController 有界快照（<= 200 个交互元素）与固定 UI 原语
|  - Web Locks + sessionStorage 标签页锁与“复制标签页”隔离
|  - ApprovalCard 可见性门禁（coverage >= 0.9，停留 >= 400ms，isTrusted）
+-----------------------------------------------------------------------+
             ^ HTTPS /v1/sessions/<id>/* + SSE (/events)
             | （仅上传脱敏观测、回执与审批决定；不含密钥或规划逻辑）
             v
+-----------------------------------------------------------------------+
| 应用无关服务端核心（src/web_operation_agent/）
|  - 传输网关与通道租约状态机（Transport Gateway & Lease）
|  - ModelDrivenTurnExecutor + TurnHarness + TurnBudget
|  - AuditedChat 出站审计策略 + SessionHandoffPolicy 参数脱敏
|  - WritePipeline（8 阶段审批、漂移复读、单飞锁、回读核验）
|  - SQLiteSessionStore、SQLiteMutationJournal、SQLiteSpendCap
+-----------------------------------------------------------------------+
             ^ ApplicationPorts SPI（显式能力声明）
             v
+-----------------------------------------------------------------------+
| 业务适配器与领域 Skill（Adapters / Skill Packs）
|  - ApplicationDescriptor、IdentityPort、ApplicationRoutePort
|  - EntityReadPort、ReferenceLookupTools、TurnTables SQL / summarize
|  - ResultPostconditionVerifier、ConventionWritePort / SubmitPort
|  - 可选 SourceTree 白名单检索与 domain_playbooks / SKILL.md
+-----------------------------------------------------------------------+
```

传统企业后台 AI 助手往往走向两个极端：

- **纯 API 对话助手**：擅长统计与查询，却无法替操作员打开编辑抽屉、触发前端表单联动校验，或在提交前展示可撤销的预填草稿。
- **无头 RPA 浏览器**：需要在服务端代持凭据，并在表格分页之间逐页点击爬取数据，既慢又容易触发内存与上下文瓶颈。

PageHelm 采用**同主体混合执行（Hybrid Same-Actor Execution）**：大规模统计与关联计算在服务端沿用当前浏览器操作员身份通过受控读取（`read_summary`、`TurnTables` SQL、`<DataTable>` 导出）直接完成，而屏幕跳转、筛选定位与表单预填则在用户面前的真实标签页内安全执行。

---

## 二、端到端单轮执行时序

```text
操作员（浏览器）             assistant.js Runtime             PageHelm 服务端             大模型提供方             业务后台 API
       |                              |                              |                          |                        |
       |--- 输入任务目标 ------------>|                              |                          |                        |
       |                              |-- POST /messages + lease --->|                          |                        |
       |                              |                              |-- AuditedChat 出站审查 ->|                        |
       |                              |                              |<-- tool call (读/操作) --|                        |
       |                              |                              |-- 同主体只读查询 ------------------------------->|
       |                              |                              |<-- 行数据 / total / 页码 ------------------------|
       |                              |                              | (TurnTables SQL / Decimal)                       |
       |                              |<-- SSE panel_state / action -|                          |                        |
       |                              |    (observe / prefill 等)    |                          |                        |
       |                              |-- ActionReceipt + obs ------>|                          |                        |
       |                              |                              |-- WritePipeline 提案 --->|                        |
       |<-- 渲染 ApprovalCard --------|<-- SSE approval_request -----|                          |                        |
       |  (面积>=0.9, 停留>=400ms)    |                              |                          |                        |
       |--- 真实点击“确认执行” ------>|                              |                          |                        |
       |                              |-- POST /approvals (一次性) ->|                          |                        |
       |                              |                              |-- pre_dispatch_reread ------------------------->|
       |                              |                              |-- 单飞锁下唯一派发 ----------------------------->|
       |                              |                              |-- 同主体回读核验 ------------------------------->|
       |<-- 渲染核验差异 / <DataTable> <-- SSE finish ---------------|                          |                        |
```

每次工具调用结果和稳定的页面观测都会先回传给**同一个服务端 Turn 循环**，再由模型做出下一步决策。浏览器端永远不自行推进多步计划。

---

## 三、服务端与浏览器权限边界矩阵

| 职责项 | 服务端核心 | 浏览器 Runtime | 说明 |
| --- | --- | --- | --- |
| 模型提示词、API Key 与工具路由 | 是 | 否 | 前端产物中不包含模型密钥或规划器 |
| 会话鉴权与写模式裁决 | 是 | 否 | 由服务端锁定 `read-only` / `assisted-draft` / `verified-operate` |
| 业务实体与字段语义 | 适配器 + Skill | 否 | 通用前端不硬编码具体应用模型 |
| DOM 快照与交互元素采集 | 否 | 是 | 受 `MAX_OBSERVATION_ELEMENTS = 200` 硬上限约束 |
| 面板状态、文案与进度键名 | 是（`panel_contract.json`） | 仅渲染 | 通过 v2 `/timeline` 与 `/events` 投影下发 |
| 审批防误触/防遮挡门禁 | 下发策略（`presentation.json`） | 客户端强制校验 | `min_visible_ratio: 0.9`、`min_visible_ms: 400`、`event.isTrusted` |
| 凭据与会话接力参数脱敏 | 入库与出站前双重脱敏 | 上报网关前脱敏 | `SessionHandoffPolicy` + 递归 URL 解码检测 |
| 持久化账本与费用上限 | 是（`SQLite`） | 否 | `SQLiteMutationJournal` + `SQLiteSpendCap` |

---

## 四、标签页身份与“复制标签页”隔离

操作员在后台使用浏览器时经常使用“复制标签页（Duplicate Tab）”。如果只把会话 ID 放在 `sessionStorage` 中，新复制的标签页会直接继承原标签页的会话与租约（Lease），导致两个标签页互相抢占 SSE 流甚至错乱审批。

`frontend/src/runtime/tabid.ts` 利用浏览器原生 **Web Locks API** 根治这一问题：

1. 当前标签页 ID 写入 `sessionStorage`（键名 `woa.assistant.tab.v1`），保证本页正常刷新后能够恢复同一会话。
2. 初始化时，`tabId()` 请求持有锁 `navigator.locks.request('woa.assistant.tab.' + id, { ifAvailable: true }, ...)`：
   - **同标签页刷新**：旧文档在 `unload` 时自动释放锁，新页面立即成功认领原 `id` 与原会话。
   - **复制标签页**：原标签页仍持续持有该锁；复制出的新标签页发现锁已被占，会立即触发轮换回调（清空复制过来的本地会话状态），使用 `crypto.randomUUID()` 生成全新标签页 ID 并建立独立会话。

---

## 五、多轮对话状态边界

`ConversationState` (`src/web_operation_agent/state/conversation.py`) 显式限制上下文窗口与权限残留：

- **默认边界**：`max_turns = 4`、总序列化 JSON 上限 `max_bytes = 32 * 1024`（`32 KiB`）、业务数据片段上限 `max_business_bytes = 16 * 1024`（`16 KiB`）。
- **可配置极值**：`max_turns <= 20`、`max_bytes <= 256 KiB`、`max_business_bytes <= 128 KiB`（且 `max_business_bytes <= max_bytes`）。
- **历史轮次不授予事实与权限**：保留轮次仅用于消解“它”“上一条”等指代关系；任何新一轮请求仍必须在当前操作员身份下重新观测页面或读取服务端最新事实。
- **新建对话语义（`POST /v1/sessions/<id>/conversation` 传 `{"action": "new"}`）**：清空历史轮次、`pending_clarification` 与 `page_operation_granted` 标记，但**绝不抹除**已落盘的 `SessionLedger` 审计账本与费用记录。

---

## 六、核心中立性与 CI 边界检查

PageHelm 通过 `scripts/check_core_boundary.py` 在每次校验中静态守护分层底线：

1. **核心 Python 无特定应用执行逻辑**：基于 AST 与分词器扫描 `src/web_operation_agent/*.py`（忽略注释与文档字符串），禁止出现具体参考应用的名称或专属 API 路由前缀。
2. **核心不反向依赖适配器或测评框架**：核心包及核心单测严禁导入 `web_operation_agent_*` 适配包或 `evals`。
3. **通用浏览器 Runtime 无组件库耦合**：`frontend/src/runtime/` 禁止写死应用路由或 Element Plus `.el-*` 类名；组件库辅助函数集中放置在 `frontend/src/integrations/`。
4. **文档与产物无本地泄露**：文档站点校验确认不泄露本地磁盘路径或内部工单标记。

---

## 七、系统明确拒绝的越界场景

| 场景 | PageHelm 处理方式 |
| --- | --- |
| 跨域外网站点或人机验证码（CAPTCHA） | 明确拒绝或交由人工处理，绝不尝试破解或绕过校验 |
| 服务端分页或返回集存在截断（`truncated=True`） | 禁止直接根据不完整切片编造“总计”，必须扩大过滤读取或显式声明局部范围 |
| 写入目标存在多条候选或字段值缺失不明确 | 调用 `finish(status="need_input", options=[...])` 提供已核验候选按钮，绝不猜值写入 |
| 单飞派发（`mutation_claimed`）后网络超时或连接中断 | 标记为 `INDETERMINATE`（状态未知），严禁自动重试 |
| 适配器未在 `capabilities()` 声明的高级能力 | 清晰向用户说明边界限制，绝不向核心伪造支持声明 |

---

## 下一步阅读

- 深入了解单轮执行引擎、并发读取合并、`TurnTables` SQL 与页面稳定机制：[执行引擎与运行时](./execution-and-runtime.md)。
- 查看 8 阶段写安全管道与客户端可见性防线：[安全模型与可验证写入](../security/verified-writes.md)。
- 按照渐进式层级接入你的首个业务应用：[L0–L3 渐进式接入](../integration/levels.md)。
