---
title: Protocol & Configuration Reference
description: Complete schema for pagehelm.toml and .env, Yudao host CLI flags, gateway HTTP/SSE endpoints, panel_contract.json v2, and runtime limits.
---

> **TL;DR:** Reference manual for all public configuration surfaces in PageHelm: strict keys and bounds in `pagehelm.toml`, `.env` credential keys, the three expanded `mode` switches, gateway REST/SSE endpoints under `/v1/sessions/<id>/`, `panel_contract.json` v2 states/payloads, and documented per-turn/session limits.

[简体中文](../zh-cn/reference/protocol-and-configuration.md)

## `pagehelm.toml` schema (`resolve_pagehelm_environment`)

`parse_pagehelm_toml()` validates strictly using Python's `tomllib`, rejecting unknown keys, wrong types (such as booleans for integer counts or non-HTTPS remote `base_url`), and any credential-like key (`*api_key*`, `*secret*`, `*token*`, `*password*`):

| Key | Type & constraints | Default / built-in behavior |
| --- | --- | --- |
| `provider` | `"deepseek"` \| `"qwen"` \| `"openai"` | Derives official `base_url`, default priced `model`, and `.env` key lookup order |
| `mode` | `"read-only"` \| `"assisted-draft"` \| `"verified-operate"` | Local `./local` defaults `"verified-operate"`; production hosts default `"read-only"` |
| `model` | Non-empty string (<= 120 chars), registered in `policies/prices.json` | Defaults by `provider`: `deepseek-flash`, `qwen3.8-flash`, or `gpt-6.1-sol` |
| `base_url` | `https://...` (or `http://localhost|127.0.0.1` loopback), no userinfo/query | Defaults by `provider` (`api.deepseek.com`, `dashscope.aliyuncs.com/...`, `api.openai.com/v1`) |
| `daily_allowance_cny` | Finite float `> 0` and `<= 1,000,000` | Per-actor daily CNY cap (`5.0` cloud host default, `20.0` `./local` default) |
| `daily_total_cap_cny` | Finite float `> 0` and `<= 1,000,000` | Process-wide daily atomic reserve cap (disabled when unset) |
| `monthly_total_cap_cny` | Finite float `> 0` and `<= 1,000,000` | Process-wide monthly atomic reserve cap (disabled when unset) |
| `max_sessions` | Int `1 .. 100,000` | Default `300` live sessions per host |
| `turn_timeout_s` | Int `1 .. 3,600` | Default `120` seconds total execution wall budget per turn (excluding user approval wait) |

### `.env` keys

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` / `QWEN_API_KEY`, `OPENAI_API_KEY` | Provider API keys matched by `provider` (1–3 lines; never logged or mounted into browser) |
| `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, `AGENT_PROVIDER` | Explicit overrides or legacy standalone configuration (compatible fallback when `pagehelm.toml` is absent) |
| `WOA_MODE` / `WOA_NATIVE_WRITES` | Environment overrides (`read-only` / `assisted-draft` / `verified-operate`, or `true` / `false`; conflicting values raise an error) |

### Mode expansion mapping

| Mode | `native_page_admission` | `native_writes` | `native_submit` | `native_write_preparation` | `native_edit_prefill` |
| --- | --- | --- | --- | --- | --- |
| `read-only` | `read-only-window` | `false` | `false` | `false` | `false` |
| `assisted-draft` | `read-only-window` | `false` | `false` | `true` | `true` |
| `verified-operate` | `read-only-window` | `true` | `true` | `false` | `true` |

## Gateway HTTP & SSE endpoints (`EmbeddedGateway`)

All browser-to-server traffic runs under the configured gateway base URL (default `/woa-agent`) after identity bootstrap at `/bootstrap`:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/healthz`, `/readyz` | `GET` | Liveness & drain-aware readiness probes (`503` while SIGTERM draining) |
| `/v1/runtime/assistant.js` | `GET` | Versioned IIFE Runtime + React panel bundle (`ETag` / `304` / `.gz`, `404 runtime_unavailable` if unbuilt) |
| `/v1/sessions/<id>/hello` | `POST` | Validates `ClientHello` origin & principal binding, issues `lease_id` & panel config |
| `/v1/sessions/<id>/observations` | `POST` | Uploads semantic `PageObservation` (`max 200` controls, active dialog priority, scroll headroom) |
| `/v1/sessions/<id>/messages` | `POST` | Submits idempotent user task (`message_id` dedup; `429` backpressure when busy) |
| `/v1/sessions/<id>/claims`, `results` | `POST` | Atomic browser action claim & `ActionReceipt` delivery (with `settled_action_observation`) |
| `/v1/sessions/<id>/approvals` | `POST` | Delivers fresh `ApprovalDecision` (`approve`, `deny`, or `prefill` when supported) |
| `/v1/sessions/<id>/cancellations` | `POST` | Cancels active or pre-arriving task cleanly (race-safe pre-cancellation buffer) |
| `/v1/sessions/<id>/reconciliations` | `POST` | Triggers read-only reconciliation for `INDETERMINATE` writes under the same actor |
| `/v1/sessions/<id>/events`, `timeline` | `GET` | `Last-Event-ID` SSE stream & timeline JSON projection (step progress, `<DataTable>`, pills, handoff) |

## `panel_contract.json` (Protocol v2) & Limits

Server and frontend validate every limit and state against `src/web_operation_agent/web_runtime/panel_contract.json`:

- **Brand defaults (`label_keys`):** `product: "PageHelm"`, `emptyIntro: "你好，我是 PageHelm"`. DOM classes/attributes retain `woa-*` for selector stability.
- **Progress & panel states:** Non-terminal (`waiting_for_runtime`, `session_recovered`, `message_accepted`, `new_conversation`, `agent_started`, `step`, `approval_recorded`, `cancelling`) and terminal (`needs_input`, `finished`, `refused`, `cancelled`, `indeterminate`, `failed`, `task_failed`). Any unmapped state fails validation strictly.
- **Structured v2 payloads:** `table` (`StructuredTable` with columns, redacted rows, `total_rows`), `input_options` (2–4 clickable `ClarificationOption` pills on `needs_input`), `handoff` (`completed_fields` with secrets omitted, `pending_fields`, `restricted_controls`, `can_undo`), and `debug_trace` (local dev inspection only).
- **Hard bounds:** `composer_max_chars = 2000`, `history_page_size = 100`, `result_max_chars = 20000`, max `200` interactive elements per page snapshot, max `5,000` tabular rows per API read projection (`200` SQL output rows), and `model_steps = 33` on the table-read Yudao executor (`24` base).
