# Web Operation Agent

[English](README.md)

把 AI 助手嵌入第一方 Web 应用的 alpha 框架。服务端负责规划、模型访问、凭据、审批和审计；浏览器观察页面、执行固定工具并展示进度。

[芋道](examples/yudao/deploy/README.md)是默认演示与测试应用；[Odoo 18 接入](examples/odoo/README.md)保留作参考，已不再维护。接口可能变化，目前不具备生产上线条件。

## 能力与架构

- 应用拥有的 Skill、类型化读取与可选源码检索。
- 目标式查询、导航，以及经审核的表单预填后交接给用户。
- 写入默认关闭；开启后须取得准确审批、单次派发并由应用核验。不确定写入从不自动重试。
- 由服务端状态驱动的 React 面板、多页会话与可选 SQLite 持久化。

```text
第一方页面 + 浏览器 Runtime
              ↕ 已认证的观察与动作
应用无关的服务端 + 会话策略/模型/审批/审计
              ↕ 类型化应用端口
应用 Adapter + 身份/路由/读取/写入/核验
```

应用须提供可信身份、权限和核验。浏览器操作不能提供数据库事务或回滚；已实现路径与小样本评测不能证明总体准确率。详见[运行时行为与限制](docs/reference/runtime-behavior.md)、[系统设计](docs/concepts/overview.md)和[安全边界](SECURITY.md)。

## 运行芋道

| 本地环境 | 命令 | 地址 |
| --- | --- | --- |
| 环境1（默认） | `./local` | <http://localhost:40001> |
| 环境2 | `./local --env 2` | <http://localhost:40002> |

每套 Compose 包含助手、业务应用、数据库和 Redis。源码从 `local` 所在目录挂载，普通源码更新只重启所选助手。准备与诊断见[本地指南](docs/guides/local-yudao.md)，Skill 为 `$local-yudao`。

服务器配置完成后用 `./deploy` 部署；准备与恢复见[部署指南](docs/guides/deploy-yudao.md)，Skill 为 `$deploy-yudao`。

## 开发

使用 Python 3.12（也支持 3.13）和 Node.js：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
python -m pip install --no-deps --editable examples/yudao
cp .env.example .env  # 只填需要的配置；不要提交 .env
python scripts/build_runtime.py --install
python scripts/build_panel.py
python scripts/check.py --plan
python scripts/check.py
```

前端变化后及每个新 checkout 都需构建浏览器资源。模型配置与容器更新按本地/部署指南操作。检查分流、合入验证与显式完整资格见[开发指南](docs/guides/development-checks.md)。

源码：`src/`（通用核心）、`frontend/`（浏览器代码）、`examples/`（适配器与部署）、`testapps/`（合成测试应用）、`evals/`（独立评测装置）。

[文档索引](docs/README.md) · [接入应用](docs/guides/integrate-an-application.md) · [写入模型](docs/concepts/writes.md) · [持久化运维](docs/guides/operate-durably.md)

## 许可证

[LICENSE](LICENSE) · [THIRD_PARTY.md](THIRD_PARTY.md)
