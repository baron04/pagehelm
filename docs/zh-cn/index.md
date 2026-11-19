---
title: PageHelm 文档中心
description: 第一方 Web 应用同源嵌入式 AI 操作台（web_operation_agent）：服务端规划与审计、同账号 API + 语义 DOM 双轨执行，以及鲜活可见审批写入。
template: splash
hero:
  title: PageHelm
  tagline: 同源嵌入，安全可控。双轨直通业务接口与实时界面。
---

[English](../index.md)

## 为什么选择同源双轨架构？

企业后台通常横跨数十个路由、严格的行级数据权限、前端字典/金额换算以及多行复杂弹窗。纯 API 聊天机器人与服务端无头 RPA 各有明显短板：

| 维度 | 纯 API 对话机器人 | 服务端无头 RPA | 纯浏览器端 DOM Agent | **PageHelm（同源双轨）** |
| --- | --- | --- | --- | --- |
| **凭据安全边界** | 常见服务超级密钥 | 服务端托管用户 Cookie（合规高危） | 模型密钥或认证易暴露 | **模型密钥锁死服务端；用户登录态不出浏览器** |
| **数千行统计聚合** | 仅限现有开放接口 | 逐页翻 DOM 极慢且易漏尾页 | 受限于节点数与上下文 | **同账号并发 `call_api` + 当轮内存 `TurnTables` (`sql`)** |
| **弹窗筛选与预填交接** | 完全不支持 | 脱离用户当前标签页，无法人工接力 | 可操作当前 DOM | **绑定观察代次的 `PageController` + Element Plus 适配 + 一键撤销** |
| **持久业务写入治理** | 需逐接口硬编码确认 | 极易误触“保存 / 删除”按钮 | 缺乏服务端单次 CAS 锁 | **八阶段 `WritePipeline`、`event.isTrusted`、至多派发一次、同账号回读** |
| **超时/断连未决恢复** | 容易盲目重试 | 页面状态未知，难以对账 | 易产生半截点击风险 | **`SQLiteMutationJournal` 冻结为 `INDETERMINATE`，严禁自动重试** |
| **出站隐私防泄露** | 依赖人工过滤 | 截图易带出明文凭据 | DOM 令牌易直接入模 | **多层 URL 解码脱敏（`AuditedChat`）与密码/文件框源头剥离** |

---

## 文档导航地图

### 架构与核心原理
- **[架构概览与信任边界](architecture/overview.md)** — 服务端与浏览器 Runtime 的严格职责切分、端到端请求生命周期、多标签页会话隔离及部署边界。
- **[执行引擎与浏览器运行时](architecture/execution-and-runtime.md)** — `ModelDrivenTurnExecutor`、`TurnHarness`、同回复并发读取与顺序提交、`TurnTables`（`sql` / `<DataTable>`）、十进制汇总引擎及 Element Plus DOM 控制细节。

### 快速上手与应用接入
- **[5 分钟快速上手与运维](integration/quickstart.md)** — 1–3 行 `.env` 与 `pagehelm.toml` 配置、双套本地沙盒（`./local --synthetic-clock`）、生产 `./deploy` 发布与 `scripts/check.py` 开发检查矩阵。
- **[L0–L3 分级接入指南](integration/levels.md)** — 从单一 `<script>` 标签（L0）到 `ApplicationPorts` 与 `QueryEvidence`（L1）、业务域手册与消歧胶囊（L2），再到核验写入与合同测试（L3）的渐进集成手册。
- **[Skill、约定更新与提交编写](integration/skills-and-writes.md)** — `SKILL.md` 与 `agents/woa.toml` 五要素编写规范、`prepare_mutation` / `execute_mutation` 实现、`ConventionWritePort` 对称更新及 `SubmitPort` 表单交接。

### 安全治理与生产运维
- **[安全边界与八阶段核验写入](security/verified-writes.md)** — `read-only` / `assisted-draft` / `verified-operate` 三档权限矩阵、八阶段 `WritePipeline`、卡片防遮挡可见驻留、`AuditedChat` 出站脱敏与宿主 CSP 前提。
- **[持久化运维、预算与可观测](security/operations-and-observability.md)** — SQLite WAL 在线热备与 `--writer-isolated` 恢复、`INDETERMINATE` 未决账本核对恢复、`SIGTERM` 优雅排空、`SQLiteSpendCap` 原子额度、零正文 OTLP 与 `TurnTrace` 回放。

### 协议与参考手册
- **[配置项与双端通信协议](reference/protocol-and-configuration.md)** — `pagehelm.toml` 全字段校验规则、`.env` 键位、宿主 CLI 参数、`/v1/sessions/` 接口与 JSON 样例、`panel_contract.json` v2 状态及硬限额。
- **[内置工具、能力开关与程序检查](reference/tools-and-capabilities.md)** — 全部内置读取、SQL、页面、Skill、提交与源码工具参数契约，`GoalPort.capabilities()` 能力枚举及 `ProgramCheckSpec` 守卫列表。

---

## 仓库目录结构

| 目录 | 职责说明 |
| --- | --- |
| `src/web_operation_agent/` | 应用无关核心包：决策循环、`TurnHarness`、`WritePipeline`、网关、协议 Schema 与 SQLite 存储 |
| `frontend/` | 浏览器 Runtime（`assistant.js`）、Shadow DOM React 面板、`PageController` 及仓内 `element-plus/` 公共集成层 |
| `examples/yudao/` | 完整参考业务适配器，附带双套隔离 Docker Compose 本地环境（`./local`）与部署工具（`./deploy`） |
| `testapps/contacts/` | 结构迥异的合成联系人测试应用，用于交叉验证框架黑盒契约与真实 Chromium 集成 |
| `docs/` 与 `frontend/docs/` | 中英双语单一 Markdown 源码（`docs/`）与 Astro Starlight 静态文档站（`frontend/docs/`，含 `/llms.txt`） |

> **Alpha 阶段声明：** PageHelm 仍处于预发布阶段，仅允许在可重置、完全隔离的合成测试环境中评估，切勿接入真实生产数据库、真实账号或资金流水。
