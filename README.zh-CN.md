# PageHelm

[English](README.md)

**PageHelm**（核心包 `web_operation_agent`）是一个面向第一方 Web 应用的嵌入式受控 AI 操作助手框架（alpha 阶段）。

- **服务端集中掌舵：** 模型密钥、Skill 规程、会话策略、安全门禁与审计日志始终留在服务端，不进入浏览器。
- **同账号混合双轨执行：** 查数走服务端同账号授权 API + 内存 `TurnTables` SQL（`<DataTable>` 分页导出），定位与预填走页面同源 DOM（`assistant.js`、`PageController`）。
- **鲜活可见写入审批：** 写入默认关闭（`read-only`）；开启后须经差异审批卡、`event.isTrusted` 手势、SQLite 单次消费、同账号回读核验，未决态（`INDETERMINATE`）永不自动重试。
- **L0–L3 渐进接入：** 先嵌入单文件面板并复用仓内 `frontend/src/integrations/element-plus/` 公共层，再逐步接入只读端口、领域指南与核验写入。

## 系统架构

```text
第一方页面（assistant.js + Shadow DOM 面板 + PageController）
                        ^ 已认证的页面观察 / 动作回执 / 审批决定
                        v
应用无关服务端（Executor · TurnHarness · WritePipeline · SQLite 存储）
                        ^ 类型化端口（ApplicationPorts & GoalData）
                        v
应用适配器（身份反查 · 路由校验 · 同账号读取 · 核验写入）
```

## 5 分钟快速上手

需 Python 3.13、Node.js（>=22）与 Docker Compose 2.30+。首次运行前按[部署说明](examples/yudao/deploy/README.md)准备 `.q0-cache/yudao/`。

```bash
cp .env.example .env                    # 只填 1–3 行模型 API Key（如 DEEPSEEK_API_KEY）
cp pagehelm.example.toml pagehelm.toml  # 选择 provider（deepseek/qwen/openai）与安全 mode
./local --synthetic-clock               # 拉起本地合成环境 1：http://localhost:40001
```

## L0–L3 分级接入

| 等级 | 接入工作 | 获得能力 | 仓内参考 |
| --- | --- | --- | --- |
| **L0** | 挂载 `/v1/runtime/assistant.js` 与 `/bootstrap` | Shadow DOM 面板、页面操作、Element Plus 公共集成层 | `frontend/src/integrations/element-plus/` |
| **L1** | 实现身份、路由与只读数据端口 | 同账号查询、`TurnTables` SQL 与 `<DataTable>` 交付 | [`testapps/contacts`](testapps/contacts/adapter.py) |
| **L2** | 补充领域指南、约定与 `entity_rules` | 模块消歧、编译期拒绝条件、`need_input` 胶囊 | [L0–L3 接入指南](docs/zh-cn/integration/levels.md) |
| **L3** | 接入写入账本、后置核验与 Skill / `SubmitPort` | 类型化写入、对称 `update_record`、原生提交与撤销 | [安全与核验写入](docs/zh-cn/security/verified-writes.md) |

## 安全模式矩阵与方案对比

| 模式（`mode`） | 读取与导航 | 表单预填 | 写入提案与审批卡 | 实际写入派发 |
| --- | --- | --- | --- | --- |
| `read-only`（生产默认） | 只读窗口下放行 | 关闭 | 关闭 | 关闭 |
| `assisted-draft` | 只读窗口下放行 | 开启（预填交接与撤销） | 仅准备提案 | 关闭（绝不派发） |
| `verified-operate`（本地默认） | 只读窗口下放行 | 开启 | 完整开启 | 获批后至多派发一次并回读 |

| 对比维度 | 纯 API 对话 / RAG | 外部无头浏览器 / RPA | 低代码工作流画布 | **PageHelm** |
| --- | --- | --- | --- | --- |
| 真实界面定位与表单预填 | 不支持 | 纯视觉/DOM 慢速试错 | 不支持 | 同源语义 DOM + 人工交接 |
| 数千行列表汇总分析 | 依赖现成聚合接口 | 逐页点击耗时高、易漏页 | 节点内存与超时受限 | 同账号接口 + 内存 SQL |
| 身份与数据权限继承 | 常见全局服务密钥 | 服务端托管用户 Cookie | 静态共享凭据 | 浏览器会话绑定同账号令牌 |
| 写入安全与防重复机制 | 依赖各工具自行处理 | 易误触原生保存按钮 | 超时重跑有重复写入风险 | 单次审批 `WritePipeline` + 未决态锁死 |

## 本地开发

```bash
python3.13 -m venv .venv && . .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
.venv/bin/python scripts/build_panel.py --install && .venv/bin/python scripts/build_runtime.py --skip-build
.venv/bin/python scripts/check.py --plan && .venv/bin/python scripts/check.py
```

## 文档与合规声明

- **文档入口：** [简体中文](docs/zh-cn/index.md) · [English](docs/index.md) · [本地示例上手](docs/zh-cn/integration/quickstart.md) · [L0–L3 接入](docs/zh-cn/integration/levels.md) · [Skill 与写入](docs/zh-cn/integration/skills-and-writes.md) · [架构总览](docs/zh-cn/architecture/overview.md) · [执行引擎与运行时](docs/zh-cn/architecture/execution-and-runtime.md) · [写入安全与隐私](docs/zh-cn/security/verified-writes.md) · [部署与运维](docs/zh-cn/security/operations-and-observability.md) · [配置与协议](docs/zh-cn/reference/protocol-and-configuration.md) · [工具与能力](docs/zh-cn/reference/tools-and-capabilities.md)
- **文档站（`frontend/docs/`）：** 直接读取 `docs/` 由 Astro Starlight 构建，同步生成 `/llms.txt` 与 `/llms-full.txt`。
- **提示：** Alpha 阶段框架，仅在隔离合成数据下使用。[SECURITY](SECURITY.md) · [LICENSE](LICENSE) · [THIRD_PARTY](THIRD_PARTY.md)
