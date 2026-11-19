---
title: 5-Min Quickstart & Ops
description: Minimal .env and pagehelm.toml setup, dual local sandboxes with ./local --synthetic-clock, production ./deploy, SQLite backups, and development checks.
---

> **TL;DR:** Put 1–3 lines of model API keys in `.env`, pick `provider` and `mode` in `pagehelm.toml`, and run `./local` (or `./local --synthetic-clock`) to launch the full-stack sandbox at `http://localhost:40001`. Production deployments run `read-only` by default via `./deploy`, back up SQLite stores consistently, and gate merges through `scripts/check.py`.

[简体中文](../zh-cn/integration/quickstart.md)

## 1. Prerequisites and local environment

Install **Python 3.13**, **Node.js (>=22)**, **npm**, and **Docker Compose 2.30+**, then bootstrap your virtual environment and build the panel + browser Runtime assets:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable ".[browser]"
python -m pip install --no-deps --editable examples/yudao
python scripts/build_panel.py --install
python scripts/build_runtime.py --skip-build
```

---

## 2. Minimal configuration (`.env` + `pagehelm.toml`)

Credentials and behavioral configuration live in separate gitignored files:

```bash
cp .env.example .env
cp pagehelm.example.toml pagehelm.toml
```

### `.env` (1–3 lines)

Add only the API key matching your chosen `provider`:

```dotenv
# Pick the provider key you use:
DEEPSEEK_API_KEY=your-key-here
# DASHSCOPE_API_KEY=your-key-here
# OPENAI_API_KEY=your-key-here
```

### `pagehelm.toml`

`parse_pagehelm_toml()` validates keys strictly via Python `tomllib`, rejects any secret-like field names (`*api_key*`, `*secret*`, `*token*`, `*password*`) inside TOML, and expands `provider` and `mode` cleanly:

```toml
provider = "deepseek"     # "deepseek" | "qwen" | "openai"
mode = "verified-operate" # "read-only" (prod default) | "assisted-draft" | "verified-operate"
# daily_allowance_cny = 20.0
# turn_timeout_s = 120
```

Supported provider defaults (when `base_url` / `model` are not explicitly overridden):

| `provider` | Official `base_url` | Default `model` | Key lookup order in `.env` |
| --- | --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | `deepseek-flash` | `DEEPSEEK_API_KEY`, `AGENT_OPENAI_API_KEY` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.8-flash` | `DASHSCOPE_API_KEY`, `QWEN_API_KEY`, `AGENT_OPENAI_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `gpt-6.1-sol` | `OPENAI_API_KEY`, `AGENT_OPENAI_API_KEY` |

> **Compatibility & pricing guard:** Legacy `.env` variables (`AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, `AGENT_PROVIDER`, `WOA_NATIVE_WRITES`) remain supported when `pagehelm.toml` is absent. Every model used by the server must have an entry in `src/web_operation_agent/policies/prices.json`.

---

## 3. Running local sandboxes (`./local`)

Before running `./local` for the first time, follow `examples/yudao/deploy/README.md` to prepare the pinned cache artifacts in `.q0-cache/yudao/` (backend JAR, admin UI `ui/dist-prod/`, database schema DDL, and pinned `PageController`).

Two completely isolated Docker Compose instances are supported side by side:

| Instance | Command | URL | Compose project |
| --- | --- | --- | --- |
| Environment 1 (default) | `./local` | `http://localhost:40001` | `woa-yudao-env1` |
| Environment 2 | `./local --env 2` | `http://localhost:40002` | `woa-yudao-env2` |

Common `./local` subcommands:

```bash
./local check             # Read-only preflight: Docker, cache versions, pagehelm.toml, pricing & policy
./local                   # Start or refresh env 1 (defaults to mode = "verified-operate" locally)
./local --synthetic-clock # Align backend app & MySQL clock to 2026-10-25T09:00:00+08:00
./local status            # Verify business API health, /readyz, and live assistant.js SHA-256
./local logs              # Tail assistant container logs (`./local logs app` for backend/MySQL/Redis)
./local stop              # Stop assistant container while keeping database & state volumes intact
```

Instance state lives outside Git in `$XDG_STATE_HOME/web-operation-agent/yudao/env{1,2}/` (default `~/.local/state/web-operation-agent/yudao/`). Editing Python code restarts only the assistant container; editing frontend sources rebuilds `assistant.js` without resetting DB volumes.

### Synthetic dataset utilities

Under `examples/yudao/deploy/` (run explicitly with your local URL, compressed MySQL dump, and `--apply`, reading accounts from gitignored `.private/yudao-accounts.json`):

- `demo_data.py` and `seed_scale.py` — populate synthetic multi-module data, high-row pagination tables, and read-only explorer user `demoexplorer`;
- `verify_demo_data.py` and `verify_demo_browser.py` — verify ledger balances, inventory totals, and real browser pagination fidelity.

---

## 4. Production deployment & persistence (`./deploy`)

Production deployments default to **`read-only`** mode (writes, edit prefill, and user-facing source search off), protected by HTTP method guards on static assets and proxy endpoints:

```bash
cp deploy.example.toml deploy.local.toml   # Set ssh_target & public_origin (gitignored)
./deploy check                             # Read-only check of local build artifacts & SSH host
./deploy                                   # Build assets + wheels, upload, probe health, switch current
./deploy status                            # Inspect remote release.json & compare assistant.js SHA-256
```

- **Graceful shutdown (`SIGTERM` drain):** Host stops accepting new turns, waits for in-flight write dispatch/readback completion up to `shutdown_drain_timeout_s`, and exits cleanly while `/readyz` returns `503`.
- **Consistent SQLite backups & reconciliation:** Never copy active `-wal` SQLite files with plain `cp`. See [Operations & Observability](../security/operations-and-observability.md) for `composition.operations backup|verify|restore --writer-isolated` and `business.operations list|resolve --writer-stopped`.

---

## 5. Development checks

Always execute checks in `.venv` (Python 3.13). Automated test suites must never invoke paid models or modify the real model spend ledger:

```bash
.venv/bin/python scripts/check.py --plan              # Preview checks selected from your git diff
.venv/bin/python scripts/check.py                     # Run the default dev profile
.venv/bin/python scripts/check.py --profile ci --base main   # Full pre-merge CI lane
bash scripts/check_wheels.sh                          # Required after packaging/dependency changes
scripts/qualify.sh --require-browser                  # Full release-candidate qualification
```

After changing frontend source files or switching worktrees, rebuild runtime assets with:

```bash
.venv/bin/python scripts/build_panel.py && .venv/bin/python scripts/build_runtime.py --skip-build
```
