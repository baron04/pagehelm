---
title: PageHelm 文档
description: 嵌入第一方 Web 应用的 AI 操作框架（web_operation_agent）：服务端规划与审计、同账号读取与页面交互、可见审批写入。
template: splash
hero:
  title: PageHelm
  tagline: 嵌入第一方 Web 应用的 AI 操作框架。
---

## 核心工作方式

- **同账号数据读取**：列表统计、条件聚合和多表关联在服务端沿用当前登录人身份执行，不在浏览器里翻页抓取，也不要求服务端代持用户 Cookie。
- **同源页面交互**：在用户当前标签页内完成页面跳转、筛选定位和表单预填；预填支持一键撤销，密码和上传控件始终交由人工输入。
- **受控状态写入**：生产默认只读（`read-only`）。开启写入后，变更必须经人工在可见审批卡片上确认、单次派发并同账号回读核验；派发后超时或断连的记录标记为未决态（`INDETERMINATE`），不自动重试。

## 阅读路径

| 目标 | 推荐入口 |
| --- | --- |
| 想先在本地跑通参考示例，体验查询、预填和审批写入 | [本地示例上手](integration/quickstart.md) |
| 要把 PageHelm 接入自己的后台系统 | [应用声明式接入指南](integration/onboarding.md) · [Skill 与类型化写入](integration/skills-and-writes.md) |
| 评估系统边界、执行机制与安全模型 | [架构总览](architecture/overview.md) · [写入安全与隐私边界](security/verified-writes.md) |
| 部署、备份对账或查阅配置与协议字段 | [部署、运维与排障](security/operations-and-observability.md) · [配置与通信协议](reference/protocol-and-configuration.md) |

## 仓库目录

| 路径 | 职责 |
| --- | --- |
| `src/web_operation_agent/` | 应用无关的服务端核心：执行循环、写入管道、网关、协议 Schema 与 SQLite 存储 |
| `frontend/` | 浏览器 Runtime（`assistant.js`）、Shadow DOM 面板、`PageController` 与 `element-plus/` 公共适配层 |
| `examples/yudao/` | 完整参考示例适配器，附带本地 Compose 脚本（`./local`）与发布脚本（`./deploy`） |
| `testapps/contacts/` | 独立的合成联系人测试应用，用于交叉验证核心协议与浏览器集成 |
| `docs/`、`frontend/docs/` | 中英双语文档源与 Astro Starlight 文档站 |

> **Alpha 阶段说明**：PageHelm 目前处于预发布阶段，仅限在隔离、可重置的合成测试环境中评估，请勿连接生产数据库、真实账号或资金业务。
