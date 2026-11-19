---
title: PageHelm 文档
description: 嵌入第一方 Web 应用的受控 AI 操作助手：服务端掌舵规划、同账号混合读写、可见差异审批。
---

> **概要：** **PageHelm**（核心包 `web_operation_agent`）将 AI 操作助手嵌入第一方企业后台。服务端统一掌舵模型规划、凭据、安全档位与审计；浏览器端 Runtime 仅观察当前页面、渲染 `<DataTable>` 结果与差异审批卡，绝不持有模型密钥或业务计划。

[English](../index.md) · [架构概览](architecture/overview.md) · [5 分钟快速上手](integration/quickstart.md) · [L0–L3 分级接入](integration/levels.md) · [核验写入与安全](security/verified-writes.md) · [协议与配置参考](reference/protocol-and-configuration.md)

## 为什么选择 PageHelm

常见后台 AI 助手常陷入两难：纯 API 对话助手脱离浏览器界面，无法帮用户“打开某张页面、填好复杂表单并停在提交前”；而外部无头浏览器代理需要代持登录态，在大表格上逐页点击耗时高，且容易误触保存或删除按钮。**PageHelm** 将**服务端同账号 API 读取**与**页面内同源 DOM 执行**结合起来：查数快而准，界面定位所见即所得，所有持久变更都停在鲜活可见的人工审批卡前。

## 文档导航

| 部分 | 关注重点 | 推荐入口 |
| --- | --- | --- |
| **简介与架构** | 服务端–Runtime–适配器三层分工、混合执行双轨、标签页会话隔离 | [架构概览](architecture/overview.md) |
| **快速上手与分级接入** | `.env` + `pagehelm.toml`、`./local`、`./deploy`、检查分流、L0–L3 渐进接入 | [5 分钟快速上手](integration/quickstart.md) · [L0–L3 分级接入](integration/levels.md) |
| **安全边界与核验写入** | 三档安全模式（`read-only` / `assisted-draft` / `verified-operate`）、八步写管线、出站脱敏 | [安全边界与核验写入](security/verified-writes.md) |
| **API 与协议参考** | `pagehelm.toml` 约束、网关 HTTP/SSE 端点、`panel_contract.json` v2 | [API 与协议配置参考](reference/protocol-and-configuration.md) |

## 仓库目录结构

| 目录 | 职责 |
| --- | --- |
| `src/web_operation_agent/` | 应用无关核心：执行器与护栏、写入安全管线、网关、Schema、SQLite 状态存储 |
| `frontend/` | 浏览器 Runtime（`assistant.js`）、Shadow DOM 面板与仓内 `element-plus/` 公共集成层 |
| `examples/yudao/` | 完整参考适配器（`yudao`），提供本地双实例 `./local` 与线上发布入口 `./deploy` |
| `testapps/contacts/` | 结构不同的合成 Contacts 应用，用于独立验证核心契约与 Chromium 集成 |
| `docs/` 与 `frontend/docs/` | 中英双语共享文档源（`docs/`）与 Astro Starlight 静态文档站（`frontend/docs/`） |

> **Alpha 状态说明：** PageHelm 仍处于早期研发阶段，请在隔离、可重置的合成测试环境中验证，勿直接接入生产账号、真实交易、邮件或客户库。
