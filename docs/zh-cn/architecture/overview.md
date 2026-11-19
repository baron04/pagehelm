---
title: 架构概览
description: PageHelm 如何划分服务端规划、浏览器 Runtime 与应用适配器职责，实现同账号安全读写与可观测性。
---

> **概要：** PageHelm 将系统清晰拆为三层：负责页面观察与固定动作的**第一方浏览器 Runtime**（`assistant.js`）、掌舵规划与安全策略的**应用无关服务端核心**，以及提供身份、路由、读取和受控写入端口的**应用适配器**。

[English](../../architecture/overview.md)

## 三层架构与职责边界

```text
+-------------------------------------------------------------------+
| 第一方业务页面（同源宿主）                                           |
|  - 嵌入式 Runtime + Shadow DOM 面板（assistant.js）                 |
|  - PageController 语义快照与固定页面动作原语                        |
|  - 审批卡连续可见驻留门控 + event.isTrusted 原生手势校验             |
+-------------------------------------------------------------------+
          ^ 已认证的页面观察 / 动作回执 / 用户审批决定（HTTPS）
          v
+-------------------------------------------------------------------+
| 应用无关服务端核心（web_operation_agent）                            |
|  - TurnExecutor & TurnHarness | 出站凭据脱敏与隐私门禁               |
|  - WritePipeline              | 预算限额、配额与 SQLite 持久化       |
|  - ProgramCheckSpec 检查接口  | TurnTrace 轨迹与时间线投影           |
+-------------------------------------------------------------------+
          ^ 类型化应用端口（ApplicationPorts & GoalData）
          v
+-------------------------------------------------------------------+
| 应用适配器（如 examples/yudao、testapps/contacts）                  |
|  - 身份反查与路由校验          | 同账号 API 与行表 SQL 读取          |
|  - 业务域指南与 Skill 手册     | 约定更新与原生表单提交处理器        |
+-------------------------------------------------------------------+
```

### 核心设计原则

1. **凭据与规则不出浏览器：** 模型 API Key、系统提示词、Skill 规程、源码索引与测评预期全部保留在服务端，浏览器侧无法窃取密钥或策略。
2. **结果全部回到同一服务端 Agent：** 每次点击、导航、接口读取或用户批准都会连同最新页面观察回传给服务端同一执行循环，再做下一步决策。
3. **运行时事实高于静态文档：** 当实时观察、同账号接口回读与 Skill/源码文档存在出入时，永远以实时事实为准。
4. **核心保持应用中性：** `src/web_operation_agent/` 与 `frontend/src/runtime/` 不得包含任何具体应用名、业务路由或组件库 CSS 选择器（由 `scripts/check_core_boundary.py` 强制门禁检查）。

## 混合执行：同账号 API 读取 + 语义 DOM 操作

PageHelm 使用统一的模型驱动执行器（`ModelDrivenTurnExecutor` 配合 `TurnHarness`）调度两条互补路径：

- **同账号 API 与内存 SQL 读取：** `call_api` 等读取工具在服务端持当前登录用户自己的访问令牌执行（绝不使用全局超级管理员账号），直接受宿主既有数据权限与租户隔离约束。读到的列表可装入当轮内存 `TurnTables` 供 `tables` / `sql` / `date_range` 分析，大数据表直接通过 `finish` 绑定结构化 `<DataTable>` 载荷（支持前端 10/25/50/全部 分页、快筛、排序与 TSV/CSV 导出），无需模型手抄长名单。同一回复内的多个独立纯读调用可在工作线程并发拉取，并在主线程按原序提交。
- **语义 DOM 导航与表单预填：** 浏览器工具（`goto`、`click`、`input`、`select`、`fill_fields`、`scroll`、`highlight`）通过绑定观察代次的元素序号操作页面。快照优先保留活跃弹窗与浮层控件；`click`、`select`、`goto` 等落定后在回执直接附带观察摘要（`settled_action_observation`），减少多余 `observe` 往返；落定探测忽略同值属性空写与长耗时噪音请求，压缩等待耗时。

## 会话隔离与全链路可观测

- **按标签页实例隔离：** 每个浏览器标签页通过 `sessionStorage` 与 Web Lock 维护独立的 `(application_id, principal_digest, runtime_instance_id)` 会话。新开或复制标签页不会抢占或取消另一标签页的在途任务。
- **有界历史而不复用陈旧数值：** `ConversationState` 默认仅保留最近 4 轮（总计 `32 KiB`）脱敏对话脉络作为指代参考；历史轮次的业务数值和路由不会当作当前事实直接复用。
- **结构化进度与轨迹回放：** `TurnTrace` 记录每步模型调用、工具分段耗时与等待细分、首跳路由类别 `first_route_kind` 与 `ProgramCheckSpec` 拦截结果，可导出为脱敏静态回放页 `trace_replay.html`。

## 当前实现边界

- **单机部署形态：** 会话恢复、写入日志（`SQLiteMutationJournal`）与请求配额基于本机 SQLite `WAL` 模式，不支持跨主机并发挂载或跨实例热迁移。
- **第一方宿主假设：** 面板通过 Shadow DOM 隔离样式，并检查 `event.isTrusted` 与可见驻留时间，但仍与宿主文档共享 JavaScript 执行域；宿主页面须保持无 XSS 并配置严格 CSP（见[安全边界与核验写入](../security/verified-writes.md)）。
- **Alpha 质量范围：** 仓库通过完整 `yudao` 与合成 `contacts` 双适配器验证框架合同，但合同通过不代表对未见新题集的通用准确率保证。
