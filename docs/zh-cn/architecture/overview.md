---
title: 架构总览
description: PageHelm 浏览器 Runtime、应用无关服务端核心与业务适配器的职责边界、单轮数据流与系统边界。
---

PageHelm 由三层组成：**第一方浏览器 Runtime**（`assistant.js`）、**应用无关的服务端核心**（`src/web_operation_agent/`），以及面向具体业务系统的**业务适配器**。三层之间通过显式协议与端口契约隔离，浏览器不持有模型密钥与规划逻辑，服务端核心不耦合具体业务应用。

## 一、三层架构与数据流

```text
+-----------------------------------------------------------------------+
| 第一方业务标签页（同源）
|  - 嵌入式 Runtime 与 Shadow DOM 面板（assistant.js）
|  - PageController 有界快照（<= 200 个交互元素）与固定 UI 原语
|  - Web Locks + sessionStorage 标签页锁与复制标签页隔离
|  - ApprovalCard 可见性门禁（coverage >= 0.9，停留 >= 400ms，isTrusted）
+-----------------------------------------------------------------------+
             ^ HTTPS /v1/sessions/<id>/* + SSE (/events)
             | （仅传输脱敏观测、回执与审批决定；不含密钥或规划逻辑）
             v
+-----------------------------------------------------------------------+
| 应用无关服务端核心（src/web_operation_agent/）
|  - 传输网关与通道租约状态机（Transport Gateway & Lease）
|  - ModelDrivenTurnExecutor + TurnHarness + TurnBudget
|  - AuditedChat 出站审计与 SessionHandoffPolicy 参数脱敏
|  - TurnTables 单轮只读 SQL 引擎、WritePipeline 与 CompiledWrites 写管道    |
|  - SQLiteSessionStore、SQLiteMutationJournal、SQLiteSpendCap           |
+-----------------------------------------------------------------------+
             ^ ApplicationPorts SPI（Identity / Routes / Reads / Writes / Forms）
             v
+-----------------------------------------------------------------------+
| 业务适配器与领域 Skill（Adapters / Skill Packs）
|  - Identity / Entity / Page / Form 端口（单应用声明式辅助：            |
|    testapps/contacts/declarative/）                                   |
|  - 可选 CompiledWrites、ConventionWritePort / SubmitPort              |
|  - 可选 SourceTree 白名单检索与 domain_playbooks / SKILL.md
+-----------------------------------------------------------------------+
```

数据读取与页面交互采用分工执行：大表统计、条件聚合和多表关联在服务端沿用当前登录人身份通过受控读取接口完成；页面跳转、筛选定位和表单预填则在用户面前的真实标签页中执行。

## 二、端到端单轮执行时序

```text
操作员（浏览器）             assistant.js Runtime             PageHelm 服务端             模型提供方              业务后台 API
       |                              |                              |                          |                        |
       |--- 输入任务目标 ------------>|                              |                          |                        |
       |                              |-- POST /messages + lease --->|                          |                        |
       |                              |                              |-- AuditedChat 出站审查 ->|                        |
       |                              |                              |<-- tool call (读/操作) --|                        |
       |                              |                              |-- 同主体只读查询 ------------------------------->|
       |                              |                              |<-- 行数据 / total / 页码 ------------------------|
       |                              |                              | (TurnTables SQL / Decimal)                       |
       |                              |<-- SSE panel_state / action -|                          |                        |
       |                              |-- ActionReceipt + obs ------>|                          |                        |
       |                              |                              |-- WritePipeline 提案 --->|                        |
       |<-- 渲染 ApprovalCard --------|<-- SSE approval_request -----|                          |                        |
       |--- 人工点击确认 ------------>|                              |                          |                        |
       |                              |-- POST /approvals (一次性) ->|                          |                        |
       |                              |                              |-- pre_dispatch_reread ------------------------->|
       |                              |                              |-- 单飞锁下唯一派发 ----------------------------->|
       |                              |                              |-- 同主体回读核验 ------------------------------->|
       |<-- 展示核验结果 / <DataTable> <-- SSE finish ---------------|                          |                        |
```

每次工具调用结果和稳定的页面观测都会回传给同一个服务端 Turn 循环，再由模型做下一步决策。浏览器端不自行推进多步计划。

## 三、服务端与浏览器职责边界

| 职责项 | 服务端核心 | 浏览器 Runtime | 说明 |
| --- | --- | --- | --- |
| 模型提示词、API Key 与工具路由 | 是 | 否 | 前端产物不含模型密钥或规划器 |
| 会话鉴权与安全模式裁决 | 是 | 否 | 服务端锁定 `read-only` / `assisted-draft` / `verified-operate` |
| 业务实体与字段语义 | 适配器 + Skill | 否 | 通用前端不硬编码业务模型 |
| DOM 快照与交互元素采集 | 否 | 是 | 受 `max_elements = 200` 上限约束 |
| 面板状态、文案与进度键名 | 是（`panel_contract.json`） | 仅渲染 | 通过 v2 `/timeline` 与 `/events` 下发 |
| 审批防遮挡与防脚本点击门禁 | 下发策略（`presentation.json`） | 客户端执行校验 | 覆盖率 `>= 0.9`、停留 `>= 400ms`、`event.isTrusted` |
| 凭据与接力参数脱敏 | 入库与出站前脱敏 | 上报网关前脱敏 | `SessionHandoffPolicy` + 递归 URL 解码检测 |
| 持久化账本与费用上限 | 是（SQLite） | 否 | `SQLiteMutationJournal` + `SQLiteSpendCap` |

## 四、标签页隔离与多轮上下文边界

### 复制标签页隔离（Web Locks）

如果只把会话 ID 存在 `sessionStorage`，浏览器“复制标签页”会继承原标签页的会话与租约，导致两个标签页争抢同一条 SSE 流。`frontend/src/runtime/tabid.ts` 用 Web Locks API 处理这一场景：

1. 当前标签页 ID 写入 `sessionStorage`（键名 `woa.assistant.tab.v1`），保证同页刷新可恢复原会话。
2. 初始化时通过 `navigator.locks.request('woa.assistant.tab.' + id, { ifAvailable: true }, ...)` 请求锁：
   - **同页刷新**：旧文档在 `unload` 时释放锁，新文档认领原 ID 并恢复会话。
   - **复制标签页**：原标签页仍持有锁；新标签页抢锁失败后清空复制来的本地会话状态，用 `crypto.randomUUID()` 生成新标签页 ID 并建立独立会话。

### 多轮对话状态边界

`ConversationState`（`src/web_operation_agent/state/conversation.py`）限制上下文保留范围：

- **默认上限**：`max_turns = 4`、总序列化 JSON 上限 `32 KiB`、业务数据片段上限 `16 KiB`（可配置极值分别为 `20` 轮、`256 KiB`、`128 KiB`，且业务片段上限不超过总上限）。
- **历史轮次不授予事实与权限**：保留轮次只用于消解“它”“上一条”等指代；新请求仍须在当前登录人身份下重新读取最新事实或页面观测。
- **新建对话**：向 `POST /v1/sessions/<id>/conversation` 提交 `{"action": "new"}` 会清空历史轮次、`pending_clarification` 与 `page_operation_granted`，但保留已落盘的 `SessionLedger` 审计账本与费用记录。

## 五、核心中立性静态检查

`scripts/check_core_boundary.py` 在开发与 CI 阶段静态校验分层约束：

1. `src/web_operation_agent/*.py`（忽略注释与文档字符串）不允许出现具体参考应用的名称或专属 API 路由前缀。
2. 核心包及核心单测不允许导入 `web_operation_agent_*` 适配包或 `evals`。
3. `frontend/src/runtime/` 不允许硬编码应用路由或 Element Plus `.el-*` 类名；组件库辅助函数集中在 `frontend/src/integrations/`。
4. 文档站构建产物不允许泄露本地磁盘路径或本地文件 URI。

## 六、明确拒绝的越界场景

| 场景 | 处理方式 |
| --- | --- |
| 跨域外网站点或人机验证码（CAPTCHA） | 拒绝执行或交回人工，不尝试绕过 |
| 服务端分页返回被截断（`truncated=True`） | 禁止将局部切片报告为全集总数，须继续读取或明确声明局部范围 |
| 写入目标存在多条同名候选或必填项缺失 | 通过 `finish(status="need_input", options=[...])` 提供候选选项，不猜值写入 |
| 单飞派发（`mutation_claimed`）后网络超时或断连 | 记录为 `INDETERMINATE`，不自动重试 |
| 适配器未在 `capabilities()` 声明的能力 | 向用户说明不支持，不向核心伪造能力声明 |

## 下一步

- 单轮调度、临时 SQL 与浏览器稳定机制：[执行引擎与浏览器运行时](./execution-and-runtime.md)
- 写入审批管道与隐私边界：[写入安全与隐私边界](../security/verified-writes.md)
- 接入自有业务系统：[应用声明式接入指南](../integration/onboarding.md)
