---
title: Protocol & Configuration Reference
description: Strict pagehelm.toml and .env schemas, 3-mode expansion, /v1/sessions/* HTTP/SSE wire payloads, panel_contract.json v2, presentation.json, and hard limits.
---

> **TL;DR:** PageHelm validates `pagehelm.toml` strictly via `parse_pagehelm_toml()`, separates secrets into `.env`, communicates with first-party browsers over authenticated `/v1/sessions/<id>/*` HTTP + SSE routes, and synchronizes UI states across Python and JS through `panel_contract.json` (`protocol: 2`), `presentation.json`, and `browser_contract.json`.

[简体中文](../zh-cn/reference/protocol-and-configuration.md)

## 1. Configuration (`pagehelm.toml` & `.env`)

`resolve_pagehelm_environment()` combines clean behavioral settings in `pagehelm.toml` with credentials from `.env`. `parse_pagehelm_toml()` rejects unknown keys, booleans masquerading as numbers, non-HTTPS remote URLs, and any secret-looking key names (`*api_key*`, `*secret*`, `*token*`, `*password*`) inside TOML.

### `pagehelm.toml` schema

| Key | Type & constraint | Default / behavior |
| --- | --- | --- |
| `provider` | `"deepseek"` \| `"qwen"` \| `"openai"` | Resolves official `base_url`, default `model`, and `.env` key order |
| `mode` | `"read-only"` \| `"assisted-draft"` \| `"verified-operate"` | Local sandbox defaults `"verified-operate"`; cloud host defaults `"read-only"` |
| `model` | Non-empty string (<= 120 chars), must exist in `policies/prices.json` | Defaults by `provider`: `deepseek-flash`, `qwen3.8-flash`, or `gpt-6.1-sol` |
| `base_url` | `https://...` (or loopback `http://localhost` / `127.0.0.1`), no userinfo/query | Defaults by `provider` |
| `daily_allowance_cny` | Finite float `> 0` and `<= 1,000,000` | Per-actor daily CNY cap (`5.0` cloud, `20.0` local) |
| `daily_total_cap_cny` | Finite float `> 0` and `<= 1,000,000` | Process-wide daily atomic reserve cap (unset = disabled) |
| `monthly_total_cap_cny` | Finite float `> 0` and `<= 1,000,000` | Process-wide monthly atomic reserve cap (unset = disabled) |
| `max_sessions` | Integer `1 .. 100,000` | Maximum concurrent active sessions per host (default `300`) |
| `turn_timeout_s` | Integer `1 .. 3,600` | Per-turn execution wall deadline in seconds (default `120`, excludes approval wait) |

### `.env` variables

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` / `QWEN_API_KEY`, `OPENAI_API_KEY` | Provider credential selected automatically by `pagehelm.toml` `provider` |
| `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, `AGENT_PROVIDER` | Explicit overrides or legacy fallback when `pagehelm.toml` is omitted |
| `WOA_MODE`, `WOA_NATIVE_WRITES` | Env overrides (`read-only` / `assisted-draft` / `verified-operate`, or `true` / `false`; conflicting settings refuse startup) |

### Execution mode expansion (`expand_operation_mode`)

| `mode` | `native_page_admission` | `native_writes` | `native_submit` | `native_write_preparation` | `native_edit_prefill` |
| --- | --- | --- | --- | --- | --- |
| `read-only` | `read-only-window` | `false` | `false` | `false` | `false` |
| `assisted-draft` | `read-only-window` | `false` | `false` | `true` | `true` |
| `verified-operate` | `read-only-window` | `true` | `true` | `false` | `true` |

---

## 2. Gateway HTTP & SSE endpoints

After host `/bootstrap` exchanges the authenticated browser login for a short-lived capability, traffic routes under the gateway prefix (default `/woa-agent`):

| Route | Method | Auth / headers | Purpose |
| --- | --- | --- | --- |
| `/healthz`, `/readyz` | `GET` | Unauthenticated | Liveness probe and drain-aware readiness probe (`503` during shutdown drain) |
| `/v1/runtime/assistant.js` | `GET` | Same-origin static | Versioned runtime IIFE with `ETag`, `304`, and `.gz` (`404 runtime_unavailable` if unbuilt) |
| `/v1/sessions/<id>/hello` | `POST` | `Authorization: Bearer`, `Origin` | Validates `ClientHello` origin & digest; issues single active `lease_id` |
| `/v1/sessions/<id>/observations` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Uploads sanitized `ObservationEnvelope` + `PageObservation` |
| `/v1/sessions/<id>/messages` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Submits idempotent user task (`429` if backpressure / in-flight) |
| `/v1/sessions/<id>/claims`, `results` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Claims pending browser action and reports settled `ActionResultEnvelope` |
| `/v1/sessions/<id>/approvals` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Submits single-use `ApprovalDecision` (`approved`, `denied`, or `prefill`) |
| `/v1/sessions/<id>/cancellations` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Cancels running turn or marks queued pre-cancel race cleanly |
| `/v1/sessions/<id>/reconciliations` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Triggers same-actor read-only reconciliation for `INDETERMINATE` writes |
| `/v1/sessions/<id>/conversation` | `POST` | Bearer, `Origin`, `X-WOA-Lease` | Expects `{"action": "new"}` to clear retained history without clearing ledger |
| `/v1/sessions/<id>/events`, `timeline` | `GET` | Bearer, `Origin`, `X-WOA-Lease` | `Last-Event-ID` SSE push stream (server-to-browser only) and projected v2 timeline |

All `POST` requests require `Content-Type: application/json`, explicit `Content-Length` (`<= MAX_BODY_BYTES = 1 MiB`), and pass strict object validation (`SCHEMA_VERSIONS` = `1`). Unredacted session handoffs or credentials trigger HTTP `400` (`code: "privacy_boundary"`).

---

## 3. Annotated JSON wire examples

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
        "name": "Clue Name",
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
  "observation_after": { "...": "ObservationEnvelope at page_epoch 4" },
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

*(Allowed `decision` values: `"approved"`, `"denied"`, or `"prefill"`.)*

---

## 4. `panel_contract.json` (`protocol: 2`) & `presentation.json`

Both `EmbeddedGateway` and the Shadow DOM UI validate projected panel payloads against `src/web_operation_agent/web_runtime/panel_contract.json`:

- **Default brand copy (`label_keys`):** `product: "PageHelm"`, `emptyIntro: "你好，我是 PageHelm"` (CSS/DOM tokens remain `woa-*`).
- **Progress states (non-terminal):** `waiting_for_runtime`, `session_recovered`, `message_accepted`, `new_conversation`, `agent_started`, `step`, `approval_recorded`, `cancelling`.
- **Terminal states:** `needs_input`, `finished`, `refused`, `cancelled`, `indeterminate`, `failed`, `task_failed`. Unregistered state strings are rejected strictly.
- **Lapse & terminal notice codes:**
  - `lapse_codes` (silent reconnect/rehydrate): `principal_expired`, `session_expired`, `browser_inactive`, `host_stopped`, `server_stopped`, `bootstrap_replaced`, `native_bootstrap_replaced`, `unknown_session`, `not_found`.
  - `terminal_panel_state_by_code`: maps `session_closed`/`unknown_session`/`not_found`/`principal_expired`/`session_expired` -> `"ended"`, `runtime_replaced` -> `"replaced"`, and `unauthorized`/`invalid_lease`/`principal_mismatch`/`origin_refused` -> `"error"`.
- **v2 Structured UI payloads:** `table` (`StructuredTable` -> `<DataTable>`), `input_options` (2–4 `ClarificationOption` pills), `handoff` (redacted `completed_fields`, `pending_fields`, `restricted_controls`, `can_undo`), and local-only `debug_trace` (`first_route_kind`, `stop_reason`, `stage_timings`, `tool_timings`, `checks`).

UI safety & launcher geometry constants live in `frontend/src/presentation.json`:

| Constant | Value | Purpose |
| --- | --- | --- |
| `overlay_z_index` | `2147483646` | Keeps Shadow DOM assistant panel above host admin drawers/modals |
| `launcher_inset` | `20` | Edge spacing (px) for floating panel launcher |
| `launcher_size` | `48` | Launcher button size (px) |
| `approval_visibility.min_visible_ratio` | `0.9` | Minimum unclipped fraction of card before click is accepted |
| `approval_visibility.min_visible_ms` | `400` | Minimum visible dwell time (ms) before approval click is accepted |
| `start.final_statuses` | `[401, 403]` | Terminal bootstrap statuses indicating expired host authentication |

---

## 5. System hard bounds summary

| Boundary | Hard limit | Source |
| --- | --- | --- |
| Composer prompt length | `2000` chars | `panel_contract.json` / gateway |
| Request HTTP body size (`MAX_BODY_BYTES`) | `1 MiB` (`1,048,576` bytes) | `transport/gateway.py` |
| Event frame size (`_MAX_EVENT_BYTES`) | `256 KiB` | `transport/protocol.py` |
| Interactive controls per observation | `200` (`max_elements`) | `browser_contract.json` |
| Ephemeral `TurnTables` capacity | `36` tables, `5000` rows, `128` cols, `1.5s` SQL query | Adapter `turn_tables.py` |
| `<DataTable>` panel export | `24` cols, `2000` rows, `300` chars/cell | Adapter `turn_tables.py` / `panel_view.py` |
| Answer protocol limits | `50` conclusions max, `2` answer repairs, `50` presented rows | `answer/answer_protocol.py` |
| Model steps per turn | `24` (core default) / `33` (`YudaoTurnExecutor`) | `panel_contract.json` / `page_prompts.py` |

---

## Next steps

- Inspect all callable model tools, `GoalPort` capability declarations, and programmatic `ProgramCheckSpec` rules in [Built-in Tools & Capabilities](./tools-and-capabilities.md).
- Review end-to-end turn execution in [Execution Engine & Browser Runtime](../architecture/execution-and-runtime.md).
