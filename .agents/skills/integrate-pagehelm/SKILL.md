---
name: integrate-pagehelm
description: "Guide developers to integrate PageHelm into a first-party web application incrementally (L0 panel embedding -> L1 same-actor reads -> L2 playbooks/disambiguation -> L3 verified writes) with server-side identity, fail-closed write safety, and non-paid contract tests. Trigger when asked to integrate, embed, connect, or adapt PageHelm into an app (接入 PageHelm / 集成助手 / 新建适配器)."
---

# Integrate PageHelm (L0–L3 Onboarding)

Use this workflow when adapting PageHelm into a first-party web application. Read [L0–L3 Integration Guide](../../../docs/integration/levels.md) ([简体中文](../../../docs/zh-cn/integration/levels.md)) and use the minimal heterogeneous REST/HTML app in [testapps/contacts/adapter.py](../../../testapps/contacts/adapter.py) as the structural reference—do **not** copy `examples/yudao` application-specific code into reusable layers or `src/web_operation_agent/`.

Default rollout target is **L0 + L1 read-only end-to-end** unless the user explicitly asks to enable L2 or L3.

## 1. Non-Negotiable Security & Architecture Rules

- **Server owns intelligence & trust; browser only observes and renders.** Planning, model keys, session policy, write authorization, audit, and panel copy/limits live strictly on the server. Never ship API keys, entity indexes, hidden permissions, or evaluators in frontend HTML/JS.
- **No forged identity.** Resolve `PrincipalContext(tenant_id, actor_id, scopes)` server-side inside `IdentityPort` from trusted same-origin session cookies or auth headers (use `bind_matching_identity` so runtime credentials and delegated API credentials agree). Never trust client-supplied user IDs, query params, or DOM text.
- **Same-origin mount required.** Serve the gateway (`/woa-agent/*`) and bootstrap endpoint (`/woa-agent-boot/bootstrap`) on the host application's origin. If cross-origin, fix nginx/reverse-proxy routing first—do not weaken origin checks.
- **Keep core neutral.** Never place app names, routes, fixture paths, or business logic inside `src/web_operation_agent/`. Verify with [scripts/check_core_boundary.py](../../../scripts/check_core_boundary.py).
- **Never fake capabilities.** Only declare flags in `GoalPort.capabilities()` (see [Tools, Capabilities & Checks](../../../docs/reference/tools-and-capabilities.md)) that are genuinely backed by same-actor code.
- **Mutations default off (`mode = "read-only"`).** Do not enable business writes until L1 read contracts and actor isolation pass. Every mutation must traverse `WritePipeline` with fresh visible approval bound to the exact target and effect, consumed once, `claimed()` persisted before external dispatch, never retried after `claimed` or timeout (`BusinessMutationIndeterminate`), and verified via same-actor readback (`ResultPostconditionVerifier`).

## 2. Step 0: Read-Only Host Preflight

Inspect the developer's application repository read-only before generating adapter code and report any missing prerequisites up front:

1. **Origin & proxy topology:** Where can `/woa-agent/` and `/woa-agent-boot/bootstrap` be mounted on the same origin as the UI?
2. **Server-side session lookup:** Which backend handler or token introspection endpoint maps cookies/headers to the signed-in actor, tenant, and permissions?
3. **Frontend stack:**
   - Vue 3 + Element Plus: plan to reuse the five helpers in [frontend/src/integrations/element-plus/](../../../frontend/src/integrations/element-plus/) (`table.js`, `select.js`, `date-picker.js`, `form.js`, `vue.js`).
   - Other UI frameworks (React/AntD/plain HTML): plan a sibling adapter directory `frontend/src/integrations/<app>/` exporting `readElementPageControl`, optional `openPage(url)`, and `readPageBlocker()`.
4. **Routing & navigation:** Confirm `"document"` vs `"spa"` (`ApplicationDescriptor.navigation_mode`), allowlisted in-app routes, and any auth/session query params that must go into `session_handoff_query_keys` for redaction.

Stop and agree on any missing prerequisite (especially same-origin routing or session resolution) before writing files.

## 3. Incremental Stages (L0 → L3)

Consult [Configuration & Protocol](../../../docs/reference/protocol-and-configuration.md), [Write Safety & Privacy](../../../docs/security/verified-writes.md), and [Skills & Typed Writes](../../../docs/integration/skills-and-writes.md) ([简体中文](../../../docs/zh-cn/integration/skills-and-writes.md)) for schema and contract details rather than duplicating signatures.

### L0: Same-Origin Panel & Bootstrap

1. Inject the versioned module script into host pages:
   ```html
   <script
     type="module"
     src="/woa-agent/v1/runtime/assistant.js"
     data-woa-bootstrap="/woa-agent-boot/bootstrap"
   ></script>
   ```
2. Implement the `/bootstrap` endpoint following [testapps/contacts/server.py](../../../testapps/contacts/server.py): reject unauthenticated callers fail-closed, bind the resolved server identity to a session grant, and return `EmbeddedGateway.bootstrap_config(...)`.
3. Serve the pinned same-origin `PageController` ES module (exporting `PageController` and `getElementByIndex`) matching the descriptor hash.

### L1: Same-Actor Read Adapter

Follow the separation in `testapps/contacts/`:

1. **Descriptor & Ports ([testapps/contacts/adapter.py](../../../testapps/contacts/adapter.py)):**
   - Declare `ApplicationDescriptor` (`application_id`, `public_origin`, `navigation_mode`, `page_controller_module`, `business_tools.catalog_sha256`).
   - Implement `IdentityPort`, `DeclaredRoutePort`, `DeclaredDataClassifier`, `EntityReadTools`, `ReferenceLookupTools`, and `PageEntryTools`, then wire them via `bind_application(adapter)`.
   - Return `QueryEvidence` (`matched_count`, `count_exact`, `looked_for`, `exact_matches`) accurately—never report truncated reads as complete totals.
2. **Goal Index & Capabilities ([testapps/contacts/goal.py](../../../testapps/contacts/goal.py)):**
   - Build `ApplicationIndex` using actor-visible entities, fields, aliases, and page routes.
   - Only list actually implemented strings in `capabilities()` (for example `"sections"`, `"handoffs"`, `"values"`, `"facets"`, `"updates"`).
3. **Turn Profile & Safety ([testapps/contacts/model_loop.py](../../../testapps/contacts/model_loop.py)):**
   - Wrap read payloads with `BEGIN_UNTRUSTED_APPLICATION_DATA` / `END_UNTRUSTED_APPLICATION_DATA` after `redact_sensitive_assignments(...)` and `SessionHandoffPolicy` stripping.
   - Support `SummaryRead` / `compute_summary` and `StructuredQueryAnswer` (`deliver`) cleanly; never accept unredacted secret fields into summary projections.

### L2: Domain Playbooks & Ambiguity Options (Optional)

Add L2 only after L1 read queries work cleanly across multiple users:

- Author concise module definitions in `domain_playbooks/*.md` so terminology ambiguities (for example CRM Customer vs ERP Counterparty) are resolved via `load_domain_guide`.
- Wire `SourceCompiler` / `entity_rules` strictly over allowlisted source files (never secrets, uploads, databases, or user data).
- When name resolution yields 2–4 real same-actor candidates, let the model render `finish(status="need_input", options=[...])` clickable pills rather than guessing.

### L3: Verified Writes & Form Handover (Opt-In Only)

Only enable `mode = "verified-operate"` when explicitly authorized after L1 verification. Choose the simplest contract per operation, in order:

1. **`ConventionWritePort` (`"updates"` capability):** Use for 1:1 symmetric flat scalar entities (`ConventionFieldSpec`, `unit_scale`, `epoch_ms`/`datetime`/`date`), automatically excluding nested lines and `DEFAULT_SECRET_FIELD_PATTERN` secret fields.
2. **Typed Write Skills (`SKILL.md` + `agents/woa.toml`):** Follow [Skills & Typed Writes](../../../docs/integration/skills-and-writes.md) and split tools into:
   - `prepare_mutation(args, context)` -> actor check, target freshness check, duplicate refusal, user/candidate value provenance check -> returns exact `BusinessMutationProposal`.
   - `execute_mutation(proposal, approval, context, claimed=...)` -> call `claimed()` **before** network mutation, raise `BusinessMutationIndeterminate` without retry on post-`claimed` failure/timeout, then verify via same-actor readback.
3. **Native Page Forms (`SubmitPort` & `MutationPrefillOffer`):** Reference [testapps/contacts/native_submit.py](../../../testapps/contacts/native_submit.py) and [testapps/contacts/mutation_prefill.py](../../../testapps/contacts/mutation_prefill.py) for `SubmitConventions` (`volatile_keys`, `decode_write`) and automatic fallback to undoable prefill (`undoFormFill`) whenever credential/upload fields or manual submission handover applies.

## 4. Non-Paid Contract & Safety Verification

Do **not** invoke paid LLM providers or touch real spend ledgers in tests. Write isolated synthetic tests using scripted doubles (`ScriptedDemoChat` / `SkillDeclarationChat` from [testapps/contacts/demo.py](../../../testapps/contacts/demo.py) and `src/web_operation_agent/tests/support/`) covering:

1. **L0 auth fail-closed:** Unauthenticated or tampered-cookie `/bootstrap` requests fail; session grant stays scoped to the resolved actor.
2. **L1 actor isolation & route fencing:** Different actors see only their own records; disallowed routes and truncated counts fail or surface incompleteness accurately.
3. **L3 approval & retry safety (if enabled):** Unapproved or denied proposals cause zero backend writes; write failures after `claimed()` become `INDETERMINATE` without automatic retry; readback mismatch fails closed.

Finally run the repository gates in `.venv`:

```bash
.venv/bin/python scripts/check_core_boundary.py
.venv/bin/python scripts/check.py --plan
.venv/bin/python scripts/check.py
```
