# Web Operation Agent

[简体中文](README.zh-CN.md)

An alpha framework for embedding an AI assistant in a first-party web application. The server owns planning, model access, credentials, approvals and audit; the browser observes the page, runs fixed tools and displays progress.

[Yudao](examples/yudao/deploy/README.md) is the default demo and test application. The [Odoo 18 integration](examples/odoo/README.md) remains an unmaintained reference. Interfaces may change; the project is not production-ready.

## Capabilities and architecture

- Application-owned Skills, typed reads and optional source retrieval.
- Goal-directed queries/navigation and reviewed form prefill with user handover.
- Writes default off; enabled writes require exact approval, dispatch once and application verification. Uncertain writes are never retried automatically.
- A React panel over server state, multi-page sessions and optional SQLite persistence.

```text
First-party page + browser Runtime
              ↕ authenticated observations/actions
Application-neutral server + session policy/model/approval/audit
              ↕ typed application ports
Application adapter + identity/routes/reads/writes/verification
```

Applications provide their own trusted identity, permissions and verification. Browser actions cannot supply database transactions or rollback. Implemented paths and evaluation samples do not establish general accuracy. See [runtime behavior and limits](docs/reference/runtime-behavior.md), [system design](docs/concepts/overview.md) and [security](SECURITY.md).

## Run Yudao

### Local environments

| Local environment | Command | URL |
| --- | --- | --- |
| Environment 1 (default) | `./local` | <http://localhost:40001> |
| Environment 2 | `./local --env 2` | <http://localhost:40002> |

Each Compose project contains its assistant, application, database and Redis. Source is mounted from the directory containing `local`; ordinary source updates restart only the selected assistant. Setup and diagnostics: [local guide](docs/guides/local-yudao.md).

### Online environment (demo)

Deploy to a configured server with `./deploy`; setup and recovery: [online deployment guide](docs/guides/deploy-yudao.md).

## Development

Use Python 3.13 and Node.js:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
python -m pip install --no-deps --editable examples/yudao
cp .env.example .env  # fill only needed settings; never commit .env
python scripts/build_runtime.py --install
python scripts/build_panel.py
python scripts/check.py --plan
python scripts/check.py
```

Rebuild browser assets after frontend changes and in each checkout. Model configuration and container updates follow the local/deployment guides. Checks, merge validation and explicit full qualification: [development guide](docs/guides/development-checks.md).

Source: `src/` (neutral core), `frontend/` (browser code), `examples/` (adapters/deployment), `testapps/` (synthetic test apps), `evals/` (separate evaluation apparatus).

[Documentation index](docs/README.md) · [Integrate an application](docs/guides/integrate-an-application.md) · [Write model](docs/concepts/writes.md) · [Durable operation](docs/guides/operate-durably.md)

## License

[LICENSE](LICENSE) · [THIRD_PARTY.md](THIRD_PARTY.md)
