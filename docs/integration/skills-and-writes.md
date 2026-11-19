---
title: Skills & Typed Writes
description: Author SKILL.md and agents/woa.toml packages, two-phase prepare/execute mutations, ConventionWritePort updates, and SubmitPort form handovers.
---

In PageHelm, Skills define organization-reviewed operating procedures and scope declarations; **Skills grant no execution or write permission on their own**. Every persistent change binds to a typed adapter write contract enforced by `WritePipeline` (see [Write Safety & Privacy](../security/verified-writes.md)).

## 1. Skill Package Layout and Validation

When loading a Skill directory, `src/web_operation_agent/skills/artifacts.py` builds an immutable content-addressed `SkillArtifact` snapshot so files cannot be modified or escaped via symlinks at runtime.

```text
skills/<kebab-case-name>/
├── SKILL.md          # Required: YAML frontmatter + 5-section procedure
├── agents/
│   └── woa.toml      # Optional: scope & write_operations (schema_version = 1)
├── references/       # Optional: UTF-8 references loaded via context.read_resource()
└── assets/           # Optional: small static assets
```

`SkillArtifact.from_files` enforces:

- **Size and file bounds:** `MAX_FILE_BYTES = 65536` (`64 KiB`), `MAX_PACKAGE_BYTES = 524288` (`512 KiB`), `MAX_FILES = 64`; only safe extensions (`.md`, `.txt`, `.csv`, `.json`, `.toml`, `.yaml`, `.yml`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg`); no symlinks, backslashes, or path traversal.
- **`SKILL.md` frontmatter:** `<= 8 KiB`, no YAML anchors, aliases, or custom tags; only keys `name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools`. `name` must match `[a-z0-9]+(?:-[a-z0-9]+)*` and equal the directory name (`allowed-tools` is advisory only and grants no permission).
- **Five-section body convention (see `examples/yudao/`):**
  1. **Target scope:** Entities and creation/maintenance flows covered by this Skill.
  2. **Adjacent non-targets:** Easily confused entities that this Skill does not handle.
  3. **Required user input:** Mandatory fields, when to ask before proceeding, and verbatim preservation rules.
  4. **Procedure using live facts:** How to resolve people, enums, and hierarchies and preserve default fields.
  5. **Completion criteria:** Single dispatch after approval, same-actor readback, and cross-owner visibility rules.
- **`agents/woa.toml` scope declaration:**
  ```toml
  schema_version = 1
  scope_entities = ["crm_clue"]
  scope_pages = ["crm/clue"]
  write_operations = ["create_clue", "update_clue"]
  ```
  When a write tool prepares a proposal, `SkillContext.support(skill_id, operation)` verifies that the Skill is loaded in the current turn and lists `operation` in `write_operations`, then emits `artifact_sha256`.

## 2. Two-Phase Typed Writes (`prepare_mutation` / `execute_mutation`)

Typed write tools separate proposal construction from irreversible execution:

1. **`prepare_mutation(args, context)`:** Verifies the active actor, reads the current record state, validates value provenance and append-preservation rules, and returns a `BusinessMutationProposal` with structured diffs, target identifiers, and `content_digest`.
2. **Approval and single-flight gate:** Handled by `WritePipeline` (see [Write Safety & Privacy](../security/verified-writes.md)).
3. **`execute_mutation(proposal, approval, context, claimed=...)`:** Invokes `claimed()` to record journal intent immediately before the external write call; raises `BusinessMutationIndeterminate` without retrying if a timeout or disconnect occurs after `claimed`; verifies postconditions via `ResultPostconditionVerifier` under the same actor.

Reference implementations live in `testapps/contacts/adapter.py` and `src/web_operation_agent/business/write_pipeline.py` (which provides `change_detail`, `create_detail`, `preserved_detail`, `side_effect_detail`, `derived_detail`, `target_rule_detail`, and `body_detail`).

## 3. Symmetric Flat Convention Updates (`ConventionWritePort`)

Adapters that declare `"updates"` in `GoalPort.capabilities()` can use `ConventionWritePort` (`src/web_operation_agent/business/convention_writes.py`) instead of hand-writing tools for every flat editable entity:

- **Field contract (`ConventionFieldSpec`):** Restricted to top-level scalar fields with 1:1 read/write symmetry (`string`, `integer`, `number`, `boolean`, `id_array`), with fixed-point scaling (`unit_scale`, e.g. `100` for cents) and timestamp formats (`epoch_ms`, `datetime`, `date`).
- **Secret exclusion:** Fields whose names or labels match `DEFAULT_SECRET_FIELD_PATTERN` (passwords, keys, tokens) are excluded.
- **Derived operations:** Generates `update_record` and two-state reversible `toggle_record_status`, both running `verify_actor` -> `resolve_target` -> `read_record` -> approval card -> `recheck_bindings` pre-dispatch reread -> single-flight dispatch -> same-actor readback of non-volatile fields.

## 4. Native Form Submit and Undoable Handover (`SubmitPort`)

For pages without dedicated typed write tools or when users prefer submitting on the live form, adapters can implement `SubmitPort` and `SubmitConventions` (`src/web_operation_agent/execution/submit_contract.py`):

1. **Declare `SubmitConventions`:** Specify `volatile_keys` (ignored during pre-dispatch reread), `record_key`, `line_key`, `name_key`, `epoch_zone`, and `decode_write(status, body)`.
2. **Single approved click (`request_submit`):** Reads the full form, checks value provenance and append preservation, displays the approval card, rereads live form and backend state after approval, clicks submit once under a single-use `SubmitPermit`, and verifies readback.
3. **Fallback to undoable prefill (`MutationPrefillOffer` / `MutationPrefillPort`):** When the user chooses "prefill only, I will submit", or when password/upload controls (`credential_field`) or non-verifiable forms are detected, switches to non-mutating draft prefill, skips restricted controls while focusing them for manual entry, and keeps `undoFormFill(turnId)` available in the panel.
