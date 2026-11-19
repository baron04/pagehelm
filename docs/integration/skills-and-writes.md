---
title: Skills & Typed Write Adapters
description: Packaging validated SKILL.md modules with agents/woa.toml, implementing two-phase prepared writes, ConventionWritePort symmetry, and SubmitConventions handoffs.
---

> **TL;DR:** Skills in PageHelm supply domain procedures and opt-in metadata declarations—never standalone runtime authority. Packages are validated at install time (`SkillArtifact`, max `64 KiB` / file, `512 KiB` / package, `64` files), while actual state transitions bind strictly to typed adapter write contracts (`prepare_mutation` / `execute_mutation`, `ConventionWritePort`, or `SubmitPort`) enforced by `WritePipeline`.

[简体中文](../zh-cn/integration/skills-and-writes.md)

## 1. Skill package format (`SKILL.md` + `agents/woa.toml`)

[`src/web_operation_agent/skills/artifacts.py`](../security/verified-writes.md) validates every Skill bundle symmetrically into an immutable content-addressed snapshot (`SkillArtifact`) upon installation. Neither runtime turns nor user approvals can swap files or follow symlinks afterward.

### Directory structure & strict limits

```text
skills/<kebab-case-name>/
├── SKILL.md                # Required: YAML frontmatter + structured 5-section guidance
├── agents/
│   └── woa.toml            # Optional host scope & write_operations allowlist (schema_version = 1)
├── references/             # Optional UTF-8 supporting guides read lazily via context.read_resource()
│   └── ...
└── assets/                 # Optional small static assets
```

Enforcement rules in `SkillArtifact.from_files`:

- **Size & count bounds:** `MAX_FILE_BYTES = 65536` (`64 KiB`), `MAX_PACKAGE_BYTES = 524288` (`512 KiB`), `MAX_FILES = 64`; allowed extensions only (`.md`, `.txt`, `.csv`, `.json`, `.toml`, `.yaml`, `.yml`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg`), no symlinks, no `..` traversal or backslash escapes.
- **`SKILL.md` frontmatter:** <= `8 KiB` safe YAML without anchors/aliases/tags; permitted keys only `{"name", "description", "license", "compatibility", "metadata", "allowed-tools"}`; `name` must match `[a-z0-9]+(?:-[a-z0-9]+)*` (and match its folder name). Note that `allowed-tools` is strictly descriptive—host scope checks and approval gates remain authoritative.
- **Recommended 5-section `SKILL.md` structure:** Consistent production Skills organize guidance into five explicit sections (as seen in `examples/yudao/` Skills):
  1. **Target scope (`适用目标`)** — exact record lifecycle the Skill owns
  2. **Adjacent non-targets (`相邻目标`)** — easily confused entities/operations it does **not** cover
  3. **Required user inputs (`所需用户信息`)** — mandatory fields, ambiguity rules, and verbatim preservation requirements
  4. **Current-facts workflow (`利用当前事实的流程`)** — candidate lookups, dictionary/unit alignment, preserved defaults
  5. **Completion & verification (`完成条件`)** — single-dispatch rule, readback verification, ownership/permission caveats
- **`agents/woa.toml` policy header:**
  ```toml
  schema_version = 1
  scope_entities = ["crm_clue"]
  scope_pages = ["crm/clue"]
  write_operations = ["create_clue", "update_clue"]
  ```
  `SkillContext.support(skill_id, operation)` requires the Skill to be loaded in the active turn AND declare that operation in `write_operations` before returning its `artifact_sha256` support proof.

---

## 2. Two-phase typed writes (`prepare_mutation` / `execute_mutation`)

Typed write tools separate **proposal preparation** from **irreversible execution** so `WritePipeline` can inspect diffs, display the visible approval card, verify no pre-dispatch drift occurred, and isolate `INDETERMINATE` outcomes cleanly:

```text
  tool handler invoked
         |
         v
  prepare_mutation(args, context)
    - Validates actor identity & live target record
    - Verifies user value provenance (no guessed IDs or overwritten appends)
    - Returns BusinessMutationProposal (exact diffs, target, content_digest)
         |
         v
  WritePipeline approves & acquires single-flight lock + re-reads record
         |
         v
  execute_mutation(proposal, approval, context, claimed=...)
    - Calls claimed() right before issuing the irreversible HTTP/DB mutation
    - Raises BusinessMutationIndeterminate on timeout/drop after claim
    - Verifies postcondition via ResultPostconditionVerifier
```

Study `testapps/contacts/adapter.py` and `src/web_operation_agent/business/write_pipeline.py` for runnable implementations showing exact `FieldDiff` helpers (`change_detail`, `create_detail`, `preserved_detail`, `side_effect_detail`).

---

## 3. Symmetric flat updates (`ConventionWritePort`)

Writing custom Skill handlers for dozens of simple entity edit screens creates unnecessary boilerplate. Adapters declaring `"updates"` in `GoalPort.capabilities()` can implement `ConventionWritePort` (`src/web_operation_agent/business/convention_writes.py`):

- **`ConventionFieldSpec` constraints:** Scalar top-level fields (`string`, `integer`, `number`, `boolean`, `id_array`) with verified 1:1 round-trip fidelity between read and update DTOs; explicitly supports integer fixed-point scaling (`unit_scale`, e.g., `100` for currency cents) and date representations (`epoch_ms`, `datetime`, `date`).
- **Secret exclusion:** Automatically rejects any field whose name or label matches `DEFAULT_SECRET_FIELD_PATTERN` (passwords, API keys, tokens, etc.).
- **Unified safety chain:** Every generated `update_record` and two-state reversible `toggle_record_status` call executes `verify_actor` -> `resolve_target` -> `read_record` -> approval card (`before -> after` diffs + preserved fields) -> pre-dispatch reread (`recheck_bindings`) -> single-flight dispatch (`dispatch_update` / `dispatch_status`) -> same-actor readback verification.

---

## 4. Native form submission & prefill conversion (`SubmitConventions`)

When no backend API write tool matches—or when the user explicitly asks to act inside the open screen—`SubmitPort` + `SubmitConventions` (`src/web_operation_agent/execution/submit_contract.py`) enables safe native form control:

1. **Declare record conventions (`SubmitConventions`):** Provide `volatile_keys` (timestamps/versions ignored in drift checks), `record_key`, `line_key`, `name_key`, `epoch_zone`, and `decode_write(status, body)`.
2. **Two safe delivery outcomes:**
   - **Verified one-click submit (`request_submit`):** Program reads full form data, verifies value provenance & append preservation, renders approval card, re-reads both form and record, presses the button once under a single-request `SubmitPermit`, and verifies via same-actor readback.
   - **Pre-approval form handover (`MutationPrefillOffer` / `MutationPrefillPort`):** When a mutation proposal can be converted to an interactive draft—or when the operator selects *"Fill form only"* on the card, or a form hits `credential_field` / unreadable fields—the server hands off a transient form draft without authorizing submission, skips secret fields cleanly, and exposes `undoFormFill(turnId)` to restore exact prior field values.

---

## Next steps

- Inspect all built-in tool schemas and `GoalPort` capabilities in [Built-in Tools & Capabilities](../reference/tools-and-capabilities.md).
- Check full `/v1/sessions/*` and `panel_contract.json` schemas in [Protocol & Configuration Reference](../reference/protocol-and-configuration.md).
