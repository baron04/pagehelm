---
title: 安全模型与可验证写入
description: 三档安全模式、8 阶段 WritePipeline 写入管道、防遮挡可见性审批门禁、凭据脱敏与提示词注入隔离设计。
---

> **TL;DR：** PageHelm 默认关闭一切浏览器及后端状态写入。任何数据变更都必须经由统一的单飞 **8 阶段 `WritePipeline`**——依次完成结构化差异投影、人工可见审批（覆盖面积 >= 0.9、停留 >= 400ms、`event.isTrusted === true`）、派发前复读漂移校验、无自动重试单飞派发，以及同主体回读核验。

[English](../../security/verified-writes.md)

## 一、三档执行安全模式

服务端在会话绑定与组合阶段锁定允许的能力等级，浏览器端无权自行提权：

| 安全模式 | 同主体 API / SQL 查询 | 页面跳转与筛选 | 非破坏性表单预填（`undoFormFill`） | 已确认的后端/提交写入 | 写后回读核验 |
| --- | --- | --- | --- | --- | --- |
| `read-only` | 允许 | 允许 | 禁止 | 禁止 | 不适用 |
| `assisted-draft` | 允许 | 允许 | 允许（跳过密码密钥、可撤销） | 禁止 | 不适用 |
| `verified-operate` | 允许 | 允许 | 允许 | **仅限通过 8 阶段 `WritePipeline`** | 强制执行（`ResultPostconditionVerifier`） |

系统提示词、Skill 文档、源码片段或历史会话记忆均**不具备写授权效力**；所有写操作均需部署配置与业务适配器显式开启。

---

## 二、8 阶段 `WritePipeline` 全流程状态机

`WritePipeline` (`src/web_operation_agent/business/write_pipeline.py`) 统一收口 typed 业务服务写入（`run_service_write`）、约定式平铺更新/状态流转以及原生 UI 表单提交（`run_submit_pipeline`）：

```text
  [1] 提案构建与结构化差异归一化
        - FieldDiff / change_detail / create_detail / preserved_detail /
          side_effect_detail / derived_detail / target_rule_detail / body_detail
        - 计算并绑定唯一 proposal.content_digest 与精确目标实体标识
         |
         v
  [2] 敏感字段与值来源校验
        - 匹配 DEFAULT_SECRET_FIELD_PATTERN 过滤密码/密钥/Token 控件
        - 检查候选值来源合法性与追加保留规则（append-preservation）
         |
         v
  [3] 可见审批卡片渲染（ApprovalCard.tsx）
        - 在 Shadow DOM 面板内渲染“旧值 -> 新值”差异与副作用提示
         |
         v
  [4] 一次性审批授予与单飞并发锁
        - VisibilityGate 校验 minVisibleRatio >= 0.9、停留 >= 400ms、event.isTrusted
        - InflightWriteTracker.single_flight(proposal.content_digest, consume_on_enter=True)
        - approval_id 严格单次消费（重放即返回 409）
         |
         v
  [5] 派发前实时复读与漂移拒签（pre_dispatch_reread）
        - 再次读取目标实体的当前状态并逐字段比对审批快照
        - 若在审批等待期间发生意外变更，派发前直接拒签终止
         |
         v
  [6] 单飞武装派发（严禁自动重试）
        - 先向 SQLiteMutationJournal 写入 mutation_claimed 标记
        - 若请求超时或连接断开，绝不自动重发二次写入
         |
         v
  [7] 同主体回读后置核验
        - 调用 ResultPostconditionVerifier / BusinessPostconditionVerifier
        - 以同一操作员身份确认业务库或界面已反映预期终态
         |
         v
  [8] 审计终态归档
        +--> verified      （已回读确认目标终态）
        +--> indeterminate （派发后结果未知；持久化保留待人工对账）
        +--> refused / cancelled （未派发或派发前因漂移/取消拦截）
```

---

## 三、客户端防遮挡与防脚本点击门禁

仅靠“弹窗点一下确认”很容易在浏览器环境中被页面遮挡层覆盖、缩出视口，或被恶意脚本调用 `.click()` 伪造。`frontend/src/state/visibility.ts` 强制落实施加由 `frontend/src/presentation.json` 定义的视觉门禁参数：

- `min_visible_ratio: 0.9`：沿祖先 DOM 节点遍历所有裁剪容器（`overflow` 为 `auto|scroll|hidden|clip`），结合 20 档 `IntersectionObserver` 阈值计算真实可见占比。分母取“卡片宽高 / 视口宽高 / 滚动视口”的最小交集，既防止长卡片无法达标，也杜绝仅露出一条缝却被点击的风险。
- `min_visible_ms: 400`：卡片必须连续稳定露出至少 `400 ms` 后，批准按钮点击才被视为有效。
- `event.isTrusted === true`：明确拒绝所有脚本合成点击事件。
- **Shadow DOM 样式与事件隔离**：面板与审批卡片挂载在独立 Shadow DOM 容器中（`overlay_z_index: 2147483646`），不受宿主页面全局 CSS 覆盖。

---

## 四、凭据、会话接力参数与出站审计

PageHelm 在 URL 边界、DOM 快照、审计日志、Trace 回放与大模型请求出口设置多层脱敏屏障：

1. **接力参数与凭据赋值清洗（`src/web_operation_agent/privacy.py`）**：
   - 基线接力键集合 `SESSION_HANDOFF_KEYS`（`externalLoginKey`、`externaLoginKey`、`externLoginKey`、`jsessionid`）支持各业务适配器通过 `SessionHandoffPolicy.extending(port.session_handoff_query_keys)` 追加自家参数。
   - `SENSITIVE_CREDENTIAL_ASSIGNMENT` 覆盖 `password`、`passwd`、`pwd`、`secret`、`token`、`access_token`、`refresh_token`、`id_token`、`api_key`、`authorization` 等赋值形式，并通过递归 URL 解码检查（如 `url_decode_depth=3`）识破多重百分号编码绕过。
2. **大模型出站非内容审计（`src/web_operation_agent/modeling/egress.py`）**：
   - `AuditedChat` 包装底层模型客户端（同时兼容 `complete` 与 `complete_cancellable`），在每次发送至模型接口前先经过 `ProviderEgressPolicy.inspect(messages, tools)` 核验，并向审计账本追加不含业务正文的 `provider_egress` 投影记录。
3. **Trace 回放写出前复检（`src/web_operation_agent/state/traceview.py`）**：
   - 导出调试回放前再次用 `url_decode_depth=3` 扫描全部投影字段；一旦残余未脱敏凭据模式，立即拒绝落盘原文并仅保留事件结果状态。

---

## 五、按需开启的 `SourceTree` 源码白名单

当部署选择开启源码辅助查错时，`SourceTree` (`src/web_operation_agent/planning/sourcetree.py`) 将读取权限收敛在显式白名单内：

- 源码策略 JSON（`policies/<name>.json`）禁止自带本地路径，必须由宿主启动时注入绝对路径 `source_root`。
- 策略显式配置 `allow_globs`、`deny_globs` 与单文件字节上限 `max_bytes_per_read`（默认 `262144` 字节），默认排除 `.env`、数据库文件、用户上传目录与业务内容。
- `SourceTree.resolve(rel)` **同时对原始相对路径 `rel` 与真实解析路径 `canonical` 校验 `deny_globs` 与 `allow_globs`**，彻底堵死利用软链接或 `views/Foo.vue -> ../secrets/Foo.vue` 洗白敏感目录的路径穿越漏洞。

---

## 六、提示词注入与权限隔离

来自页面 DOM 文本、客户备注、Skill Markdown 或模型自由输出的全部内容一律视为**不可信数据（Untrusted Input）**：

- 任何不可信文本均无法签发 `approval_id`、修改安全模式、放宽 `SourceTree` 白名单或跳过单飞审批锁。
- 审批卡片展示的结构化字段差异均由服务端归一化校验（`validate_details`），不盲从前端随意拼接的提示文案。
- 协议层出现未知字段或越界载荷时，传输网关直接抛出类型化的 `RemoteProtocolError`，绝不降级容忍或靠匹配异常文本恢复逻辑。

---

## 七、生产环境 CSP 配置检查清单

将 `assistant.js` 接入启用了严格 Content Security Policy 的后台页面时，请核对以下配置：

- 以同源或受信网关源加载 `assistant.js` 与 `assistant.css`（`script-src 'self'`、`style-src 'self'` —— 运行时基于 Shadow DOM 渲染，无需启用 `unsafe-eval`）。
- 在 `connect-src` 中放行同源 `/v1/sessions/*` 接口调用与 `/events` SSE 长连接。
- 结合同源 Cookie 与 HTTPS 反向代理部署，并按 `frontend/src/presentation.json` 中的 `start.final_statuses: [401, 403]` 约定优雅处理登录态失效。

---

## 下一步阅读

- 了解 SQLite 备份恢复、未知写入人工对账、三层费用治理与 OTLP 遥测：[运维与可观测性](./operations-and-observability.md)。
- 查阅 `/v1/sessions/*` 完整 HTTP/SSE 线协议与配置项：[协议与配置参考](../reference/protocol-and-configuration.md)。
