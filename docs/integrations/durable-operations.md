# Durable local operations

Use a shared private SQLite database on a local filesystem for cooperating worker processes. Network filesystems and multiple hosts are outside this backend's supported scope. Stop all old workers before upgrading an existing mutation journal.

```python
from web_operation_agent.state import (
    SQLitePrincipalRevocations,
    SQLiteProviderQuota,
    SQLiteSessionStore,
)
from web_operation_agent.composition.live import LiveProductServer

store = SQLiteSessionStore("private/sessions.sqlite3")
server = LiveProductServer(durable_store=store)
quota = SQLiteProviderQuota(store, scope="tenant-id/provider-account", max_requests=60)
revocations = SQLitePrincipalRevocations(store)
# Pass shared_quota=quota when constructing ChatModel.
# Pass principal_validator=revocations.validate to LiveProductServer.
```

The host must obtain a fresh authenticated principal from its application login boundary and supply it when creating a session. To recover, reuse the same stable session ID with that fresh principal after the former owner releases or loses its lease. Application, tenant, workspace, actor and allowed origins must match. The store rotates ownership fences; the registry issues a new capability and the browser handshake issues a new lease. Credentials, observations, actions and approvals are never restored. Accepted messages retain their deduplication tombstones; interrupted messages are not automatically executed again. The host can access `channel.recovered_history` for bounded historical progress, which grants no current facts or permissions. The in-memory model conversation context is intentionally rebuilt rather than restored as authority.

A heartbeat renews ownership. Fenced workers stop publishing actions and fail cancellation checks. An HTTP write already in flight cannot be undone by a fence: the mutation journal continues to hold its business key until the result is definitive or an operator resolves it. Exactly one owner requires every cooperating worker to use this store; independent databases do not coordinate.

The provider quota is a rolling request budget shared by its server-assigned scope. Workers must agree on the same limit/window for that scope; conflicting policies are rejected. Reservations are conservative and not refunded. Exhaustion occurs before provider dispatch and creates no spend row. This is a request quota, not a distributed token/currency quota or global concurrency limiter. The existing process-local concurrency guard remains active.

When the application reports logout, credential revocation, or an access change, call `revocations.revoke(principal, reason="application_logout")`. The validator runs at authenticated channel boundaries, including before message acceptance, page actions, approval and model cancellation checks. A revoked session closes before another unclaimed operation can be dispatched. The exact issued principal stays revoked; a new application login must bind a new principal and receives a new capability. The table stores only a principal digest, timestamp and bounded reason code. Application login/session state remains the authority that decides when to call this hook.

Inactive session histories can be removed with `store.prune(older_than_ms=...)`, with a cutoff at least one day old. Active session tombstones are retained. Operators should retain unresolved mutation records independently; deleting them could permit a duplicate write. SQLite files must be protected with filesystem access controls and disk encryption where application data requires it. Stored mutation arguments are normalized business data, not merely hashes.

## Back up and restore local state

Do not copy a live SQLite database with `cp`: committed pages may still be in its WAL. The operations command uses SQLite's online backup API, checks every snapshot, writes a content-digest manifest without host paths, and publishes the directory atomically.

```bash
python -m web_operation_agent.composition.operations backup \
  --sessions private/sessions.sqlite3 \
  --mutations private/mutations.sqlite3 \
  --destination backups/2026-09-14
python -m web_operation_agent.composition.operations verify backups/2026-09-14
```

Restore always targets a new directory. Stop and isolate all writers first, then make that fact explicit:

```bash
python -m web_operation_agent.composition.operations restore backups/2026-09-14 \
  --destination recovery/2026-09-14 \
  --writer-isolated
```

The command refuses an existing destination, a modified or undeclared file, a symlink, a failed SQLite integrity check, or a restore without writer isolation. The result reports unresolved `dispatching` and `indeterminate` mutations. Keep the recovered deployment write-disabled until those rows and any dispatches newer than the backup have been reconciled against the application.

No browser capability, runtime lease, approval, delegated credential, or model continuation is in the bundle. A recovered host authenticates the actor again and issues a new capability. Restored progress is history; it cannot authorize or replay an operation. Back up the application database, attachment storage, secrets, and configuration through their own supported mechanisms in the same recovery plan; this command covers only the framework's two SQLite files.

## Inspect and resolve uncertain writes

```bash
python -m web_operation_agent.business.operations --journal private/mutations.sqlite3 list
python -m web_operation_agent.business.operations --journal private/mutations.sqlite3 resolve PROPOSAL_DIGEST --expected-updated-at TIMESTAMP --outcome completed --operator OPERATOR_ID --evidence verified-evidence.json --writer-stopped
```

The second command is a dry run. Add `--apply` only after verifying the old writer and upstream request can no longer execute and obtaining definitive evidence of completion or definite failure. A matching current record alone cannot prove that this proposal created it; an absent record cannot rule out an in-flight write. The expected timestamp prevents a stale resolution from overwriting newer journal state. The resolution and its operator/evidence digests commit atomically with the terminal state. No operation retries the original write or reissues approval.

`mutation_ops.reconcile(journal, proposal_digest, application=bound_adapter, principal=fresh_principal)` performs an application-owned read under the original actor and does not change the journal. Twenty reports current matching records and scan completeness conservatively as an unknown outcome requiring human review. The CLI intentionally does not discover credentials or load arbitrary adapters.
