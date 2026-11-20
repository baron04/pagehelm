"""Standard Skill/operation proof over the synthetic application's existing REST API."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from web_operation_agent.business.convention_writes import (
    ConventionFieldSpec,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
    WriteRouteCapability,
    convention_tool_definitions,
)
from web_operation_agent.business.effects import EffectProjection, RequestedEffect, canonical
from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessToolIdentityError,
    BusinessToolPermissionError,
    BusinessToolReferenceError,
    BusinessToolRequestError,
)
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.business.write_pipeline import ProposalDiff, create_detail
from web_operation_agent.composition.business_operations import RegisteredOperations
from web_operation_agent.skills import SkillArtifact, SkillRegistry


class ContactCreation:
    def __init__(self, executor):
        self.executor = executor

    def prepare(self, args, *, context=None):
        spec = self.executor.prepare_mutation("create_contact", args, context=context)
        diff = ProposalDiff.from_details(
            spec.targets,
            tuple(create_detail(name, value) for name, value in spec.args.items()),
        )
        return replace(
            spec,
            details=diff.details(),
            effect_projection=EffectProjection(self.project_inputs(spec.args), required_receipts=("record",)),
        )

    def project_inputs(self, args):
        return RequestedEffect("contacts", "create", values_json=canonical(args))

    def dispatch(self, args, *, context=None):
        result = self.executor.execute_mutation("create_contact", args, context=context)
        contact = result.content["contact"]
        return replace(
            result,
            content={
                **result.content,
                "verified": True,
                "entity": "contact",
                "reference": contact["name"],
                "record_id": contact["id"],
                "effects": [{"effect": "record", "verified": True, "id": contact["id"]}],
            },
        )


_CONTACTS_UPDATE_SPEC = ConventionUpdateSpec(
    entity="contacts",
    label="Contact",
    fields=(
        ConventionFieldSpec("name", "string", label="Name", required=True, max_length=120),
        ConventionFieldSpec("email", "string", label="Email", required=True, max_length=200),
    ),
)

_SUBSCRIPTION_STATUS_SPEC = TwoStateStatusSpec(
    entity="contact-subscriptions",
    label="Contact subscription",
    states=(("enabled", "enabled"), ("disabled", "disabled")),
    synonyms=(("开启", "enabled"), ("启用", "enabled"), ("关闭", "disabled"), ("停用", "disabled")),
)


class ContactsConventionWritePort:
    """Second-adapter proof of the neutral ConventionWritePort over MinimalContacts REST."""

    def __init__(self, executor, identity):
        self.executor = executor
        self.identity = identity

    def update_specs(self) -> dict[str, ConventionUpdateSpec]:
        return {"contacts": _CONTACTS_UPDATE_SPEC}

    def status_specs(self) -> dict[str, TwoStateStatusSpec]:
        return {"contact-subscriptions": _SUBSCRIPTION_STATUS_SPEC}

    def write_route_capability(self) -> WriteRouteCapability:
        return WriteRouteCapability(
            compiled_api_writes=True,
            inline_row_status_submit=False,
            page_form_mode="prefill_handoff",
        )

    def verify_actor(self, context) -> str:
        if context is None:
            raise BusinessToolIdentityError("convention writes require an actor-bound context")
        context.require_live(application_id="minimal.contacts", required_scope="contacts:write")
        assertion, permissions = self.identity._whoami(
            {"Authorization": f"Bearer {self.executor._api_token}"}, "delegated_api"
        )
        principal = context.principal
        if (assertion.actor_id != principal.actor_id
                or assertion.workspace_id != principal.workspace_id
                or assertion.tenant_id != principal.tenant_id):
            raise BusinessToolIdentityError("delegated API token no longer matches bound actor")
        if "contacts:write" not in permissions:
            raise BusinessToolPermissionError("actor lacks contacts:write permission")
        return principal.actor_id

    def resolve_target(self, entity: str, target: str, *, spec) -> dict:
        rows = self.executor._contact_rows()
        wanted = target.strip()
        qualified = re.fullmatch(r"(.+)（ID=([^()（）]+)）", wanted)
        if qualified:
            exact = [r for r in rows if r["name"] == qualified.group(1) and r["id"] == qualified.group(2)]
        else:
            exact = [r for r in rows if r["name"].casefold() == wanted.casefold() or r["id"] == wanted]
        if len(exact) != 1:
            choices = tuple(f"{r['name']}（ID={r['id']}）" for r in rows)[:6]
            raise BusinessToolReferenceError(
                "contact is missing or ambiguous; supply exact name",
                entity=entity,
                choices=choices,
            )
        record = self.read_record(entity, exact[0]["id"])
        if not isinstance(record, dict):
            raise BusinessToolRequestError("target record is not readable")
        return record

    def read_record(self, entity: str, record_id) -> dict | None:
        sub_path = (
            f"/api/contacts/{record_id}"
            if entity == "contacts"
            else f"/api/contact-subscriptions/{record_id}"
        )
        req = urllib.request.Request(
            self.executor.origin + sub_path,
            headers={"Authorization": f"Bearer {self.executor._api_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (401, 403):
                raise BusinessToolPermissionError("readback denied") from exc
            raise BusinessToolRequestError(f"read failed: HTTP {exc.code}") from exc
        key = "contact" if entity == "contacts" else "subscription"
        return payload.get(key) if isinstance(payload, dict) else None

    def encode_field(self, spec: ConventionUpdateSpec, field: ConventionFieldSpec, raw_value, current_record: dict):
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise BusinessToolRequestError(f"{field.name} must be a non-empty string")
        val = raw_value.strip()
        if field.max_length and len(val) > field.max_length:
            raise BusinessToolRequestError(f"{field.name} exceeds max length {field.max_length}")
        if field.name == "email":
            val = val.casefold()
        old_val = str(current_record.get(field.name) or "")
        return val, old_val, val, None

    def recheck_bindings(self, bindings: list[dict], *, actor_id: str) -> None:
        return None

    def _put_once(self, sub_path: str, payload: dict) -> None:
        req = urllib.request.Request(
            self.executor.origin + sub_path,
            data=json.dumps(payload).encode("utf-8"),
            method="PUT",
            headers={
                "Authorization": f"Bearer {self.executor._api_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    raise BusinessMutationIndeterminate("unexpected status", phase="dispatch")
                json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise BusinessToolPermissionError("application denied write") from exc
            raise BusinessMutationIndeterminate("HTTP error during PUT", phase="dispatch") from exc
        except (OSError, ValueError) as exc:
            raise BusinessMutationIndeterminate("transport error during PUT", phase="dispatch") from exc

    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None:
        self._put_once(f"/api/contacts/{record_id}", payload)

    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None:
        self._put_once(f"/api/contact-subscriptions/{record_id}", {"id": str(record_id), "status": status_code})

    def invalidate_reads(self, entities: frozenset[str]) -> None:
        return None


def contact_operations(executor, enabled: bool, *, convention_updates: bool = False, identity=None):
    base_skills = SkillRegistry.discover(Path(__file__).parent / "packages", namespace="minimal.contacts")
    artifacts = list(base_skills.artifacts)
    schema = next(
        (s["function"] for s in executor.tool_schemas() if s["function"]["name"] == "create_contact"),
        None,
    )
    definitions = (
        ()
        if not enabled
        else (
            ToolDefinition(
                "create_contact",
                schema["description"],
                json.dumps(schema["parameters"]),
                "write",
                "contacts:write",
                ToolOperation("contact", "create"),
                ContactCreation(executor),
                literal_inputs=("name", "email"),
                input_fields=(("name", "contacts", "name"), ("email", "contacts", "email")),
                effect_contract=ContactCreation(executor),
            ),
        )
    )
    if convention_updates and identity is not None:
        conv_skill = SkillArtifact.from_files(
            "minimal.contacts",
            {
                "SKILL.md": (
                    b"---\nname: record-convention-maintenance\n"
                    b"description: Update flat contact fields and toggle two-state contact subscriptions.\n---\n"
                    b"# Convention Maintenance\nUse update_record for flat contact updates and toggle_record_status for two-state subscription toggles.\n"
                ),
                "agents/woa.toml": (
                    b'schema_version = 1\n'
                    b'scope_entities = ["contact-subscriptions"]\n'
                    b'write_operations = ["update_record", "toggle_record_status"]\n'
                ),
            },
        )
        artifacts.append(conv_skill)
        if enabled:
            port = ContactsConventionWritePort(executor, identity)
            definitions = (*definitions, *convention_tool_definitions(port, scope="contacts:write"))
    skills = SkillRegistry(tuple(artifacts))
    return RegisteredOperations(
        ToolRegistry(definitions),
        skills,
        lambda entities: None,
        observed_mutation_operations=frozenset(d.name for d in definitions),
    )


class ContactsTools:
    def __init__(self, reads, writes):
        self.reads, self.writes = reads, writes

    def tool_schemas(self):
        write_names = {s["function"]["name"] for s in self.writes.tool_schemas()} | {"create_contact"}
        return (
            *[s for s in self.reads.tool_schemas() if s["function"]["name"] not in write_names],
            *self.writes.tool_schemas(),
        )

    def tool_effects(self):
        write_names = {s["function"]["name"] for s in self.writes.tool_schemas()} | {"create_contact"}
        return {
            s["function"]["name"]: ("write" if s["function"]["name"] in write_names else "read")
            for s in self.tool_schemas()
        }

    def execute(self, name, args, *, context=None):
        return self.reads.execute(name, args, context=context)

    def prepare_mutation(self, name, args, *, context=None):
        return self.writes.prepare_mutation(name, args, context=context)

    def execute_mutation(self, name, args, *, context=None):
        return self.writes.execute_mutation(name, args, context=context)
