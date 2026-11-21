---
name: integrate-pagehelm
description: "Guide developers to integrate PageHelm into a first-party web application using the low-cost declarative onboarding SDK (IdentityResolver -> OpenAPI/EntitySpec -> frontend preset & DeclarativeSessionHost -> optional TurnTables SQL & WritePipeline writes) with fail-closed auth/write gates and non-paid contract tests. Trigger when asked to integrate, embed, connect, or adapt PageHelm into an app (接入 PageHelm / 集成助手 / 新建适配器)."
---

# Integrate PageHelm (Declarative Onboarding SDK)

Use this workflow when adapting PageHelm into a first-party web application. Read [Declarative Application Onboarding](../../../docs/integration/onboarding.md) ([简体中文](../../../docs/zh-cn/integration/onboarding.md)) and use the declarative REST/HTML reference in [testapps/contacts/declarative/](../../../testapps/contacts/declarative/), [testapps/contacts/adapter.py](../../../testapps/contacts/adapter.py), and [testapps/contacts/server.py](../../../testapps/contacts/server.py) alongside core [composition ports](../../../src/web_operation_agent/composition/ports.py), [TurnTables](../../../src/web_operation_agent/execution/turn_tables.py), and [CompiledWrites](../../../src/web_operation_agent/business/compiled_writes.py) as the structural model—do **not** copy `examples/yudao` application-specific code into reusable layers or `src/web_operation_agent/`.

Default rollout target is **read-only end-to-end (`allow_mutations=False`)** unless the user explicitly authorizes write enablement.

## 1. Non-Negotiable Security & Architecture Rules

- **Server owns intelligence & trust; browser only observes and renders.** Planning, model keys, session policy, write authorization, audit, and panel copy/limits live strictly on the server. Never ship API keys, entity indexes, hidden permissions, or evaluators in frontend HTML/JS.
- **Server-side identity only.** Resolve `ResolvedActor(actor_id, permissions, tenant_id, workspace_id)` inside `IdentityResolver` or `HttpWhoamiIdentityPort` from trusted same-origin session cookies or auth headers (`permissions` defaults to fail-closed `frozenset()`; when passing independent `api_token`/`headers_factory`, always supply a real `delegated_resolver`, or explicitly set `same_origin_shared_session=True` only when sharing one same-origin session credential with no separate `api_token`). Never trust client-supplied user IDs, query params, or DOM text.
- **Same-origin mount required.** Serve the gateway (`/woa-agent/*`) and bootstrap endpoint (`/woa-agent-boot/bootstrap`) on the host application's origin. If cross-origin, fix reverse-proxy routing first—do not weaken origin checks.
- **Keep core neutral.** Never place app names, routes, fixture paths, single-app declarative wrappers, or business logic inside `src/web_operation_agent/` (core houses `ApplicationPorts`, `TurnTables`, `WritePipeline`, and `CompiledWrites`). Verify with [scripts/check_core_boundary.py](../../../scripts/check_core_boundary.py).
- **Never fake capabilities.** `DeclarativeGoalPort` / `DeclarativeModelGoalPort` must declare only capabilities actually implemented on the backend/UI.
- **Writes default off (`mode = "read-only"`, `allow_mutations=False`).** Every opt-in mutation must go through `WritePipeline` with fresh visible approval bound to the exact target and effect, consumed once, `claimed()` persisted before external dispatch, never retried after `claimed` or timeout (`BusinessMutationIndeterminate`), and verified via same-actor readback.

## 2. Step 0: Read-Only Host Preflight

Inspect the developer's application repository read-only before generating integration code and confirm:

1. **Origin & proxy topology:** Where `/woa-agent/` and `/woa-agent-boot/bootstrap` mount on the same origin as the UI.
2. **Server-side session resolution:** How request cookies or headers resolve to `ResolvedActor` (either a direct server callback `IdentityResolver` or an internal `/api/whoami` endpoint for `HttpWhoamiIdentityPort`).
3. **OpenAPI / Entity Metadata:** Whether an OpenAPI 3.x schema exists for `entities_from_openapi(...)`, or which `EntitySpec`, `PageSpec`, and `FormSpec` declarations are needed, plus allowed route regexes and `session_handoff_query_keys`.
4. **Frontend UI stack (`FRONTEND_PRESETS`):**
   - `"element-plus"` (Vue 3 + Element Plus): reuse the five helpers in [frontend/src/integrations/element-plus/](../../../frontend/src/integrations/element-plus/).
   - `"plain-html"` or custom UI library: serve a pinned same-origin `PageController` ES module and sibling helper module in `frontend/src/integrations/<app>/` exporting `readElementPageControl`, optional `openPage(url)`, and `readPageBlocker()`.

Stop and agree on any missing prerequisite before writing files.

## 3. Declarative Onboarding Workflow

Consult [Configuration & Protocol](../../../docs/reference/protocol-and-configuration.md), [Write Safety & Privacy](../../../docs/security/verified-writes.md), and [Skills & Typed Writes](../../../docs/integration/skills-and-writes.md) ([简体中文](../../../docs/zh-cn/integration/skills-and-writes.md)) for contract fields rather than duplicating core code.

1. **Mount Browser Runtime & Host Gateway:**
   - Include `<script type="module" src="/woa-agent/v1/runtime/assistant.js" data-woa-bootstrap="/woa-agent-boot/bootstrap"></script>` on host pages.
   - Subclass `DeclarativeSessionHost` (configured with `DeclarativeHostConfig`) to own `SQLiteSessionStore`, `SQLitePrincipalRevocations`, `SQLiteMutationJournal`, `ProviderRuntimeGuard`, and `open_or_reuse_session(...)` / `_revoke_actor_id(...)`. Reject unauthenticated `/bootstrap` requests fail-closed.

2. **Declare Application Metadata (`DeclarativeAppSpec`):**
   - Call `entities_from_openapi(openapi_doc, ...)` (automatically excludes secret fields and pairs `GET`/`PUT` item routes) or declare `EntitySpec`, `PageSpec` (including optional `QueryFormSpec` and `sections`), and `FormSpec`.
   - Set `navigation_mode=FRONTEND_PRESETS[<preset>].navigation_mode`, `tool_route_patterns`, and `session_handoff_query_keys`.
   - Instantiate `DeclarativeApplicationAdapter(spec, origin, api_token=...)`.

3. **Bind Goal Resolution & Turn Profile (`TurnTables` SQL + `<DataTable>`):**
   - Construct `DeclarativeModelGoalPort(adapter, drafts=..., filters=..., updates=...)`.
   - Build the turn profile via `build_declarative_profile(adapter, goal_port, drafts=..., filters=..., enable_sql=True, time_zone=...)`.
   - When `enable_sql=True`, core's `ModelDrivenTurnExecutor` automatically instantiates per-turn `TurnTables`, ingests fetched rows into `t1..tN`, exposes `sql`, `tables`, and `date_range`, and exports redacted `<DataTable>` blocks on `finish(table_ref="q1")`.

4. **Optional Verified Writes (Only When Explicitly Authorized):**
   - **`convention_updates=True` & `StatusToggleSpec`:** Automatically derives `update_record` and `toggle_record_status` for symmetric flat scalar fields.
   - **`TypedWriteSpec` / `CompiledWrites` + `SKILL.md` / `agents/woa.toml`:** Supplies `normalizer`, `precondition_check`, `dispatch_fn`, and `readback_fn` (or compiled CRUD/nested aggregate/relation specs via `src/web_operation_agent/business/compiled_writes.py`); executes inside `WritePipeline` with single-use approval and `BusinessMutationIndeterminate` protection.
   - **`FormSpec` & row-action `SubmitSpec` (`DeclarativeDraftSteps`, `DeclarativeMutationPrefill`, `DeclarativeSubmitPort`):** Page forms **only prefill fields and highlight the submit button** (`fill_fields(then_highlight=...)` + `undoFormFill`, `allow_form_submit=False`) for manual user confirmation—never auto-submit forms or emit form approval cards (`request_submit` on page forms downgrades to prefill handover). Only reviewed inline table row status actions (`row_id`) use `SubmitSpec` with single-consume `request_submit` approval cards.

## 4. Non-Paid Contract & Safety Verification

Do **not** invoke paid LLM providers or touch real spend ledgers in tests. Write isolated synthetic tests using scripted doubles (`ScriptedDemoChat` or `_Chat` from `src/web_operation_agent/tests/support/`) verifying:

1. **Auth & actor isolation:** Unauthenticated or tampered-cookie `/bootstrap` requests fail closed; different tenants/actors see only their own records; secret fields are stripped before SQL storage and model egress.
2. **Route & capability honesty:** Unreviewed routes, cross-origin URLs, and undeclared capabilities are refused cleanly without guessing.
3. **Write safety (if enabled):** Unapproved or denied proposals cause zero backend writes; failures after `claimed()` become `INDETERMINATE` without retry; readback mismatch fails closed; form prefill stops before manual submit.

Run the verification gates inside `.venv`:

```bash
.venv/bin/python scripts/check_core_boundary.py
.venv/bin/python scripts/check.py --plan
.venv/bin/python scripts/check.py
```
