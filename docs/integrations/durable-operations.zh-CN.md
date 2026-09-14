# 本机持久化运维

协作进程使用本机文件系统上的同一私有 SQLite 数据库。此后端不支持网络文件系统或跨主机部署。升级既有 mutation journal 前须停止所有旧 worker。

```python
from web_operation_agent.durable import (
    SQLitePrincipalRevocations,
    SQLiteProviderQuota,
    SQLiteSessionStore,
)
from web_operation_agent.live import LiveProductServer

store = SQLiteSessionStore("private/sessions.sqlite3")
server = LiveProductServer(durable_store=store)
quota = SQLiteProviderQuota(store, scope="tenant-id/provider-account", max_requests=60)
revocations = SQLitePrincipalRevocations(store)
# 创建 ChatModel 时传入 shared_quota=quota。
# 创建 LiveProductServer 时传入 principal_validator=revocations.validate。
```

宿主从应用登录边界重新获取有效 principal，然后在旧 owner 释放或租约过期后，以同一稳定 session ID 恢复。应用、租户、工作区、actor 和允许的 Origin 必须相同。存储层递增 owner fence，Registry 重新签发 capability，浏览器握手重新签发 lease。凭据、页面观察、动作和审批都不恢复。已接受消息保留去重记录，中断消息不自动重跑。宿主可读取 `channel.recovered_history` 展示有界历史进度；它不授予当前事实或权限。模型的内存对话上下文会重新建立。

心跳续期 owner。被 fencing 的旧 worker 无法继续发布动作，取消检查也会失败关闭。已在途的 HTTP 写入不能被 fence 撤回，其业务键继续由 mutation journal 持有，直到有确定结果或运维裁决。所有协作 worker 必须使用同一个存储，独立数据库不会协调。

Provider quota 是按服务端指定 scope 共享的滚动请求额度。同一 scope 的上限和窗口必须一致，冲突配置会被拒绝。额度预留采取保守方式，不退款；耗尽时在 provider 派发前拒绝，不产生费用行。这不是分布式 token/金额配额，也不是全局并发限制；原来的进程内并发 guard 继续工作。

应用报告注销、凭据撤销或权限变化时，调用 `revocations.revoke(principal, reason="application_logout")`。验证器会在认证 channel 边界运行，包括接收消息、页面动作、审批与模型取消检查之前。已撤销会话会在下一个未认领操作派发前关闭。该次签发的 principal 永久失效；重新登录必须绑定新 principal 并获得新 capability。表中只保存 principal 摘要、时间戳与有界原因码。何时调用该钩子仍由应用自身的登录或会话状态决定。

通过 `store.prune(older_than_ms=...)` 清理至少一天前的不活跃会话历史；活跃会话的去重记录保留。未解决 mutation 应单独保留，删除可能放行重复写入。SQLite 文件需文件权限保护，业务数据要求时还需磁盘加密。保存的 mutation 参数是规范化业务数据，不只是摘要。

## 备份与恢复本机状态

不要用 `cp` 复制正在工作的 SQLite 数据库：已提交页面可能仍在 WAL 中。运维命令使用 SQLite 在线备份 API，检查每份快照，生成不含主机路径的内容摘要清单，并以原子目录切换发布备份。

```bash
python -m web_operation_agent.operations backup \
  --sessions private/sessions.sqlite3 \
  --mutations private/mutations.sqlite3 \
  --destination backups/2026-09-14
python -m web_operation_agent.operations verify backups/2026-09-14
```

恢复只能写入新目录。先停止并隔离所有 writer，再明确声明该条件：

```bash
python -m web_operation_agent.operations restore backups/2026-09-14 \
  --destination recovery/2026-09-14 \
  --writer-isolated
```

目标目录已存在、文件被修改或未声明、符号链接、SQLite 完整性检查失败，或者没有隔离 writer 时，命令都会拒绝。结果会报告处于 `dispatching` 与 `indeterminate` 状态的未解决 mutation。在逐条核对这些记录及备份时点之后的应用派发前，恢复后的部署必须保持禁止写入。

备份不包含浏览器 capability、Runtime lease、审批、委托凭据或模型继续执行状态。恢复后的宿主必须重新认证 actor 并签发新 capability；恢复的进度仅是历史，不能授权或重放操作。应用数据库、附件存储、密钥和配置须通过各自支持的方式纳入同一恢复方案；本命令只覆盖框架的两个 SQLite 文件。

## 查看和裁决不确定写入

```bash
python -m web_operation_agent.mutation_ops --journal private/mutations.sqlite3 list
python -m web_operation_agent.mutation_ops --journal private/mutations.sqlite3 resolve PROPOSAL_DIGEST --expected-updated-at TIMESTAMP --outcome completed --operator OPERATOR_ID --evidence verified-evidence.json --writer-stopped
```

第二条命令默认 dry-run。确认旧 writer 及上游请求不可能继续执行，并取得完成或确定失败的证据后，才使用 `--apply`。当前存在同名记录不能证明是此 proposal 创建，当前不存在记录也不能排除在途请求。时间戳比较防止旧裁决覆盖新状态；裁决记录及 operator/evidence 摘要与状态变化在同一事务提交。不会重试原写入或恢复审批。

`mutation_ops.reconcile(journal, proposal_digest, application=bound_adapter, principal=fresh_principal)` 使用原 actor 的新鲜身份执行应用拥有的只读核对，不改变 journal。Twenty 报告当前匹配记录数与扫描完整性，保守返回需人工处理的 unknown。CLI 不自动查找凭据或加载任意 Adapter。
