---
title: 写入安全与隐私边界
description: 三档安全模式、WritePipeline 写入管道、客户端防遮挡门禁、凭据脱敏、SourceTree 白名单与 CSP 要求。
---

PageHelm 默认关闭所有浏览器与服务端状态写入。开启写入后，所有数据变更均经由 `WritePipeline`（`src/web_operation_agent/business/write_pipeline.py`）执行：先构建并校验结构化差异，再由操作员在可见审批卡片上确认，最后在单飞锁下复读、至多派发一次并同账号回读核验。

## 一、三档安全模式

服务端在组合与会话绑定阶段锁定安全模式，浏览器端无法自行提权：

| 安全模式 | 同账号 API / SQL 读取 | 页面跳转与筛选 | 非破坏性表单预填（可撤销） | 写入提案准备 | 写入派发与回读核验 |
| --- | --- | --- | --- | --- | --- |
| `read-only`（生产默认） | 允许 | 允许 | 关闭 | 关闭 | 关闭 |
| `assisted-draft` | 允许 | 允许 | 允许（跳过密码与上传控件） | 允许 | 关闭（不派发） |
| `verified-operate`（本地默认） | 允许 | 允许 | 允许 | 允许 | 允许（经 `WritePipeline` 并强制回读） |

系统提示词、Skill 文档、源码片段或历史会话记忆均不授予写入权限；写入必须由部署配置与业务适配器同时显式开启。

## 二、`WritePipeline` 写入管道

`WritePipeline` 统一承接四条写入路径（Skill 类型化写工具、约定平铺更新/状态切换、类型化状态操作与原生表单提交），按六个阶段 `(a)–(f)` 顺序推进：

```text
  (a) 提案准备与结构化校验
        - 适配器准备 BusinessMutationProposal 并绑定目标标识与 content_digest
        - 归一化差异项（normalize_approval_details），核验效果投影（verify_effect_projection）
        - 校验值来源、追加保留规则，过滤匹配 DEFAULT_SECRET_FIELD_PATTERN 的敏感控件
        - 按需生成 MutationPrefillOffer（供用户改选“仅预填，自行提交”）
         |
         v
  (b) 鲜活可见审批与单次消费
        - Shadow DOM 面板渲染“旧值 -> 新值”差异与副作用说明
        - 客户端 VisibilityGate 校验可见占比 >= 0.9、连续露出 >= 400ms、event.isTrusted === true
        - 审批决定单次消费（重复提交返回 409）；用户也可选择预填交接或拒绝
         |
         v
  (c) 单飞并发锁与意图落盘
        - InflightWriteTracker.single_flight(proposal.content_digest, consume_on_enter=True)
        - 派发前在 SQLiteMutationJournal 记录 mutation_claimed
         |
         v
  (d) 派发前复读与至多一次派发
        - pre_dispatch_reread 再次读取目标实体或表单，检测审批等待期间的并发变更；发生漂移则拒签
        - 最多派发一次；调用超时或连接中断时不自动重试
         |
         v
  (e) 同账号回读核验
        - ResultPostconditionVerifier / verify_readback 以同一操作员身份确认目标终态
         |
         v
  (f) 审计终态归档
        +--> verified      （已回读确认）
        +--> not_approved / refused / cancelled （未获批或派发前拦截）
        +--> indeterminate （派发后结果未知；落盘保留待人工对账）
```

## 三、客户端防遮挡与防脚本点击门禁

仅靠普通按钮点击容易受到页面遮挡层覆盖、视口外误触或脚本调用 `.click()` 的影响。`frontend/src/state/visibility.ts` 按 `frontend/src/presentation.json` 强制执行视觉与事件门禁：

- `min_visible_ratio: 0.9`：遍历祖先裁剪容器（`overflow` 为 `auto|scroll|hidden|clip`），结合 20 档 `IntersectionObserver` 阈值计算真实可见占比；分母取“卡片尺寸 / 视口尺寸 / 滚动视口”的最小交集，兼顾长卡片与遮挡检测。
- `min_visible_ms: 400`：审批卡片连续稳定露出至少 `400 ms` 后，确认点击才生效。
- `event.isTrusted === true`：拒绝脚本合成的点击事件。
- **Shadow DOM 隔离**：面板与审批卡片挂载于独立 Shadow DOM 容器（`overlay_z_index: 2147483646`），与宿主全局 CSS 隔离。

## 四、凭据清洗与出站审计

PageHelm 在 URL 边界、DOM 快照、审计日志、回放导出与模型请求出口均执行脱敏检查：

1. **接力参数与凭据赋值清洗（`src/web_operation_agent/privacy.py`）**：
   - 基线接力键集合 `SESSION_HANDOFF_KEYS`（`externalLoginKey`、`externaLoginKey`、`externLoginKey`、`jsessionid`）支持各适配器通过 `SessionHandoffPolicy.extending(port.session_handoff_query_keys)` 扩展。
   - `SENSITIVE_CREDENTIAL_ASSIGNMENT` 匹配 `password`、`passwd`、`pwd`、`secret`、`token`、`access_token`、`refresh_token`、`id_token`、`api_key`、`authorization` 等赋值形式，并通过递归 URL 解码检查（`url_decode_depth=3`）识别多重编码绕过。
2. **模型出站非内容审计（`src/web_operation_agent/modeling/egress.py`）**：
   - `AuditedChat` 包装底层模型客户端（兼容 `complete` 与 `complete_cancellable`），每次发送前执行 `ProviderEgressPolicy.inspect(messages, tools)` 并在审计账本中写入不含业务正文的 `provider_egress` 事件。
3. **Trace 回放写出复检（`src/web_operation_agent/state/traceview.py`）**：
   - 导出回放 HTML 前再次以 `url_decode_depth=3` 扫描全部投影字段；若检出残留凭据模式，拒绝写出原文，仅保留事件结果状态。

## 五、按需启用的 `SourceTree` 源码白名单

开启源码辅助查错时，`SourceTree`（`src/web_operation_agent/planning/sourcetree.py`）将可读范围限制在显式白名单内：

- 源码策略文件（`policies/<name>.json`）不包含本地绝对路径，必须由宿主在启动时注入 `source_root`。
- 策略声明 `allow_globs`、`deny_globs` 与单文件上限 `max_bytes_per_read`（默认 `262144` 字节），默认排除 `.env`、数据库文件、用户上传目录与业务内容。
- `SourceTree.resolve(rel)` 同时对原始相对路径 `rel` 和规范化后的真实路径 `canonical` 校验 `deny_globs` 与 `allow_globs`，防止通过软链接绕过白名单。

## 六、提示词注入与协议隔离

来自页面 DOM 文本、业务记录备注、Skill Markdown 或模型输出的内容均视为不可信输入：

- 不可信文本不能签发 `approval_id`、更改安全模式、放宽 `SourceTree` 白名单或跳过单飞锁。
- 审批卡片展示的结构化差异由服务端 `validate_details` 校验，不依赖前端自由拼接文本。
- 传输协议遇到未知字段或越界载荷时直接抛出类型化 `RemoteProtocolError`，不依赖匹配异常文本做兜底恢复。

## 七、宿主 CSP 检查清单

接入启用了严格 Content Security Policy 的后台页面时，确认以下指令：

- 允许同源或受信网关源加载 `assistant.js` 与 `assistant.css`（`script-src 'self'`、`style-src 'self'`；运行时不依赖 `unsafe-eval`）。
- 在 `connect-src` 中放行同源 `/v1/sessions/*` 请求与 `/events` SSE 连接。
- 配合同源 Cookie 与 HTTPS 反向代理部署，并按 `frontend/src/presentation.json` 中的 `start.final_statuses: [401, 403]` 处理登录态失效。

## 下一步

- 部署发布、SQLite 备份恢复与未知写入对账：[部署、运维与排障](./operations-and-observability.md)
- 协议端点与配置约束：[配置与通信协议](../reference/protocol-and-configuration.md)
