---
title: 本地示例上手
description: 使用仓库内置参考应用 examples/yudao 在本地启动完整的 PageHelm 合成环境，并运行开发检查。
---

本页说明如何借助仓库内置的参考示例应用（`examples/yudao`，一套完整后台系统）在本地启动 PageHelm，体验同账号查询、页面操作和审批写入。

> 如果你准备将 PageHelm 接入自己的业务系统，不需要准备 `yudao` 示例缓存，请直接阅读 [应用声明式接入指南](./onboarding.md)。线上发布与备份恢复见 [部署、运维与排障](../security/operations-and-observability.md)。

## 一、前置条件与依赖安装

需预先安装 **Python 3.13**、**Node.js (>=22)**、**npm** 和 **Docker Compose 2.30+**。

1. 按 `examples/yudao/deploy/README.md` 准备好参考应用的本地构建缓存 `.q0-cache/yudao/`（服务端 JAR、管理端 `ui/dist-prod/`、DDL 及锁定版本的 `PageController`）。
2. 初始化 Python 虚拟环境并构建前端面板与浏览器 Runtime：

```bash
python3.13 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
python scripts/build_panel.py --install
python scripts/build_runtime.py --skip-build
```

## 二、配置密钥与运行模式

密钥与普通运行配置分别存放在 `.env` 和 `pagehelm.toml`（两者均已在 `.gitignore` 中忽略）：

```bash
cp .env.example .env
cp pagehelm.example.toml pagehelm.toml
```

在 `.env` 中仅填写所用模型服务商对应的 API Key：

```dotenv
# 按实际使用的 provider 选择其一
DEEPSEEK_API_KEY=your-key-here
# DASHSCOPE_API_KEY=your-key-here
# OPENAI_API_KEY=your-key-here
```

在 `pagehelm.toml` 中指定模型提供方和安全模式（TOML 中禁止出现密钥字段，完整字段与默认值见 [配置与通信协议](../reference/protocol-and-configuration.md)）：

```toml
provider = "deepseek"     # "deepseek" | "qwen" | "openai"
mode = "verified-operate" # "read-only" | "assisted-draft" | "verified-operate"
# daily_allowance_cny = 20.0
# turn_timeout_s = 120
```

未创建 `pagehelm.toml` 时，仍兼容原有 `AGENT_OPENAI_*` / `AGENT_PROVIDER` / `WOA_NATIVE_WRITES` 环境变量。所有使用的模型都必须在 `src/web_operation_agent/policies/prices.json` 中存在计价条目。

## 三、启动本地参考环境（`./local`）

本机提供两套相互隔离的 Docker Compose 实例，可同时运行：

| 实例 | 启动命令 | 浏览器地址 | Compose 项目名 |
| --- | --- | --- | --- |
| 环境 1（默认） | `./local` | `http://localhost:40001` | `woa-yudao-env1` |
| 环境 2 | `./local --env 2` | `http://localhost:40002` | `woa-yudao-env2` |

常用操作命令：

```bash
./local check             # 只读检查：Docker、缓存版本、pagehelm.toml、计价与策略
./local                   # 启动或更新环境 1（本地未显式设 mode 时默认 verified-operate）
./local --synthetic-clock # 将应用与 MySQL 时钟对齐合成数据基准日（2026-10-25T09:00:00+08:00）
./local status            # 核验业务接口、/readyz 与线上 assistant.js SHA-256
./local logs              # 查看助手日志（`./local logs app` 查看业务应用、MySQL 与 Redis 日志）
./local stop              # 仅停止助手容器，保留数据库与状态卷
```

实例私有状态保存在仓库外 `$XDG_STATE_HOME/web-operation-agent/yudao/env{1,2}/`（默认 `~/.local/state/web-operation-agent/yudao/`）。修改 Python 源码时只重启助手容器，修改前端时自动重编 `assistant.js`，不会重置数据库卷。

## 四、合成数据工具（按需手动运行）

`examples/yudao/deploy/` 下提供用于构造和核验演示数据的脚本，运行时需显式传入本机地址、MySQL 备份路径及 `--apply`（账号读取被忽略的 `.private/yudao-accounts.json`）：

- `demo_data.py`、`seed_scale.py`：补充多模块演示数据、长列表分页数据及只读探索账号 `demoexplorer`。
- `verify_demo_data.py`、`verify_demo_browser.py`：核对库存、账目平衡与浏览器分页展示。

线上环境发布、SQLite 备份恢复与未知写入对账见 [部署、运维与排障](../security/operations-and-observability.md)。

## 五、开发检查

仓库改动检查在 `.venv`（Python 3.13）下运行；自动化测试不会调用付费模型或改写费用账本：

```bash
.venv/bin/python scripts/check.py --plan              # 查看按当前改动选出的检查集
.venv/bin/python scripts/check.py                     # 运行 dev 检查通道
.venv/bin/python scripts/check.py --profile ci --base main  # 合入前 CI 检查
bash scripts/check_wheels.sh                          # 打包或依赖变更时运行
scripts/qualify.sh --require-browser                  # 发布候选全量验收
```

修改前端源码或切换工作树后，重新构建面板与 Runtime 产物：

```bash
.venv/bin/python scripts/build_panel.py && .venv/bin/python scripts/build_runtime.py --skip-build
```
