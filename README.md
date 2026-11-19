# PageHelm

[简体中文](README.zh-CN.md)

**PageHelm** (`web_operation_agent`) is an alpha framework for embedding a server-governed AI operations assistant directly inside first-party web applications.

- **Server-owned control:** Model keys, Skill manuals, session policy, safety gates, and audit logs remain strictly on the server.
- **Same-actor hybrid execution:** Combines server-side delegated API + SQL reads (`TurnTables`, `<DataTable>`) with live browser DOM navigation and prefill (`assistant.js`, `PageController`).
- **Fresh visible write approvals:** Mutations default **off** (`read-only`); enabled writes require exact diffs, trusted visible clicks, single-consume dispatch, same-actor readback, and **never** retry `INDETERMINATE` outcomes.
- **Incremental onboarding (L0–L3):** Start with the single-script panel and shared `frontend/src/integrations/element-plus/` helpers, then layer typed reads, domain playbooks, and verified writes.

## Architecture

```text
First-party Page (assistant.js + Shadow DOM Panel + PageController)
                        ^ authenticated observations / actions / approvals
                        v
Application-Neutral Server (Executor · TurnHarness · WriteSafetyPipeline · SQLite Store)
                        ^ typed ports (ApplicationPorts & GoalData)
                        v
Application Adapter (Identity · Routes · Same-Actor Reads · Verified Writes)
```

## 5-Minute Quickstart

Requires Python 3.13, Node.js (>=22), and Docker Compose 2.30+. Prepare reference fixtures in `.q0-cache/yudao/` per [deploy reference](examples/yudao/deploy/README.md).

```bash
cp .env.example .env                    # Fill 1-3 lines with your API key only (e.g. DEEPSEEK_API_KEY)
cp pagehelm.example.toml pagehelm.toml  # Pick provider ("deepseek"|"qwen"|"openai") & safety mode
./local --synthetic-clock               # Launch local sandbox env 1 at http://localhost:40001
```

## Progressive Integration (L0–L3)

| Level | Integration work | Unlocked capability | Reference |
| --- | --- | --- | --- |
| **L0** | Embed `/v1/runtime/assistant.js` + `/bootstrap` | Shadow DOM panel, semantic DOM actions, Element Plus helpers | `frontend/src/integrations/element-plus/` |
| **L1** | Implement `Identity`, `Routes`, and typed read ports | Same-actor queries, `TurnTables` SQL, paginated `<DataTable>` | [`testapps/contacts`](testapps/contacts/adapter.py) |
| **L2** | Add domain playbooks, conventions & `entity_rules` | Module disambiguation, compiled rule checks, `need_input` pills | [L0–L3 Guide](docs/integration/levels.md) |
| **L3** | Add journal, postcondition verifier & Skills / `SubmitPort` | Typed Skill writes, flat `update_record`, native submit & undo prefill | [Verified Writes](docs/security/verified-writes.md) |

## Safety Matrix & Approach Comparison

| Mode | Reads & nav | Form prefill | Write proposals | Mutation dispatch |
| --- | --- | --- | --- | --- |
| `read-only` (prod default) | Read-only window | Off | Off | Off |
| `assisted-draft` | Read-only window | On (handoff + undo) | Prepare only | Off |
| `verified-operate` (local default) | Read-only window | On | On | Once after fresh approval |

| Dimension | Pure API / RAG Bot | Headless Browser Agent / RPA | Low-Code Workflow Builder | **PageHelm** |
| --- | --- | --- | --- | --- |
| Live UI navigation & form prefill | None | Slow full-page loops | None | Same-origin DOM Runtime + handover |
| Tabular reads (thousands of rows) | Needs custom endpoints | Slow UI pagination | Memory/node bottlenecks | Same-actor API + `TurnTables` SQL |
| Auth & permission inheritance | Often service keys | Server holds user cookies | Static shared keys | Delegated per-session browser identity |
| Write safety & retry control | Custom per tool | Clicks live save buttons | Auto-retry risks duplicates | 8-stage approval + `INDETERMINATE` lock |

## Development

```bash
python3.13 -m venv .venv && . .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
.venv/bin/python scripts/build_panel.py --install && .venv/bin/python scripts/build_runtime.py --skip-build
.venv/bin/python scripts/check.py --plan && .venv/bin/python scripts/check.py
```

## Documentation & Legal

- **Docs:** [English](docs/index.md) · [简体中文](docs/zh-cn/index.md) · [Architecture](docs/architecture/overview.md) · [Quickstart & Ops](docs/integration/quickstart.md) · [L0–L3 Integration](docs/integration/levels.md) · [Verified Writes](docs/security/verified-writes.md) · [Protocol Reference](docs/reference/protocol-and-configuration.md)
- **Docs site (`frontend/docs/`):** Built from `docs/` via Astro Starlight with `/llms.txt` & `/llms-full.txt`.
- **Notice:** Alpha software; evaluate only against isolated synthetic data. [SECURITY](SECURITY.md) · [LICENSE](LICENSE) · [THIRD_PARTY](THIRD_PARTY.md)
