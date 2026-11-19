---
title: Local Demo Quickstart
description: Start the bundled examples/yudao synthetic environment locally and run development checks.
---

This guide starts PageHelm locally against the repository's bundled reference application (`examples/yudao`, a full back-office web app) so you can test same-actor reads, UI actions, and approval-gated writes.

> If you are integrating PageHelm into your own application, you do not need the `yudao` cache—start with the [L0–L3 Integration Guide](./levels.md). For online deployment, backups, and reconciliation, see [Deployment & Operations](../security/operations-and-observability.md).

## 1. Prerequisites and Installation

Install **Python 3.13**, **Node.js (>=22)**, **npm**, and **Docker Compose 2.30+**.

1. Prepare the reference application cache under `.q0-cache/yudao/` following `examples/yudao/deploy/README.md` (backend JAR, admin `ui/dist-prod/`, DDL, and pinned `PageController`).
2. Create the Python environment and build the panel and browser Runtime bundles:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
python scripts/build_panel.py --install
python scripts/build_runtime.py --skip-build
```

## 2. Configure Credentials and Mode

Secrets and non-secret runtime settings live in separate git-ignored files (`.env` and `pagehelm.toml`):

```bash
cp .env.example .env
cp pagehelm.example.toml pagehelm.toml
```

Add only the API key for your chosen provider in `.env`:

```dotenv
# Uncomment the key matching your provider
DEEPSEEK_API_KEY=your-key-here
# DASHSCOPE_API_KEY=your-key-here
# OPENAI_API_KEY=your-key-here
```

Set `provider` and `mode` in `pagehelm.toml` (secret keys are rejected in TOML; see [Configuration & Protocol](../reference/protocol-and-configuration.md) for all fields and provider defaults):

```toml
provider = "deepseek"     # "deepseek" | "qwen" | "openai"
mode = "verified-operate" # "read-only" | "assisted-draft" | "verified-operate"
# daily_allowance_cny = 20.0
# turn_timeout_s = 120
```

When `pagehelm.toml` is absent, legacy `AGENT_OPENAI_*`, `AGENT_PROVIDER`, and `WOA_NATIVE_WRITES` environment variables remain supported. Every model used must have a pricing entry in `src/web_operation_agent/policies/prices.json`.

## 3. Start the Local Reference Environment (`./local`)

Two isolated Docker Compose instances are available and can run side by side:

| Instance | Command | Browser URL | Compose project |
| --- | --- | --- | --- |
| Env 1 (default) | `./local` | `http://localhost:40001` | `woa-yudao-env1` |
| Env 2 | `./local --env 2` | `http://localhost:40002` | `woa-yudao-env2` |

Common commands:

```bash
./local check             # Read-only preflight: Docker, cache pins, pagehelm.toml, pricing, policies
./local                   # Start or update env 1 (defaults to verified-operate locally)
./local --synthetic-clock # Align app and MySQL clocks to synthetic baseline (2026-10-25T09:00:00+08:00)
./local status            # Verify API, /readyz, and live assistant.js SHA-256
./local logs              # Tail assistant logs (`./local logs app` for app, MySQL, and Redis)
./local stop              # Stop only the assistant container, keeping database and state volumes
```

Per-instance state is stored outside the repository under `$XDG_STATE_HOME/web-operation-agent/yudao/env{1,2}/` (default `~/.local/state/web-operation-agent/yudao/`). Python changes restart only the assistant container and frontend changes rebuild `assistant.js` without resetting database volumes.

## 4. Synthetic Data Utilities (Manual)

Scripts in `examples/yudao/deploy/` populate and verify demo datasets when run explicitly with the local base URL, MySQL backup path, and `--apply` (accounts are read from git-ignored `.private/yudao-accounts.json`):

- `demo_data.py`, `seed_scale.py`: Seed multi-module demo records, paginated list scale data, and the read-only `demoexplorer` account.
- `verify_demo_data.py`, `verify_demo_browser.py`: Verify stock balances, accounting totals, and browser pagination rendering.

For online rollout (`./deploy`), SQLite backups, and `INDETERMINATE` reconciliation, see [Deployment & Operations](../security/operations-and-observability.md).

## 5. Development Checks

Run repository checks inside `.venv` (Python 3.13). Automated tests do not call paid models or modify the spend ledger:

```bash
.venv/bin/python scripts/check.py --plan                     # Preview diff-selected checks
.venv/bin/python scripts/check.py                            # Run the dev check lane
.venv/bin/python scripts/check.py --profile ci --base main   # Pre-merge CI gate
bash scripts/check_wheels.sh                                 # Required for packaging or dependency changes
scripts/qualify.sh --require-browser                         # Full release-candidate qualification
```

After changing frontend source or switching worktrees, rebuild the panel and Runtime assets:

```bash
.venv/bin/python scripts/build_panel.py && .venv/bin/python scripts/build_runtime.py --skip-build
```
