# Odoo 单机试用部署

这是面向单个组织私有试用的参考部署：在一台 Linux Docker Compose 主机上运行固定摘要的 Odoo 18、PostgreSQL 17、服务端 Agent 和 Caddy。Odoo 自己的登录会话建立浏览器身份；浏览器只把短期签名断言转交给 Agent，不能提供 actor id、API key、模型密钥、权限列表或审批。它不是多租户云架构。

## 前提与边界

需要当前版本的 Docker Engine 与 Compose v2、Python 3.12、Node.js 和干净 checkout。使用有持久 Docker volume 的私有 Linux 主机，只允许运维人员访问 shell 和 Docker。公开端口默认只绑定 loopback；允许远程客户端之前，应放到组织控制的入口之后。内置 Caddy CA 只适合在受控试用中把根证书安装到托管客户端；正式域名应改用组织证书。

部署为每个用户使用一把独立的 Odoo API key。应用自身的访问规则是最终权限来源，delegation manifest 只会进一步缩小 typed 工具 scope。删除 grant 不能撤回已经在执行的任务：需要在 Odoo 撤销 key、让用户退出登录；若必须立即关闭全部已签发的浏览器 capability，再重启 Agent。

## 准备与启动

构建 Odoo 镜像前，先下载并校验仓库固定版本的 PageController。生成的第三方文件由 Git 忽略，但会进入本地 Docker 构建上下文。

```bash
PYTHONPATH=src:examples/odoo/src .venv/bin/python -m web_operation_agent_odoo.deploy assets
python deploy/odoo-single-node/prepare.py --private-dir .private/odoo-single-node
```

把 `AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL` 和可选的 `AGENT_PROVIDER` 写进 `.private/odoo-single-node/model.env`，文件权限保持 `0600`。不调用模型的确定性资格验证可以设置 `WOA_SCRIPTED=1`。

显式选择以下值并在所有后续 Compose 与运维命令中保持一致，避免与开发夹具冲突。

```bash
export WOA_PROJECT_NAME=woa-odoo-pilot
export WOA_PRIVATE_DIR="$PWD/.private/odoo-single-node"
export WOA_PUBLIC_ORIGIN=https://localhost:18443
export WOA_HTTPS_PORT=18443
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"
docker compose -f deploy/odoo-single-node/docker-compose.yml build
docker compose -f deploy/odoo-single-node/docker-compose.yml --profile setup run --rm odoo-init
docker compose -f deploy/odoo-single-node/docker-compose.yml up -d db odoo
```

打开 `https://localhost:18443/web/login`，使用 `.private/odoo-single-node/odoo-admin-login-password` 中生成的管理员密码创建试用用户，并在每位用户自己的 profile 中创建一把有过期时间的 API key。记下 Odoo 的数字用户 id。每把 key 分别保存为只有运维人员可读、权限为 `0600` 的临时 JSON 文件：

```json
{"database":"woa_trial","login":"person@example.test","api_key":"替换为该用户自己的KEY"}
```

下面的只读用户登记命令会先用运行中的 Odoo 验证 API key 和 actor 是否一致，再把密钥复制进私有目录：

```bash
docker compose -f deploy/odoo-single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$PWD/reader-key.json:/run/woa-register/key.json:ro" -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --rpc-base-url http://odoo:8069 --actor-id 9 --api-key-file /run/woa-register/key.json --scope customers:read --scope invoices:read --scope products:read --scope sale_orders:read
```

写用户只登记业务所需的 scope。`--allow-mutations` 只让这些写 scope 有资格使用；部署级 `WOA_ALLOW_MUTATIONS=1` 和页面内针对精确效果的一次性确认仍然缺一不可。

```bash
docker compose -f deploy/odoo-single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$PWD/writer-key.json:/run/woa-register/key.json:ro" -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --rpc-base-url http://odoo:8069 --actor-id 8 --api-key-file /run/woa-register/key.json --scope customers:read --scope invoices:read --scope products:read --scope sale_orders:read --scope customers:write --scope invoices:write --scope sale_orders:write --allow-mutations
```

登记后删除临时输入文件并启动 Agent。普通用户登录 Odoo 后即可看到嵌入助手，不需要 Playwright、浏览器 profile 注入或独立 launcher。

```bash
export WOA_ALLOW_MUTATIONS=1
docker compose -f deploy/odoo-single-node/docker-compose.yml up -d
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/healthz"
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/readyz"
```

`healthz` 表示进程存活，`readyz` 只返回有界 session 与 provider 准入状态，不含用户内容。仅容器内部可读的 telemetry 快照可这样检查：

```bash
docker compose -f deploy/odoo-single-node/docker-compose.yml exec -T agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8767/internal/metrics').read().decode())"
```

默认上限是 10 个浏览器 session、全局 3 个活跃 provider 请求和每个 actor 2 个活跃请求。第 11 个 session 收到类型化的 `429 assistant_capacity`；逐 actor 准入防止单个用户耗尽全部 provider 名额。

## 可观测性与告警

把 `WOA_OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Agent 容器可访问的 OTLP/HTTP 接收端，例如组织托管或同一 Compose 网络中的本地 OpenTelemetry Collector。Agent 会向 `/v1/traces` 与 `/v1/metrics` 发送标准 JSON：span 覆盖模型、typed 工具、浏览器往返、答案核验与整轮执行，metric 覆盖模型排队、模型调用数和轮次耗时。导出异步且尽力而为，收集器故障不能改变任务或写入语义。

`WOA_ALERT_WEBHOOK_URL` 指向私有运维接收端。无业务内容的告警覆盖 provider 连续失败三次、写入结果不确定、备份失败和恢复失败；`WOA_COST_ALERT_CNY` 可以设置一次 run 的已知模型费用阈值。标签与告警都不含用户目标、记录字段、答案、原始身份、密钥或租户名称。监控信号不能替代审计账本和业务后置条件核验。

## 备份与恢复

至少每天执行一次部署备份，才能满足试用期 RPO 不超过 24 小时的目标。命令会停止 Agent 和 Odoo 写入端，用 SQLite 在线备份 API 保存 session、mutation 与 conversation 状态，生成 MVCC 一致的 PostgreSQL dump，归档 Odoo 附件，校验所有摘要，再重新启动服务。备份不含 API key、模型密钥、浏览器 capability、lease 和审批；私有凭据目录应由组织的密钥管理系统独立备份。

```bash
deploy/odoo-single-node/operations.py --project "$WOA_PROJECT_NAME" backup --destination .private/odoo-backups/2026-09-14
deploy/odoo-single-node/operations.py --project "$WOA_PROJECT_NAME" verify .private/odoo-backups/2026-09-14
```

恢复会破坏显式指定的隔离 Compose 项目，因此必须重复输入同一个项目名。恢复前先核对备份时点之后已经派发的业务写入：旧 journal 只是证据，绝不能触发重放。恢复流程会重建应用数据库和附件 volume、恢复 Agent 的持久记录、丢弃全部执行权限，而且不会调用模型或工具。

```bash
deploy/odoo-single-node/operations.py --project "$WOA_PROJECT_NAME" restore .private/odoo-backups/2026-09-14 --confirm-project "$WOA_PROJECT_NAME"
```

参考主机的实测恢复耗时为 19.34 秒，低于 30 分钟试用目标。Agent 直接重启也低于 5 分钟目标：稳定 session 身份与对话记录保留，新 capability 重新签发，旧 capability 被拒绝。这是单台资格主机的观测数据，不是普遍时延保证。

## 升级与回滚

每次升级前先完成并校验备份。将当前镜像保存为不可变的本地或仓库 tag，用准确源码 revision 构建候选镜像，记录摘要，只重建 Agent，然后执行 readiness 和普通浏览器检查。涉及数据库或 addon migration 时必须另行设计 Odoo 数据回滚；镜像回退不会反向撤销数据迁移。

```bash
docker image tag "$WOA_PROJECT_NAME-agent:latest" "$WOA_PROJECT_NAME-agent:rollback-$(date +%Y%m%d)"
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"
docker compose -f deploy/odoo-single-node/docker-compose.yml build agent
docker compose -f deploy/odoo-single-node/docker-compose.yml up -d --no-deps --force-recreate agent
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/readyz"
```

若候选版本在数据迁移前资格验证失败，把保存的镜像摘要重新标为 `latest`，重建 Agent，再执行 readiness 和一条只读浏览器旅程。P7.3 已实测这条候选升级与镜像回滚路径，应用数据库和持久审计状态均未丢失。

## 试用限制

该拓扑的每个服务都只有一个进程并位于一台机器。活跃模型调用和页面动作不会迁移；重启只保留安全历史与未决写入记录，不会恢复任务或审批。内置 TLS 与 loopback listener 需要由远程部署的入口和托管证书替换。密钥目前是文件输入，备份依赖运维执行且不做异地复制，OTel 接收器、仪表盘与通知路由也由运维方提供。跨主机扩缩容、跨地域恢复、企业 SSO、自动密钥轮换和托管连接器控制面均不在本试用范围内。
