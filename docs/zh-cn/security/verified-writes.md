---
title: 安全边界与核验写入
description: 写入默认关闭、三档模式权限矩阵、八阶段 WriteSafetyPipeline、出站脱敏、Shadow DOM 与宿主 CSP 信任模型。
---

> **概要：** 业务写入默认完全关闭（`read-only`）。开启 `verified-operate` 后，所有持久变更仍须经过**八阶段 `WriteSafetyPipeline`**：严格核对值来源与效果投影、弹出鲜活可见审批卡（要求 `event.isTrusted` 与连续可见驻留）、SQLite 事务单次消费派发、提交前重读、同账号回读核验，且对 `INDETERMINATE` 未决状态**永不自动重试**。

[English](../../security/verified-writes.md)

## 三档运行模式与权限矩阵

| 模式（`pagehelm.toml` / CLI） | 页面观察与导航 | 表单预填 | 写入提案与审批卡 | 实际写入派发 |
| --- | --- | --- | --- | --- |
| `read-only`（线上生产默认） | 只读窗口与 GET 围栏下放行 | 禁止（`native_edit_prefill=false`） | 禁止（`native_writes=false`） | 禁止（`native_submit=false`） |
| `assisted-draft` | 只读窗口与 GET 围栏下放行 | 核验草稿场景允许预填（`native_edit_prefill=true`） | 允许准备提案与交接（`native_write_preparation=true`） | **禁止实际派发变更** |
| `verified-operate`（本地验收默认） | 只读窗口与 GET 围栏下放行 | 允许 | 完整开启 Skill、约定更新与原生提交提案 | 获批后单次派发并回读（`native_writes=true`、`native_submit=true`） |

> **提示词、文档与历史会话均无写权限：** 系统规则、Skill 正文、接口返回值、页面文本和过往对话记录都不能授予任何写入许可。删除、作废或角色权限变更等危险动作即使在 `verified-operate` 下也必须先向用户说明影响并取得明确确认，才生成审批卡。

## 八阶段 `WriteSafetyPipeline`

无论采用 Skill 写入、约定更新（`update_record` / `toggle_record_status`）还是原生表单提交（`SubmitPort`），统一通过 `BusinessWriteService`：

1. **值来源核验：** 助手写入的每个业务字段值都必须溯源自用户原话（含规范化日期与金额换算）、用户点选的真实候选、当轮工具换算回执，或附加保留校验过的记录原值。来源不明或缺少必填项直接以 `needs_input` 追问，严禁猜测。
2. **同账号只读准备与效果投影：** 适配器持当前操作者自己的 `PrincipalContext` 解析确切记录 ID、读取当前值并构造最小变更负载，生成人眼可读的逐字段差异（`原值 → 新值`，未改字段可折叠）。
3. **锁定提案摘要（`proposal_digest`）：** 将目标对象、操作人、回合号、请求形态与预期效果计算为不可变摘要；批准前后任何漂移都会令本次批准作废。
4. **鲜活可见审批卡（`event.isTrusted` + 可见驻留）：** 仅当卡片连续实质可见（`useVisibilityGate`）且用户发出原生浏览器手势（`event.isTrusted === true`）时才可点按确认；若被宿主抽屉遮挡（配合 `--woa-drawer-offset` 检测）会拒绝确认点击。审批具备时效且不可跨轮复用。
5. **单次消费原子认领（`SQLiteMutationJournal`）：** SQLite 事务在本地原子地将提案从 `prepared` 跃迁为 `dispatching`，保证一次批准至多派发一次，并阻塞同一业务键上的并发未决提案。
6. **派发前实时重读：** 真实发出写请求前一刻，服务端（及原生提交的浏览器表单读取器）再次核对目标记录无并发改动与卡面一致性。
7. **受限许可下单次派发：** 实际请求在窗口作用域守卫许可下至多执行一次，原生提交只放行与审批卡完全一致的接口路径、记录编号和差异字段。
8. **同账号回读与 `INDETERMINATE` 不重试：** 派发后按同一登录账号回读目标记录逐项核验。遇超时、断连、5xx 或回读证据不全，立刻记为 **`INDETERMINATE`** 并停止本轮——**绝不自动重试**；可由用户触发只读对账或运维 `--writer-stopped` 核实结案。

## 出站隐私脱敏与间接提示词注入隔离

- **凭据出站零泄露（`AuditedChat` + `privacy.py`）：** 所有发往外部大模型或落盘轨迹（`conversation-state.sqlite3`、`TurnTrace`、`audit.jsonl`）的内容都先经过支持多层 URL 解码的脱敏器，屏蔽密码、Bearer Token、API Key 与适配器声明的 `session_handoff_query_keys`（替换为 `[REDACTED]`），并通过二次校验拦截残余明文凭据。`<input type="password">` 与文件上传控件在观察阶段屏蔽取值且永不自动代填。
- **控制面与不可信数据面分离：** 页面观察、接口返回值、Skill 与源码文本统一作为非可信数据喂给模型；浏览器 Runtime 没有任意 JS 执行与任意外网请求能力，且 `origins.py` 强制同源导航（拦截 `javascript:` 与站外跳转），阻断单据备注等文本里的间接提示词注入把内部数据外带。
- **Shadow DOM 与宿主 CSP 边界（明示限制）：** 面板渲染在独立 `ShadowRoot` 内，无需放宽宿主严格 CSP（支持 `script-src 'self'`、`style-src 'self'` 与 `require-trusted-types-for 'script'`），但它与第一方宿主页面共享同一 JS 执行域，宿主应用必须自身无 XSS 漏洞；内嵌版不用于嵌入不可信第三方站点。
