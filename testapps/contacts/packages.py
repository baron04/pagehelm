"""Standard Skill/operation proof over the synthetic application's existing REST API."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from web_operation_agent.business.compiled_writes import (
    CompiledAggregateUpdateSpec,
    CompiledChildCollectionSpec,
    CompiledCreateSpec,
    CompiledFieldDefault,
    CompiledRelationSpec,
    compiled_write_tool_definitions,
)
from web_operation_agent.business.convention_writes import (
    ConventionFieldSpec,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
    WriteRouteCapability,
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

_NOTE_CREATE_SPEC = CompiledCreateSpec(
    entity="contact-notes",
    label="Contact note",
    fields=(
        ConventionFieldSpec("title", "string", label="Title", required=True, max_length=120),
        ConventionFieldSpec("owner", "string", label="Owner", required=True, max_length=80),
        ConventionFieldSpec("category", "string", label="Category", required=True, max_length=40),
        ConventionFieldSpec("priority", "string", label="Priority", required=False, enum_type="priority"),
        ConventionFieldSpec("body", "string", label="Body", required=False, max_length=1000),
    ),
    defaults=(
        CompiledFieldDefault("owner", "actor", display="当前登录账号"),
        CompiledFieldDefault("category", "literal", value="general", display="general"),
        # D3 sentinel placeholder: integer 0 on an enum field must NOT auto-fill silently.
        CompiledFieldDefault("priority", "literal", value=0),
    ),
    name_fields=("title",),
)

_DOSSIER_AGG_SPEC = CompiledAggregateUpdateSpec(
    entity="contact-dossiers",
    label="Contact dossier",
    fields=(
        ConventionFieldSpec("name", "string", label="Name", required=True, max_length=120),
        ConventionFieldSpec("owner", "string", label="Owner", required=True, max_length=80),
        ConventionFieldSpec("note", "string", label="Note", required=False, max_length=500),
    ),
    collections=(
        CompiledChildCollectionSpec(
            name="items",
            label="Dossier items",
            fields=(
                ConventionFieldSpec("label", "string", label="Label", required=True, max_length=80),
                ConventionFieldSpec("value", "string", label="Value", required=True, max_length=200),
            ),
            name_fields=("label", "value"),
        ),
    ),
)

_CONTACT_GROUP_RELATION = CompiledRelationSpec(
    entity="contacts",
    label="Contact",
    relation="groups",
    relation_label="Contact groups",
    target_entity="contact-groups",
    action_path="/api/contact-groups/{id}",
    read_path="/api/contact-groups/{id}",
    target_param="id",
    items_param="groups",
    param_mode="body",
    http_method="PUT",
    aliases=("group", "群组", "分组"),
)

_AVAILABLE_CONTACT_GROUPS = ("Engineering", "VIP", "Sales")


class ContactsConventionWritePort:
    """Second-adapter proof of neutral ConventionWritePort + CompiledWritePort over MinimalContacts REST."""

    def __init__(self, executor, identity):
        self.executor = executor
        self.identity = identity

    def update_specs(self) -> dict[str, ConventionUpdateSpec]:
        return {"contacts": _CONTACTS_UPDATE_SPEC}

    def status_specs(self) -> dict[str, TwoStateStatusSpec]:
        return {"contact-subscriptions": _SUBSCRIPTION_STATUS_SPEC}

    def create_specs(self) -> dict[str, CompiledCreateSpec]:
        return {"contact-notes": _NOTE_CREATE_SPEC}

    def aggregate_update_specs(self) -> dict[str, CompiledAggregateUpdateSpec]:
        return {"contact-dossiers": _DOSSIER_AGG_SPEC}

    def relation_specs(self) -> dict[str, tuple[CompiledRelationSpec, ...]]:
        return {"contacts": (_CONTACT_GROUP_RELATION,)}

    @staticmethod
    def excluded_create_reason(entity: str) -> str:
        if entity == "accounts":
            return "账号新建包含必填密码校验字段，禁止通过通用 API 代填"
        return ""

    def _get_json(self, sub_path: str) -> dict | None:
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
        return payload if isinstance(payload, dict) else None

    def _request_once(self, method: str, sub_path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.executor.origin + sub_path,
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {self.executor._api_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status not in (200, 201):
                    raise BusinessMutationIndeterminate("unexpected status", phase="dispatch")
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise BusinessToolPermissionError("application denied write") from exc
            raise BusinessMutationIndeterminate(f"HTTP error during {method}", phase="dispatch") from exc
        except (OSError, ValueError) as exc:
            raise BusinessMutationIndeterminate(f"transport error during {method}", phase="dispatch") from exc

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
        if entity == "contact-dossiers":
            data = self._get_json("/api/contact-dossiers") or {}
            rows = list(data.get("dossiers") or ())
        else:
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
                "target is missing or ambiguous; supply exact name",
                entity=entity,
                choices=choices,
            )
        record = self.read_record(entity, exact[0]["id"])
        if not isinstance(record, dict):
            raise BusinessToolRequestError("target record is not readable")
        return record

    def read_record(self, entity: str, record_id) -> dict | None:
        if entity == "contact-notes":
            payload = self._get_json(f"/api/contact-notes/{record_id}")
            return payload.get("note") if isinstance(payload, dict) else None
        if entity == "contact-dossiers":
            payload = self._get_json(f"/api/contact-dossiers/{record_id}")
            return payload.get("dossier") if isinstance(payload, dict) else None
        sub_path = (
            f"/api/contacts/{record_id}"
            if entity == "contacts"
            else f"/api/contact-subscriptions/{record_id}"
        )
        payload = self._get_json(sub_path)
        key = "contact" if entity == "contacts" else "subscription"
        return payload.get(key) if isinstance(payload, dict) else None

    def encode_field(self, spec, field: ConventionFieldSpec, raw_value, current_record: dict):
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

    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None:
        self._request_once("PUT", f"/api/contacts/{record_id}", payload)

    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None:
        self._request_once("PUT", f"/api/contact-subscriptions/{record_id}", {"id": str(record_id), "status": status_code})

    def dispatch_create(self, spec: CompiledCreateSpec, payload: dict) -> str:
        res = self._request_once("POST", f"/api/{spec.entity}", payload)
        return str((res.get("note") or {}).get("id") or "")

    def dispatch_aggregate_update(self, spec: CompiledAggregateUpdateSpec, record_id, payload: dict, *, current_record: dict):
        return self._request_once("PUT", f"/api/contact-dossiers/{record_id}", payload)

    def resolve_relation_items(self, spec: CompiledRelationSpec, items: list[str]) -> tuple[list, list[str], list[dict]]:
        resolved: list[str] = []
        for item in items:
            tok = item.strip()
            matches = [g for g in _AVAILABLE_CONTACT_GROUPS if g.casefold() == tok.casefold()]
            if len(matches) != 1:
                raise BusinessToolRequestError(f"unknown contact group: {item!r}")
            if matches[0] not in resolved:
                resolved.append(matches[0])
        return resolved, list(resolved), []

    def read_relation_ids(self, spec: CompiledRelationSpec, target_id) -> list:
        payload = self._get_json(f"/api/contact-groups/{target_id}") or {}
        return list(payload.get("groups") or ())

    def relation_labels_for_ids(self, spec: CompiledRelationSpec, ids: list) -> list[str]:
        return [str(x) for x in ids]

    def dispatch_relation(self, spec: CompiledRelationSpec, target_id, item_ids: list) -> None:
        self._request_once("PUT", f"/api/contact-groups/{target_id}", {"groups": list(item_ids)})

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
                    b"description: Create notes, update flat or nested contact dossiers, toggle subscriptions, and assign contact groups.\n---\n"
                    b"# Convention Maintenance\nUse create_record, update_record, toggle_record_status, and assign_record_relations.\n"
                ),
                "agents/woa.toml": (
                    b'schema_version = 1\n'
                    b'scope_entities = ["contact-subscriptions"]\n'
                    b'write_operations = ["create_record", "update_record", "toggle_record_status", "assign_record_relations"]\n'
                ),
            },
        )
        artifacts.append(conv_skill)
        if enabled:
            port = ContactsConventionWritePort(executor, identity)
            definitions = (*definitions, *compiled_write_tool_definitions(port, scope="contacts:write"))
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
