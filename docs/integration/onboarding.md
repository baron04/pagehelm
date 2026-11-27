---
title: Declarative Application Onboarding
description: "Integrate PageHelm into a first-party web application on core ApplicationPorts, TurnTables SQL, CompiledWrites/WritePipeline, and the reference declarative onboarding modules under testapps/contacts/declarative/."
---

Core (`src/web_operation_agent/`) provides application-neutral composition ports (`ApplicationPorts`, `bind_application`), per-turn in-memory SQLite (`TurnTables`, `src/web_operation_agent/execution/turn_tables.py`), and verified write pipelines (`WritePipeline`, `CompiledWrites`). The single-app declarative REST/HTML helpers live in `testapps/contacts/declarative/` (kept outside the core distribution wheel until proven against a second structurally different adapter) and serve as the reference pattern for integrating a new backend without hand-writing a dozen separate port classes.

> Coding-agent onboarding workflow: load `.agents/skills/integrate-pagehelm/SKILL.md` (`$integrate-pagehelm`) to inspect your host application read-only, scaffold a `DeclarativeAppSpec` + `DeclarativeApplicationAdapter` + `DeclarativeSessionHost`, and run non-paid contract checks before enabling mutations.

## Prerequisites

Confirm three host prerequisites before writing integration code:

- **Same-origin mount:** Serve the PageHelm gateway (`/woa-agent`) and bootstrap endpoint (`/woa-agent-boot/bootstrap`) on the same browser origin as the host UI so cookies, SSE streams, and DOM observations remain same-origin.
- **Server-side session resolution:** Provide a server callback (`IdentityResolver`) mapping trusted request cookies or headers to `ResolvedActor` without trusting browser-supplied user IDs, query params, or DOM text.
- **Same-origin Runtime script:** Include the versioned `assistant.js` ES module plus a pinned `PageController` (reusing `FRONTEND_PRESETS["element-plus"]` or `"plain-html"`).

## 1. Three Host Inputs

A declarative PageHelm adapter needs only three application-side declarations (plus optional write specs):

| Input | SDK surface | What the host supplies | Reference |
| --- | --- | --- | --- |
| **1. Server Identity** | `IdentityResolver` / `HttpWhoamiIdentityPort` | Resolve trusted cookies/headers to `ResolvedActor(actor_id, permissions, tenant_id, workspace_id)` (fail-closed empty `permissions` default; real `delegated_resolver` when passing `api_token`, or `same_origin_shared_session=True`) | `testapps/contacts/declarative/declarative_identity.py` |
| **2. Data & Route Metadata** | `entities_from_openapi(...)` or `EntitySpec`, `PageSpec`, `FormSpec` | OpenAPI 3.x document or explicit entity/page/form descriptors, allowed route regexes, and `session_handoff_query_keys` | `testapps/contacts/declarative/openapi.py` |
| **3. UI Preset & Session Host** | `FRONTEND_PRESETS` + `DeclarativeSessionHost` | Mount `assistant.js`, pick `"element-plus"` or `"plain-html"` preset, and configure session/quota/mutation stores | `testapps/contacts/declarative/declarative_host.py` |
| **4. Optional Verified Writes** | `TypedWriteSpec`, `convention_updates`, `StatusToggleSpec`, `SubmitSpec` | Opt-in mutations governed end-to-end by `WritePipeline` / `CompiledWrites`, single-consume approval cards, and same-actor readback (`SubmitSpec` covers table row actions only; page forms prefill-only) | `testapps/contacts/declarative/declarative_writes.py` |

## 2. Frontend Mounting & UI Presets

Include the single versioned ES module on authenticated host pages:

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

On load, the Runtime requests `data-woa-bootstrap`. The host resolves the current actor server-side, opens or reuses the isolated session through `DeclarativeSessionHost.open_or_reuse_session(...)`, and returns `_browser_bootstrap(session)` (backed by `EmbeddedGateway.bootstrap_config(...)`). Model keys never reach the browser.

- **Vue 3 + Element Plus (`FRONTEND_PRESETS["element-plus"]`):** Reuses the five bundled helpers in `frontend/src/integrations/element-plus/` (`table.js`, `select.js`, `date-picker.js`, `form.js`, `vue.js`).
- **Plain Semantic HTML / Custom UI (`FRONTEND_PRESETS["plain-html"]`):** Serves a same-origin `PageController` ES module exporting `PageController` and `getElementByIndex` (plus custom `frontend/src/integrations/<app>/` helpers exporting `readElementPageControl`, optional `openPage(url)`, and `readPageBlocker()`).

## 3. Minimal Adapter & Loop Composition

Derive entities directly from an OpenAPI 3.x schema with `entities_from_openapi(...)` (which strips secret fields matching `DEFAULT_SECRET_FIELD_PATTERN` automatically and pairs `GET`/`PUT` record endpoints) or define `EntitySpec`, `PageSpec`, and `FormSpec` directly, then compose the adapter and session host:

```python
from testapps.contacts.declarative import (
    DeclarativeAppSpec,
    DeclarativeApplicationAdapter,
    DeclarativeModelGoalPort,
    FRONTEND_PRESETS,
    PageSpec,
    ResolvedActor,
    build_declarative_profile,
    entities_from_openapi,
)
from testapps.contacts.declarative.declarative_host import (
    DeclarativeHostConfig,
    DeclarativeSessionHost,
)

entities = entities_from_openapi(
    OPENAPI_SPEC,
    entity_overrides={
        "contacts": {
            "list_tool_name": "list_contacts",
            "list_filter_param": "name_contains",
            "list_filter_field": "name",
        }
    },
    default_read_scope="contacts:read",
    default_write_scope="contacts:write",
)

spec = DeclarativeAppSpec(
    application_id="my.app",
    display_name="My Application",
    entities=entities,
    pages=(PageSpec(key="contacts", label="Contacts", route="/contacts", entity="contacts"),),
    page_resolver=lambda cookie: ResolvedActor("actor-1", frozenset({"contacts:read"})),
    delegated_resolver=lambda: ResolvedActor("actor-1", frozenset({"contacts:read"})),
    # For same-origin cookie sessions without a separate api_token, omit api_token & delegated_resolver and set:
    # same_origin_shared_session=True,
    navigation_mode=FRONTEND_PRESETS["element-plus"].navigation_mode,
    tool_route_patterns=(r"/contacts", r"/contacts/[a-z0-9-]+"),
)

adapter = DeclarativeApplicationAdapter(spec, "https://app.example.test", api_token=delegated_token)
goal_port = DeclarativeModelGoalPort(adapter)
profile = build_declarative_profile(adapter, goal_port, enable_sql=True, time_zone="Asia/Shanghai")
```

With `enable_sql=True`, `build_declarative_profile(...)` automatically loads collected records into core's per-turn in-memory SQLite engine (`TurnTables`, `src/web_operation_agent/execution/turn_tables.py`), registers `sql`, `tables`, and `date_range`, and enables `finish(table_ref="q1")` to export redacted interactive `<DataTable>` blocks. Reference the refactored Contacts integration in `testapps/contacts/adapter.py` and `testapps/contacts/server.py`.

## 4. Optional Domain Playbooks & Disambiguation

To clarify domain terminology across modules or disambiguate same-name records before answering or writing:

- Author concise `domain_playbooks/*.md` files loaded on demand via `load_domain_guide`.
- Attach allowlisted `SourceTree` / `SourceCompiler` preconditions only over reviewed source directories (excluding secrets, uploads, databases, and user data).
- When 2–4 real same-actor records match a query, return clickable disambiguation options via `finish(status="need_input", options=[...])`.

## 5. Opt-In Verified Writes (`WritePipeline`)

Register write operations only for entities you want the assistant to modify; set `WOA_READ_ONLY=true` on the server whenever you want a global read-only kill-switch. Every write contract is enforced by `WritePipeline` ([Write Safety & Privacy](../security/verified-writes.md)) and [Skills & Typed Writes](./skills-and-writes.md):

1. **Symmetric flat convention updates (`convention_updates=True` & `StatusToggleSpec`):** Automatically generates `update_record` and two-state `toggle_record_status` for scalar non-secret fields on `EntitySpec`s that declare `convention_update=True`.
2. **Typed write operations (`TypedWriteSpec` + `SKILL.md` / `agents/woa.toml`):** Declares `normalizer`, `precondition_check`, `dispatch_fn`, and same-actor `readback_fn`; raises `BusinessMutationIndeterminate` without automatic retry if dispatch outcome is lost or readback disagrees.
3. **Page form prefill handover & inline row actions (`FormSpec` & `SubmitSpec`):** Page forms strictly use non-mutating field prefill (`DeclarativeMutationPrefill`, `fill_fields(then_highlight=...)`, and `undoFormFill`) and highlight the submit button for manual user confirmation—never auto-submitting forms or issuing form approval cards (calling `request_submit` on a page form automatically downgrades to prefill handover). Only reviewed inline table row status actions (`row_id`) use `SubmitSpec` + `SubmitConventions` single-consume `request_submit` approval cards.

## Pre-Merge Verification Checklist

Validate every new integration with non-paid synthetic tests before merge:

- Core neutrality & boundary check: `.venv/bin/python scripts/check_core_boundary.py`
- Diff-aware suite: `.venv/bin/python scripts/check.py --plan && .venv/bin/python scripts/check.py`
- Security & safety invariants: unauthenticated/tampered bootstrap refuses fail-closed; cross-tenant/cross-actor isolation holds; sensitive fields are stripped from SQL tables and egress; unapproved writes cause zero mutations; post-`claimed` failures enter `INDETERMINATE` without retry.
