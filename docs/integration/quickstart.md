---
title: 5-Minute Quickstart & Operations
description: Configure .env + pagehelm.toml, run local dual Yudao environments with ./local --synthetic-clock, deploy online with ./deploy, and run repository checks.
---

> **TL;DR:** Put only your provider API key in `.env`, select `provider` and safety `mode` in `pagehelm.toml`, and launch an isolated local sandbox with `./local` (or `./local --synthetic-clock`). Production deployments use `./deploy`, online SQLite backup/restore commands, and explicit `check.py` gates.

[简体中文](../zh-cn/integration/quickstart.md)

## 1. Local development environment setup

Requires **Python 3.13**, **Node.js (>=22)**, **npm**, and **Docker Compose 2.30+**.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
python scripts/build_panel.py --install
python scripts/build_runtime.py --skip-build
```

## 2. Minimal configuration (`.env` + `pagehelm.toml`)

Keep secrets in `.env` (gitignored) and non-secret behavior in `pagehelm.toml` (gitignored):

```bash
cp .env.example .env
cp pagehelm.example.toml pagehelm.toml
```

In `.env`, fill **1–3 lines** with your API key only:

```dotenv
DEEPSEEK_API_KEY=your-key-here
```

In `pagehelm.toml`, choose a built-in `provider` (`deepseek`, `qwen`, `openai`—each derives its official base URL and default priced model) and safety `mode` (`read-only`, `assisted-draft`, `verified-operate`):

```toml
provider = "deepseek"
mode = "verified-operate"
```

> Legacy `.env` files defining `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, and `WOA_NATIVE_WRITES` continue to work automatically when `pagehelm.toml` is omitted. Every configured model must have a registered entry in `src/web_operation_agent/policies/prices.json`.

## 3. Run the local reference sandbox (`./local`)

Before running `./local` for the first time, prepare `.q0-cache/yudao/` (patched backend JAR, admin UI `dist-prod/`, schema SQL, and pinned PageController) following [examples/yudao/deploy/README.md](../../examples/yudao/deploy/README.md).

Two isolated local Compose environments share the checkout:

| Environment | Command | Browser URL | Compose project |
| --- | --- | --- | --- |
| Env 1 (default) | `./local` | `http://localhost:40001` | `woa-yudao-env1` |
| Env 2 | `./local --env 2` | `http://localhost:40002` | `woa-yudao-env2` |

### Common `./local` commands

```bash
./local check                  # Read-only preflight: Docker, cache, pagehelm.toml, pricing, policies
./local                        # Start or update env 1 (reads pagehelm.toml + .env; defaults to verified-operate)
./local --synthetic-clock      # Align MySQL + Java wall clock to synthetic seed anchor (2026-10-25T09:00:00+08:00)
./local status                 # Verify backend health, assistant readiness, and active assistant.js SHA-256
./local logs                   # Tail assistant container logs (use `logs app` for MySQL/Redis/Java/nginx)
./local stop                   # Stop assistant container while preserving database & state volumes
```

Instance state lives outside the repository under `$XDG_STATE_HOME/web-operation-agent/yudao/env{1,2}/` (defaulting to `~/.local/state/web-operation-agent/yudao/`). Model edits rebuild only the selected assistant container; Java/MySQL volumes stay intact unless recreated by an explicit clock change.

### Synthetic data utilities (opt-in)

Located in `examples/yudao/deploy/` (not auto-executed by `./local`). Both write tools require a loopback URL, an explicit compressed MySQL backup, and `--apply`, reading credentials from gitignored `.private/yudao-accounts.json`:

- `demo_data.py` & `seed_scale.py` — populate synthetic scale records and `demoexplorer` accounts;
- `verify_demo_data.py` & `verify_demo_browser.py` — audit balances, totals, and paginated tables as `demoexplorer`.

## 4. Production deployment & durable operations (`./deploy`)

Production cloud hosts default to **`read-only`** (writes, edit prefill, and user-facing source lookup disabled) behind nginx and an explicit read-only HTTP method fence.

```bash
cp deploy.example.toml deploy.local.toml   # Set ssh_target & public_origin (gitignored)
./deploy check                             # Read-only local + SSH remote verification
./deploy                                   # Build wheels & assistant.js, upload release, health-check & switch
./deploy status                            # Compare remote release.json & served assistant.js SHA-256
```

- **Graceful shutdown (`SIGTERM`):** New turns stop immediately while in-flight write dispatches + readbacks drain up to `shutdown_drain_timeout_s`.
- **SQLite backup & isolated restore:** Use online SQLite backups (`python -m web_operation_agent.composition.operations backup|verify|restore`) rather than raw file `cp` over active `-wal` files. Restores require an isolated `--writer-isolated` directory and report any unresolved `dispatching` / `indeterminate` mutations before re-enabling writes.
- **Reconciling `INDETERMINATE` writes:** Inspect pending records via `python -m web_operation_agent.business.operations --journal <path> list` and resolve with `--apply --writer-stopped` only after verifying actual application state, or trigger same-actor read-only reconciliation via `POST /v1/sessions/<id>/reconciliations`.

## 5. Development checks

Use Python 3.13 from `.venv` and select checks from your actual diff:

```bash
.venv/bin/python scripts/check.py --plan              # Preview checks selected for local edits
.venv/bin/python scripts/check.py                     # Run dev profile checks
.venv/bin/python scripts/check.py --profile ci --base main   # Full CI profile before merge
bash scripts/check_wheels.sh                          # Required for packaging/dependency changes
scripts/qualify.sh --require-browser                  # Full release qualification only
```

Whenever frontend or runtime files change, rebuild browser bundles (`scripts/build_panel.py` and `scripts/build_runtime.py --skip-build`). Tests never call paid model APIs or alter the evaluation spend ledger.
