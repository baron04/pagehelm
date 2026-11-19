---
title: 5 分钟快速上手与运维
description: 使用 .env 与 pagehelm.toml 完成极简配置，运行本地双环境 ./local --synthetic-clock、线上部署 ./deploy 与开发检查。
---

> **概要：** `.env` 只需填写 1–3 行模型 API Key，在 `pagehelm.toml` 指定 `provider` 与安全 `mode`，即可用 `./local` 或 `./local --synthetic-clock` 拉起全栈本地合成环境。线上发布使用 `./deploy`，持久化依赖 SQLite 在线备份与只读对账，改动通过 `check.py` 分流核验。

[English](../../integration/quickstart.md)

## 一、开发与运行依赖准备

需预先安装 **Python 3.13**、**Node.js (>=22)**、**npm** 以及 **Docker Compose 2.30+**。

```bash
python3.13 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
python scripts/build_panel.py --install
python scripts/build_runtime.py --skip-build
```

## 二、极简配置（`.env` + `pagehelm.toml`）

凭据与非敏感行为配置彻底分离（两者均已被 `.gitignore` 忽略）：

```bash
cp .env.example .env
cp pagehelm.example.toml pagehelm.toml
```

在 `.env` 中只填入对应服务商的 API Key（1–3 行）：

```dotenv
DEEPSEEK_API_KEY=your-key-here
```

在 `pagehelm.toml` 中设置 `provider`（`deepseek`、`qwen`、`openai`，自动推导官方 `base_url` 与已登记计价的默认 `model`，均可覆盖）和 `mode`（`read-only`、`assisted-draft`、`verified-operate`）：

```toml
provider = "deepseek"
mode = "verified-operate"
```

> 兼容提示：未创建 `pagehelm.toml` 时，原有的 `AGENT_OPENAI_BASE_URL` / `AGENT_OPENAI_API_KEY` / `AGENT_OPENAI_MODEL` + `WOA_NATIVE_WRITES` 写法继续兼容。所有使用的模型必须在 `src/web_operation_agent/policies/prices.json` 中有计价条目。

## 三、启动本地参考环境（`./local`）

首次使用 `./local` 前，请先按 [examples/yudao/deploy/README.md](../../../examples/yudao/deploy/README.md) 准备好 `.q0-cache/yudao/` 中的固定缓存产物（服务端 JAR、管理端 `ui/dist-prod/`、DDL 与锁定版本 PageController）。

本机提供两套完全隔离的 Compose 实例（同一环境串行、双环境可并行）：

| 实例 | 启动命令 | 浏览器地址 | Compose 项目名 |
| --- | --- | --- | --- |
| 环境 1（默认） | `./local` | `http://localhost:40001` | `woa-yudao-env1` |
| 环境 2 | `./local --env 2` | `http://localhost:40002` | `woa-yudao-env2` |

### 常用命令

```bash
./local check                  # 只读前置检查：Docker、缓存版本、pagehelm.toml、计价与策略
./local                        # 启动或更新环境 1（未显式设 mode 时本地默认 verified-operate）
./local --synthetic-clock      # 将应用与 MySQL 时钟对齐合成数据基准日（2026-10-25T09:00:00+08:00）
./local status                 # 核验业务接口、助手就绪及线上 assistant.js SHA-256
./local logs                   # 查看助手日志（`logs app` 查看业务应用、MySQL 与 Redis 日志）
./local stop                   # 仅停止助手容器，保留数据库与实例状态卷
```

实例私有状态保存在仓库外 `$XDG_STATE_HOME/web-operation-agent/yudao/env{1,2}/`（默认 `~/.local/state/web-operation-agent/yudao/`）。修改 Python 源码只重启助手容器，修改前端自动重编 `assistant.js`，不会无故重置数据库卷。

### 合成数据工具（显式手动运行）

位于 `examples/yudao/deploy/`，须传入本机地址、MySQL 压缩备份及 `--apply`，账号读取被忽略的 `.private/yudao-accounts.json`：

- `demo_data.py`、`seed_scale.py` — 补充多模块演示数据、长列表分页数据及只读探索账号 `demoexplorer`；
- `verify_demo_data.py`、`verify_demo_browser.py` — 核对库存、账目平衡与浏览器分页展示。

## 四、线上部署与持久化运维（`./deploy`）

云端站点默认以 **`read-only`** 运行（关闭写入、编辑预填与用户态源码检索），前端静态资源与代理受只读 HTTP 方法围栏保护。

```bash
cp deploy.example.toml deploy.local.toml   # 填写 ssh_target 与 public_origin（已忽略）
./deploy check                             # 只读检查本机产物与 SSH 服务器前置条件
./deploy                                   # 编译前端与 wheel、上传发布、健康核验并切换 current
./deploy status                            # 查看远程 release.json 并比对线上 assistant.js 摘要
```

- **优雅停机（SIGTERM 排空）：** 宿主收到停止信号后不再接新回合，等待在途写入派发与回读完成（上限 `shutdown_drain_timeout_s`）后退出。
- **SQLite 在线备份与隔离恢复：** 请使用 `python -m web_operation_agent.composition.operations backup|verify|restore`，切勿直接 `cp` 活跃 `-wal` 库。恢复会校验完整性、要求 `--writer-isolated` 新目录，并列出所有未决 `dispatching`/`indeterminate` 笔数，核对清楚前保持写入关闭。
- **未决写入裁决：** 用 `python -m web_operation_agent.business.operations --journal <path> list` 排查，仅在确认旧请求不会继续执行并查明应用状态后以 `--apply --writer-stopped` 裁决，或经面板调用 `POST /v1/sessions/<id>/reconciliations` 同账号只读核对。

## 五、开发检查

所有检查均在 `.venv`（Python 3.13）运行，严禁测试中调用付费模型或改写评价费用账本：

```bash
.venv/bin/python scripts/check.py --plan              # 预览按当前改动自动选出的检查集合
.venv/bin/python scripts/check.py                     # 运行 dev 检查通道
.venv/bin/python scripts/check.py --profile ci --base main   # 合入前完整 CI 门禁
bash scripts/check_wheels.sh                          # 打包或依赖改动必跑
scripts/qualify.sh --require-browser                  # 发布候选全量资格验收
```

前端源码改动或切换独立工作树后，需重新运行 `scripts/build_panel.py` 与 `scripts/build_runtime.py --skip-build`。
