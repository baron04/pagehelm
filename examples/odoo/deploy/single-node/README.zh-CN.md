# Odoo 单机试用部署

这是面向单个组织私有试用的参考部署：在一台 Linux Docker Compose 主机上运行固定摘要的 Odoo 18、PostgreSQL 17、服务端 Agent 和独立 Caddy 网关。Odoo 自己的登录会话建立浏览器身份；浏览器只把短期签名断言转交给 Agent，不能提供 actor id、API key、模型密钥、权限列表或审批。助手空闲时会在过期前取得新断言；只有 Odoo 浏览器身份和委托 API 身份仍指向同一 actor、公司、scope 与 delegation，Agent 才为原会话续期。它不是多租户云架构。

## 前提与边界

需要当前版本的 Docker Engine 与 Compose v2、Python 3.12 或 3.13、Node.js 和干净 checkout。所有命令由同一个非 root 部署账号在 `umask 077` 下执行，该账号应拥有 checkout、私有输入目录和备份目标。使用有持久 Docker volume 的私有 Linux 主机，只允许运维人员访问 shell 和 Docker。公开端口默认只绑定 loopback；允许远程客户端之前，应放到组织控制的入口之后。内置 Caddy CA 只适合在受控试用中把根证书安装到托管客户端；正式域名应改用组织证书。

部署为每个用户使用一把独立的 Odoo API key。应用自身的访问规则是最终权限来源，delegation manifest 只会进一步缩小 typed 工具 scope。任务进行中或存在待确认操作时不会续期；配置的 key 路径、scope 或 mutation grant 发生变化也会拒绝续期。登记、替换或删除 delegation 后必须重启 Agent，登记命令会明确返回该要求。在 Odoo 撤销 key 并让用户退出登录会关闭已签发的浏览器 capability，并写入持久 actor 撤销水位。

该试用拓扑把应用数据库固定为 `woa_trial`。初始化、Odoo `dbfilter`、资格数据、备份和恢复均使用同一名称，不提供只接通部分链路的数据库名覆盖参数。

## 准备与启动

构建 Odoo 镜像前，先下载并校验仓库固定版本的 PageController。生成的第三方文件由 Git 忽略，但会进入本地 Docker 构建上下文。

```bash
PYTHONPATH=src:examples/odoo/src .venv/bin/python -m web_operation_agent_odoo.deploy assets
python examples/odoo/deploy/single-node/prepare.py --private-dir .private/odoo-single-node
```

把 `AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL` 和可选的 `AGENT_PROVIDER` 写进 `.private/odoo-single-node/model.env`，文件权限保持 `0600`。不调用模型的确定性资格验证可以设置 `WOA_SCRIPTED=1`。

显式选择以下值并在所有后续 Compose 与运维命令中保持一致，避免与开发夹具冲突。

```bash
export WOA_PROJECT_NAME=woa-odoo-pilot
export WOA_PRIVATE_DIR="$PWD/.private/odoo-single-node"
export WOA_PUBLIC_ORIGIN=https://localhost:18443
export WOA_HTTPS_PORT=18443
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml build
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml --profile setup run --rm odoo-init
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml up -d db odoo gateway
```

打开 `https://localhost:18443/web/login`，使用 `.private/odoo-single-node/odoo-admin-login-password` 中生成的管理员密码创建试用用户，并在每位用户自己的 profile 中创建一把有过期时间的 API key。记下 Odoo 的数字用户 id。每把 key 分别保存为只有运维人员可读、权限为 `0600` 的临时 JSON 文件：

```json
{"database":"woa_trial","login":"person@example.test","api_key":"替换为该用户自己的KEY"}
```

下面的只读用户登记命令会先用运行中的 Odoo 验证 API key 和 actor 是否一致，再把密钥复制进私有目录：

```bash
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$PWD/reader-key.json:/run/woa-register/key.json:ro" -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --rpc-base-url http://odoo:8069 --actor-id 9 --api-key-file /run/woa-register/key.json --scope customers:read --scope invoices:read --scope products:read --scope sale_orders:read
```

写用户只登记业务所需的 scope。`--allow-mutations` 只让这些写 scope 有资格使用；部署级 `WOA_ALLOW_MUTATIONS=1` 和页面内针对精确效果的一次性确认仍然缺一不可。

```bash
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$PWD/writer-key.json:/run/woa-register/key.json:ro" -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --rpc-base-url http://odoo:8069 --actor-id 8 --api-key-file /run/woa-register/key.json --scope customers:read --scope invoices:read --scope products:read --scope sale_orders:read --scope customers:write --scope invoices:write --scope sale_orders:write --allow-mutations
```

删除 delegation 时不需要再次提供旧 key，执行后重启 Agent：

```bash
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --actor-id 8 --database woa_trial --remove
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml restart agent
```

登记后删除临时输入文件。每次登记、替换或删除 delegation 后都重启 Agent，使其重新加载 manifest 和 `delegated-keys` 中实际引用的文件。Agent 容器不会挂载数据库密码、Odoo master password 或用户登录密码。普通用户登录 Odoo 后即可看到嵌入助手，不需要 Playwright、浏览器 profile 注入或独立 launcher。

```bash
export WOA_ALLOW_MUTATIONS=1
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml up -d
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/healthz"
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml exec -T agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8766/readyz').read().decode())"
```

同源浏览器请求可能不带 `Origin`；参考 Caddy 入口只在该请求头缺失时补入 `WOA_PUBLIC_ORIGIN`。显式携带的 `Origin` 会原样交给核心 gateway 校验，不匹配时仍被拒绝。gateway 必须继续只监听 loopback，Odoo 与 `/assistant/` 必须经过同一个可信入口，`WOA_PUBLIC_ORIGIN` 必须与浏览器实际看到的 origin 完全一致。

`healthz` 表示 Agent 进程存活。`readyz` 只返回有界 session 与 provider 准入状态，不含用户内容，并且不会由公开网关路由。仅容器内部可读的 telemetry 快照可这样检查：

```bash
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml exec -T agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8767/internal/metrics').read().decode())"
```

默认上限是 10 个浏览器 session、每个 actor 3 个浏览器 session、全局 3 个活跃 provider 请求和每个 actor 2 个活跃请求。浏览器连续 90 秒没有发出经过认证的 Agent 请求会释放 session 名额，服务端内部 owner heartbeat 不计作浏览器在线。容量耗尽会返回类型化的 `429 assistant_capacity`。

Caddy 是独立服务。Agent 不可用时 Odoo 仍可使用，`/assistant/*` 返回 HTTP 503。Agent 容器健康检查只检查进程存活，因此 provider 故障不会导致 Compose 杀死健康进程；是否接收助手流量应通过上面的内部 `readyz` 命令判断。

## 可观测性与告警

把 `WOA_OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Agent 容器可访问的 OTLP/HTTP 接收端，例如组织托管或同一 Compose 网络中的本地 OpenTelemetry Collector。Agent 会向 `/v1/traces` 与 `/v1/metrics` 发送标准 JSON：span 覆盖模型、typed 工具、浏览器往返、答案核验与整轮执行，metric 覆盖模型排队、模型调用数和轮次耗时。导出异步且尽力而为，收集器故障不能改变任务或写入语义。

`WOA_ALERT_WEBHOOK_URL` 指向私有运维接收端。无业务内容的告警覆盖 provider 连续失败三次、已派发请求连续三次超时、写入结果不确定或仍未解决、保留任务失败、备份失败和恢复失败；`WOA_COST_ALERT_CNY` 可以设置一次 run 的已知模型费用阈值。用户主动取消不计入 provider 可用性故障。标签与告警都不含用户目标、记录字段、答案、原始身份、密钥或租户名称。监控信号不能替代审计账本和业务后置条件核验。

Agent 每小时执行一次保留任务：未决 mutation 行以及存在任意未决写时的全部 run 账本始终保留；已解决 mutation、非活跃 session 和 conversation 状态保留 30 天；过期签名事件 nonce 与旧撤销水位会被清理；缺少终态事件的 telemetry run 会被关闭。保留任务失败会计数并告警，但不会授权删除或重试。

## 备份与恢复

至少每天执行一次部署备份，才能满足试用期 RPO 不超过 24 小时的目标。命令会停止 Agent 和 Odoo 写入端，用 SQLite 在线备份 API 保存 session、mutation 与 conversation 状态，生成 MVCC 一致的 PostgreSQL dump，归档 Odoo 附件，校验所有摘要，再重新启动服务。备份不含 API key、模型密钥、浏览器 capability、lease 和审批；私有凭据目录应由组织的密钥管理系统独立备份。

```bash
examples/odoo/deploy/single-node/operations.py --project "$WOA_PROJECT_NAME" backup --destination .private/odoo-backups/2026-09-14
examples/odoo/deploy/single-node/operations.py --project "$WOA_PROJECT_NAME" verify .private/odoo-backups/2026-09-14
```

恢复会破坏显式指定的隔离 Compose 项目，因此必须重复输入同一个项目名。恢复前先核对备份时点之后已经派发的业务写入：旧 journal 只是证据，绝不能触发重放。命令会拒绝备份或当前部署中的未决 mutation，先创建并验证恢复前安全备份，再把数据库、附件和 Agent 状态恢复到 staging 命名空间，全部可读后才执行短切换。失败时 Agent 与 Odoo 保持停止，并返回安全备份路径。恢复后的状态不含执行权限，也不会调用模型或工具。备份与恢复只重新启动它们停掉的那些服务，自己需要的一次性容器所用的镜像和私有目录从运行中的部署读取，因此在只导出了项目名的 shell 里执行，也不会把部署按本文件的默认值重建。

```bash
examples/odoo/deploy/single-node/operations.py --project "$WOA_PROJECT_NAME" restore .private/odoo-backups/2026-09-14 --confirm-project "$WOA_PROJECT_NAME"
```

在目标主机实测之前不能设定 RTO。成功命令会返回 `recovery_seconds`；跳过或失败的演练不构成恢复证据。每次演练后都应执行普通只读查询，确认旧 cookie 和浏览器 capability 被拒绝，并在重新开放写入前检查未决 mutation 状态。

## 升级与回滚

每次升级前先完成并校验备份。将当前镜像保存为不可变的本地或仓库 tag，用准确源码 revision 构建候选镜像，记录摘要，只重建 Agent，然后执行 readiness 和普通浏览器检查。涉及数据库或 addon migration 时必须另行设计 Odoo 数据回滚；镜像回退不会反向撤销数据迁移。

```bash
docker image tag "$WOA_PROJECT_NAME-agent:latest" "$WOA_PROJECT_NAME-agent:rollback-$(date +%Y%m%d)"
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml build agent
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml up -d --no-deps --force-recreate agent
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml exec -T agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8766/readyz').read().decode())"
```

助手的浏览器端代码（`assistant.js` 与面板）由 Agent 提供，升级它只需重建 Agent。当发布改动了 `woa_runtime` 插件本身时，还要重新构建镜像并重新创建 Odoo 容器。Odoo 入口会在开始接受流量前幂等升级该插件，因此挂载进来的模板或控制器不会继续以数据库中的旧版本运行并静默移除助手入口。独立的 setup 步骤只在首次创建数据库并设置生成的管理员密码时仍然必需。

```bash
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml build odoo
docker compose -f examples/odoo/deploy/single-node/docker-compose.yml up -d --no-deps --force-recreate odoo
```

若候选版本在数据迁移前资格验证失败，把保存的镜像摘要重新标为 `latest`，重建 Agent，再执行 readiness 和一条只读浏览器旅程。每个部署都应记录镜像摘要、检查项和审计状态检查结果；没有实际执行的回滚命令只是操作流程，不是验证证据。

## 试用限制

该拓扑的每个服务都只有一个进程并位于一台机器。活跃模型调用和页面动作不会迁移；重启只保留安全历史与未决写入记录，不会恢复任务或审批。内置 TLS 与 loopback listener 需要由远程部署的入口和托管证书替换。密钥目前是文件输入，备份依赖运维执行且不做异地复制，OTel 接收器、仪表盘与通知路由也由运维方提供。跨主机扩缩容、跨地域恢复、企业 SSO、自动密钥轮换和托管连接器控制面均不在本试用范围内。
