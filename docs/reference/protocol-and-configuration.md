---
title: Configuration & Protocol
description: pagehelm.toml and .env validation rules, mode expansion, HTTP/SSE endpoints and JSON payloads, panel_contract.json v2, presentation.json, and system hard bounds.
---

This reference covers PageHelm configuration rules, the HTTP/SSE transport protocol, `panel_contract.json` (`protocol: 2`), `presentation.json` UI constants, and system hard limits.

## 1. Configuration (`pagehelm.toml` and `.env`)

`resolve_pagehelm_environment()` keeps runtime settings separate from secrets. `parse_pagehelm_toml()` validates TOML via Python's stdlib `tomllib`, rejecting unknown keys, booleans masquerading as numbers, non-HTTPS remote URLs, and credential-like key names (`*api_key*`, `*secret*`, `*token*`, `*password*`).

### `pagehelm.toml` Fields

| Field | Type & constraint | Default & behavior |
| --- | --- | --- |
| `provider` | `"deepseek"` \| `"qwen"` \| `"openai"` | Expands official `base_url`, priced default `model`, and `.env` key lookup order |
| `mode` | `"read-only"` \| `"assisted-draft"` \| `"verified-operate"` | `"verified-operate"` for `./local`; `"read-only"` for hosted deploys |
| `model` | Non-empty string (`<= 120` chars), must exist in `policies/prices.json` | Derived from `provider` |
| `base_url` | `https://...` (or loopback `http://localhost` / `127.0.0.1`), no userinfo or query | Derived from `provider` |
| `daily_allowance_cny` | Finite float `> 0` and `<= 1,000,000` | Per-actor daily CNY cap (`5.0` hosted, `20.0` local) |
| `daily_total_cap_cny` | Finite float `> 0` and `<= 1,000,000` | Process-wide daily atomic reserve cap (unset = disabled) |
| `monthly_total_cap_cny` | Finite float `> 0` and `<= 1,000,000` | Process-wide monthly atomic reserve cap (unset = disabled) |
| `max_sessions` | Integer `1..100,000` | Default `300` active sessions per host |
| `turn_timeout_s` | Integer `1..3,600` | Wall-clock turn budget in seconds (default `120`, excluding approval wait) |

### Provider Defaults

| `provider` | Default `base_url` | Default `model` | `.env` key lookup order |
| --- | --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | `deepseek-flash` | `DEEPSEEK_API_KEY`, `AGENT_OPENAI_API_KEY` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.8-flash` | `DASHSCOPE_API_KEY`, `QWEN_API_KEY`, `AGENT_OPENAI_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `gpt-6.1-sol` | `OPENAI_API_KEY`, `AGENT_OPENAI_API_KEY` |

### `.env` Variables

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` / `QWEN_API_KEY`, `OPENAI_API_KEY` | Matched automatically according to `provider` in `pagehelm.toml` |
| `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, `AGENT_PROVIDER` | Explicit overrides or legacy configuration without `pagehelm.toml` |
| `WOA_MODE`, `WOA_NATIVE_WRITES` | Environment overrides (startup fails if conflicting with `pagehelm.toml`) |

### Safety Mode Expansion (`expand_operation_mode`)

| `mode` | `native_page_admission` | `native_writes` | `native_submit` | `native_write_preparation` | `native_edit_prefill` |
| --- | --- | --- | --- | --- | --- |
| `read-only` | `read-only-window` | `false` | `false` | `false` | `false` |
| `assisted-draft` | `read-only-window` | `false` | `false` | `true` | `true` |
| `verified-operate` | `read-only-window` | `true` | `true` | `false` | `true` |

## 2. Gateway HTTP and SSE Endpoints

After exchanging a host session at `/bootstrap`, browser traffic goes through the gateway prefix (default `/woa-agent`):

| Endpoint | Method | Auth & headers | Purpose |
| --- | --- | --- | --- |
| `/healthz`, `/readyz` | `GET` | None | Liveness and drain-aware readiness (`503` while draining) |
| `/v1/runtime/assistant.js` | `GET` | Same-origin static asset | Embedded IIFE bundle with `ETag`, `304`, and `.gz` (`404 runtime_unavailable` if unbuilt) |
| `/v1/sessions/<id>/hello` | `POST` | `Authorization: Bearer`, `Origin` | Validates `ClientHello` origin and principal digest; issues unique active `lease_id` |
| `/v1/sessions/<id>/observations` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Uploads redacted `ObservationEnvelope` and `PageObservation` |
| `/v1/sessions/<id>/messages` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Submits an idempotent user task (`429` if busy) |
| `/v1/sessions/<id>/claims`, `results` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Claims pending browser actions and returns `ActionResultEnvelope` |
| `/v1/sessions/<id>/approvals` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Submits single-use `ApprovalDecision` (`approved`, `denied`, `prefill`) |
| `/v1/sessions/<id>/cancellations` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Cancels an active turn or records a pre-cancellation race |
| `/v1/sessions/<id>/reconciliations` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Runs same-actor read-only reconciliation for `INDETERMINATE` writes |
| `/v1/sessions/<id>/conversation` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Accepts `{"action": "new"}` to clear conversation memory without deleting ledgers |
| `/v1/sessions/<id>/events`, `timeline` | `GET` | Bearer, `Origin`, `X-WOA-Lease` | Unidirectional SSE stream (`Last-Event-ID`) and v2 timeline projection |

All `POST` requests require `Content-Type: application/json`, a valid `Content-Length` (`<= 1 MiB`), and typed validation (`SCHEMA_VERSIONS = 1`). Payloads containing unredacted handoff parameters or credentials fail with HTTP `400` (`code: "privacy_boundary"`).

## 3. Wire Protocol JSON Examples

### `POST /v1/sessions/<id>/hello` (`ClientHello`)

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "client_id": "tab_7d92b41f",
  "runtime_instance_id": "rt_9f18c30a",
  "runtime": "woa-embedded-runtime",
  "capabilities": ["observe", "click", "fill", "select", "scroll", "handoff_form"],
  "page_origin": "https://admin.example.com",
  "application_id": "yudao-admin",
  "principal_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "last_event_id": "e00000012"
}
```

### `POST /v1/sessions/<id>/observations` (`ObservationEnvelope`)

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "observation_id": "obs_0004",
  "page_epoch": 4,
  "caused_by_action_id": "act_0003",
  "events": [],
  "observation": {
    "schema_version": 1,
    "epoch": 4,
    "url": "https://admin.example.com/crm/clue",
    "title": "CRM Clue Management",
    "landmarks": ["main", "dialog"],
    "elements": [
      {
        "ref": "e1",
        "role": "textbox",
        "name": "Lead Name",
        "tag": "input",
        "epoch": 4,
        "attrs": {"type": "text"}
      }
    ],
    "selected_controls": ["Active"],
    "render_state": "ready",
    "truncated": false
  }
}
```

### `POST /v1/sessions/<id>/results` (`ActionResultEnvelope`)

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "turn_id": "turn_0002",
  "action_id": "act_0003",
  "based_on_observation_id": "obs_0003",
  "page_epoch": 3,
  "command_digest": "9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab",
  "status": "completed",
  "observation_after": { "...": "Full ObservationEnvelope at page_epoch 4" },
  "receipt": { "...": "ActionReceipt (applied_fields, rejected_fields, previous_fields, restricted_fields)" },
  "error_code": "",
  "error_detail": ""
}
```

### `POST /v1/sessions/<id>/approvals` (`ApprovalDecision`)

```json
{
  "schema_version": 1,
  "session_id": "sess_01HZX8R9A2B3C4",
  "approval_id": "apr_92c17f08",
  "decision": "approved",
  "principal_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "turn_id": "turn_0002",
  "operation_id": "op_update_clue_81"
}
```

Valid `decision` values: `"approved"`, `"denied"`, or `"prefill"`.

## 4. `panel_contract.json` and `presentation.json`

The server and Shadow DOM panel share `src/web_operation_agent/web_runtime/panel_contract.json` (`protocol: 2`) to validate all projections:

- **Default copy keys (`label_keys`):** `product: "PageHelm"`, `emptyIntro: "你好，我是 PageHelm"`; DOM classes and CSS prefixes stay `woa-*`.
- **State enums:** Non-terminal: `waiting_for_runtime`, `session_recovered`, `message_accepted`, `new_conversation`, `agent_started`, `step`, `approval_recorded`, `cancelling`. Terminal: `needs_input`, `finished`, `refused`, `cancelled`, `indeterminate`, `failed`, `task_failed`. Unregistered states are rejected.
- **Lapse codes & terminal mappings:**
  - `lapse_codes`: `principal_expired`, `session_expired`, `browser_inactive`, `host_stopped`, `server_stopped`, `bootstrap_replaced`, `native_bootstrap_replaced`, `unknown_session`, `not_found`.
  - `terminal_panel_state_by_code`: `session_closed` / `unknown_session` / `not_found` / `principal_expired` / `session_expired` -> `"ended"`; `runtime_replaced` -> `"replaced"`; `unauthorized` / `invalid_lease` / `principal_mismatch` / `origin_refused` -> `"error"`.
- **v2 structured payloads:** `table` (`StructuredTable` -> `<DataTable>`), `input_options` (2–4 `ClarificationOption` buttons), `handoff` (redacted applied/remaining/restricted fields and `can_undo`), plus local-dev `debug_trace` (`first_route_kind`, `stop_reason`, `stage_timings`, `tool_timings`, `checks`).

Frontend interaction and geometry constants live in `frontend/src/presentation.json`:

| Constant | Value | Purpose |
| --- | --- | --- |
| `overlay_z_index` | `2147483646` | Keeps the Shadow DOM panel above host drawers and modals |
| `launcher_inset` | `20` | Floating launcher edge inset (px) |
| `launcher_size` | `48` | Floating launcher width and height (px) |
| `approval_visibility.min_visible_ratio` | `0.9` | Minimum visible ratio within clipping containers before approval clicks count |
| `approval_visibility.min_visible_ms` | `400` | Minimum continuous visible dwell (ms) before approval clicks count |
| `start.final_statuses` | `[401, 403]` | Terminal HTTP status codes indicating host session expiry |

## 5. System Hard Limits

| Boundary | Hard limit | Source module |
| --- | --- | --- |
| User message length | `2000` chars | `panel_contract.json` / gateway validation |
| POST request body (`MAX_BODY_BYTES`) | `1 MiB` (`1,048,576` bytes) | `transport/gateway.py` |
| Event frame size (`_MAX_EVENT_BYTES`) | `256 KiB` | `transport/protocol.py` |
| DOM observation elements | `200` (`max_elements`) | `browser_contract.json` |
| Per-turn `TurnTables` SQLite | `36` tables, `5000` rows/table, `128` cols, `1.5s` query timeout | `execution/turn_tables.py` |
| `<DataTable>` export | `24` cols, `2000` rows, `300` chars/cell | `execution/turn_tables.py` / `panel_view.py` |
| Answer Protocol bounds | `50` items, `2` repair passes, `50` displayed records | `answer/answer_protocol.py` |
| Turn model step cap | `24` (core default) / `33` (`YudaoTurnExecutor`) | `panel_contract.json` / `page_prompts.py` |
