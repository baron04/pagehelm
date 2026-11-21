"""Declarative WritePipeline-protected mutations, convention updates, and row actions."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Callable

from web_operation_agent.business.convention_writes import ConventionFieldSpec, ConventionUpdateSpec, TwoStateStatusSpec, convention_tool_definitions
from web_operation_agent.business.effects import EffectProjection, RequestedEffect, canonical
from web_operation_agent.business.errors import BusinessMutationIndeterminate, BusinessToolError, BusinessToolIdentityError, BusinessToolPermissionError, BusinessToolReferenceError, BusinessToolRequestError
from web_operation_agent.business.tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.business.write_pipeline import DEFAULT_SECRET_FIELD_PATTERN, ProposalDiff, create_detail, execute_verified_handler_write
from web_operation_agent.composition.business_operations import RegisteredOperations
from web_operation_agent.composition.ports import MutationReconciliation
from web_operation_agent.execution.submit_contract import ActionRequest, SubmitConventions
from web_operation_agent.skills import SkillArtifact, SkillRegistry
from web_operation_agent.state.identity import PrincipalError
from .openapi import EntitySpec


@dataclass(frozen=True, slots=True)
class TypedWriteSpec:
    """Declarative two-phase verified write specification."""

    name: str
    description: str
    parameters: dict[str, Any]
    entity: str
    operation_action: str
    required_scope: str
    normalizer: Callable[[dict[str, Any]], dict[str, Any]]
    effect_summary: str
    targets_fn: Callable[[dict[str, Any]], tuple[str, ...]]
    reconcile_key_fn: Callable[[dict[str, Any]], str]
    dispatch_fn: Callable[[Any, dict[str, Any]], str]
    readback_fn: Callable[[Any, dict[str, Any], str], dict[str, Any]]
    precondition_check: Callable[[Any, dict[str, Any]], None] | None = None
    result_builder: Callable[[dict[str, Any], str, str], BusinessToolResult] | None = None
    reconcile_fn: Callable[[Any, dict[str, Any]], str] | None = None
    literal_inputs: tuple[str, ...] = ()
    input_fields: tuple[tuple[str, str, str], ...] = ()
    postcondition_paths: tuple[str, ...] = ("content.readback_verified", "audit.readback_verified")
    data_classes: tuple[str, ...] = ("business.identifier", "application.route", "business.mutation_result")
    record_label: str = ""
    reference_field: str = "name"
    use_verification_interval: bool = True


class DeclarativeTypedWriteHandler:
    """Two-phase prepare/dispatch handler executing through ``WritePipeline``."""

    def __init__(
        self,
        spec: TypedWriteSpec,
        executor: Any,
        *,
        application_id: str,
        identity_port: Any = None,
        require_context: bool = True,
    ):
        self.spec = spec
        self.executor = executor
        self.application_id = application_id
        self.identity_port = identity_port if identity_port is not None else getattr(executor, "identity_port", None)
        self.require_context = bool(require_context)

    def project_inputs(self, args: dict[str, Any]) -> RequestedEffect:
        return RequestedEffect(self.spec.entity, self.spec.operation_action, values_json=canonical(args))

    def prepare_spec(self, args: dict[str, Any], *, context=None) -> BusinessMutationSpec:
        proposed = self.spec.normalizer(args)
        if self.spec.precondition_check is not None:
            self.spec.precondition_check(self.executor, proposed)
        targets = self.spec.targets_fn(proposed)
        diff = ProposalDiff.from_details(targets, tuple(create_detail(k, str(v)) for k, v in proposed.items()))
        return BusinessMutationSpec(
            args=proposed, reconcile_key=self.spec.reconcile_key_fn(proposed),
            effect=self.spec.effect_summary, targets=targets, details=diff.details(),
            effect_projection=EffectProjection(self.project_inputs(proposed), required_receipts=("record",)),
        )

    def prepare(self, args: dict[str, Any], *, context=None) -> BusinessMutationSpec:
        if hasattr(self.executor, "prepare_mutation"):
            return self.executor.prepare_mutation(self.spec.name, args, context=context)
        return self.prepare_spec(args, context=context)

    def execute_verified(self, args: dict[str, Any], *, context=None) -> BusinessToolResult:
        proposed = self.spec.normalizer(args)
        reconcile_key = self.spec.reconcile_key_fn(proposed)

        def verify_actor():
            identity = self.identity_port if self.identity_port is not None else getattr(self.executor, "identity_port", None)
            if identity is not None and (context is not None or self.require_context):
                return identity.verify_actor_for_write(context, required_scope=self.spec.required_scope)
            if context is not None:
                context.require_live(application_id=self.application_id, required_scope=self.spec.required_scope)
                return context.principal.actor_id
            if self.require_context:
                raise BusinessToolIdentityError("writes require an actor-bound context")
            if identity is not None:
                delegated = identity.resolve_delegated()
                if delegated is None:
                    raise BusinessToolIdentityError("writes require an actor-bound context")
                assertion, perms = delegated
                if self.spec.required_scope and self.spec.required_scope not in perms:
                    raise BusinessToolPermissionError(
                        f"actor lacks {self.spec.required_scope} permission", layer="application"
                    )
                return assertion.actor_id
            raise BusinessToolIdentityError("writes require an actor-bound context")

        def pre_dispatch_reread(_actor):
            if self.spec.precondition_check is not None:
                self.spec.precondition_check(self.executor, proposed)
            return proposed

        def dispatch_once(_actor, _reread_state):
            record_id = self.spec.dispatch_fn(self.executor, proposed)
            if not record_id:
                raise BusinessMutationIndeterminate("mutation accepted but returned no record identifier", phase="acknowledgement")
            return str(record_id)

        def verify_readback(_actor, _reread_state, record_id, _uncertain):
            record = self.spec.readback_fn(self.executor, proposed, record_id)
            scope = self.executor._scope(context) if hasattr(self.executor, "_scope") else ""
            if self.spec.result_builder is not None:
                return self.spec.result_builder(record, record_id, scope)
            return BusinessToolResult(
                content={
                    "created": True, "readback_verified": True, "verified": True,
                    "entity": self.spec.record_label or self.spec.entity,
                    "reference": record.get(self.spec.reference_field, record_id),
                    "record_id": str(record_id), "record": dict(record), "scope": scope,
                    "effects": [{"effect": "record", "verified": True, "id": str(record_id)}],
                },
                audit={"application": self.application_id, "operation": self.spec.name, "records_created": 1, "readback_verified": True},
            )

        actor_tag = context.principal.actor_id if context is not None else "direct"
        inflight_key = f"{self.application_id}:{self.spec.name}:{actor_tag}:{reconcile_key}"
        return execute_verified_handler_write(
            verify_actor=verify_actor,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            allow_readback_recovery=False,
            use_verification_interval=self.spec.use_verification_interval,
            inflight_key=inflight_key,
        )

    def dispatch(self, args: dict[str, Any], *, context=None) -> BusinessToolResult:
        if hasattr(self.executor, "execute_mutation"):
            return self.executor.execute_mutation(self.spec.name, args, context=context)
        return self.execute_verified(args, context=context)


@dataclass(frozen=True, slots=True)
class StatusToggleSpec:
    """Declarative two-state status toggle configuration."""

    entity: str
    label: str
    states: tuple[tuple[str, int | str | bool], tuple[str, int | str | bool]]
    detail_endpoint_template: str
    update_endpoint_template: str
    detail_key: str = ""
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    status_field: str = "status"
    volatile_fields: frozenset[str] = frozenset()
    synonyms: tuple[tuple[str, str], ...] = ()
    resolve_from_entity: str = ""


class DeclarativeConventionWritePort:
    """Declarative ``ConventionWritePort`` derived from ``EntitySpec`` and ``StatusToggleSpec``."""

    def __init__(
        self, *, application_id: str, entities: tuple[EntitySpec, ...],
        status_toggles: tuple[StatusToggleSpec, ...] = (), read_executor: Any,
        identity_port: Any, write_scope: str,
    ):
        self.application_id, self.executor, self.identity, self.write_scope = (
            application_id, read_executor, identity_port, write_scope,
        )
        self._entities = {e.name: e for e in entities if e.convention_update}
        self._all_entities = {e.name: e for e in entities}
        self._toggles = {t.entity: t for t in status_toggles}
        self._field_normalizers = {
            (e.name, f.name): f.normalizer
            for e in self._entities.values()
            for f in e.fields if f.normalizer is not None
        }
        self._update_specs = {
            e.name: ConventionUpdateSpec(
                entity=e.name, label=e.singular_label or e.label, id_field=e.id_field,
                name_fields=(e.name_field,), volatile_fields=e.volatile_fields,
                fields=tuple(
                    ConventionFieldSpec(
                        name=f.name,
                        kind=("string" if f.kind in {"text", "choice", "date", "datetime"} else "number" if f.kind == "monetary" else f.kind),
                        label=f.label, required=f.required, max_length=f.max_length, min_length=f.min_length,
                        minimum=f.minimum, maximum=f.maximum, unit_scale=f.unit_scale,
                    )
                    for f in e.fields if f.updateable and not f.is_secret and not DEFAULT_SECRET_FIELD_PATTERN.search(f.name)
                ),
            )
            for e in self._entities.values()
        }
        self._status_specs = {
            t.entity: TwoStateStatusSpec(
                entity=t.entity, label=t.label, states=t.states, id_field=t.id_field,
                name_fields=t.name_fields, status_field=t.status_field, volatile_fields=t.volatile_fields, synonyms=t.synonyms,
            )
            for t in self._toggles.values()
        }

    def update_specs(self) -> dict[str, ConventionUpdateSpec]:
        return dict(self._update_specs)

    def status_specs(self) -> dict[str, TwoStateStatusSpec]:
        return dict(self._status_specs)

    def verify_actor(self, context) -> str:
        return self.identity.verify_actor_for_write(context, required_scope=self.write_scope)

    def resolve_target(self, entity: str, target: str, *, spec) -> dict:
        source_name = (
            self._toggles[entity].resolve_from_entity
            if entity in self._toggles and self._toggles[entity].resolve_from_entity
            else entity
        )
        src_spec = self._all_entities.get(source_name)
        id_k = src_spec.id_field if src_spec is not None else spec.id_field
        name_k = src_spec.name_field if src_spec is not None else spec.name_fields[0]
        rows = self.executor.fetch_rows(source_name)
        wanted = target.strip()
        qualified = re.fullmatch(r"(.+)（ID=([^()（）]+)）", wanted)
        if qualified:
            exact = [r for r in rows if str(r.get(name_k, "")) == qualified.group(1) and str(r.get(id_k, "")) == qualified.group(2)]
        else:
            exact = [r for r in rows if str(r.get(name_k, "")).casefold() == wanted.casefold() or str(r.get(id_k, "")) == wanted]
        if len(exact) != 1:
            choices = tuple(f"{r.get(name_k, '')}（ID={r.get(id_k, '')}）" for r in rows)[:6]
            raise BusinessToolReferenceError(f"{entity} target is missing or ambiguous; supply exact name", entity=entity, choices=choices)
        record = self.read_record(entity, exact[0][id_k])
        if not isinstance(record, dict):
            raise BusinessToolRequestError("target record is not readable")
        return record

    def read_record(self, entity: str, record_id) -> dict | None:
        if entity in self._toggles:
            t = self._toggles[entity]
            return self.executor.client.get_record(t.detail_endpoint_template.format(id=record_id), detail_key=t.detail_key)
        espec = self._all_entities.get(entity)
        if espec is None:
            return None
        if espec.record_reader is not None:
            return espec.record_reader(self.executor, record_id)
        return self.executor.client.get_record(espec.detail_endpoint_template.format(id=record_id), detail_key=espec.detail_key) if espec.detail_endpoint_template else None

    def encode_field(self, spec: ConventionUpdateSpec, field: ConventionFieldSpec, raw_value, current_record: dict):
        old_raw = current_record.get(field.name)
        old_val = "" if old_raw is None else str(old_raw)
        if field.kind == "string":
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise BusinessToolRequestError(f"{field.name} must be a non-empty string")
            val: Any = raw_value.strip()
            if field.min_length is not None and len(val) < field.min_length:
                raise BusinessToolRequestError(f"{field.name} is shorter than min length {field.min_length}")
            if field.max_length is not None and len(val) > field.max_length:
                raise BusinessToolRequestError(f"{field.name} exceeds max length {field.max_length}")
        elif field.kind == "integer":
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise BusinessToolRequestError(f"{field.name} must be an integer")
            val = raw_value
            if (field.minimum is not None and val < field.minimum) or (field.maximum is not None and val > field.maximum):
                raise BusinessToolRequestError(f"{field.name} is outside allowed bounds")
        elif field.kind == "number":
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)) or not math.isfinite(float(raw_value)):
                raise BusinessToolRequestError(f"{field.name} must be a finite number")
            val = raw_value
            if (field.minimum is not None and val < field.minimum) or (field.maximum is not None and val > field.maximum):
                raise BusinessToolRequestError(f"{field.name} is outside allowed bounds")
        elif field.kind == "boolean":
            if not isinstance(raw_value, bool):
                raise BusinessToolRequestError(f"{field.name} must be a boolean")
            val = raw_value
        else:
            val = raw_value
        custom = self._field_normalizers.get((spec.entity, field.name))
        if custom is not None:
            val = custom(val)
        return val, old_val, str(val), None

    def recheck_bindings(self, bindings: list[dict], *, actor_id: str) -> None:
        return None

    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None:
        espec = self._entities[spec.entity]
        if espec.update_dispatcher is not None:
            espec.update_dispatcher(self.executor, record_id, payload)
            return
        self.executor.client.put_once(espec.update_endpoint_template.format(id=record_id), payload)

    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None:
        t = self._toggles[spec.entity]
        self.executor.client.put_once(t.update_endpoint_template.format(id=record_id), {t.id_field: record_id, t.status_field: status_code})

    def invalidate_reads(self, entities: frozenset[str]) -> None:
        return None


def admit_armed_write(armed, method: str, path: str, query: str, body: bytes) -> dict:
    """Gate verdict for one armed native browser write inside its short-lived window."""
    if armed is None:
        return {"allowed": False, "code": "not_armed"}
    decision = armed.admit(method, path, query, body)
    if not decision.allowed:
        return {"allowed": False, "code": decision.code}
    return {"allowed": True, "code": "", "settle": lambda status, resp_body: armed.settle(decision.sequence, status, resp_body)}


@dataclass(frozen=True, slots=True)
class SubmitSpec:
    """Declarative native row-action ``SubmitPort`` specification."""

    prefix: str
    collection_key: str
    conventions: SubmitConventions
    browser_prefix: str = ""
    row_action_method: str = "POST"
    row_action_template: str = ""
    delete_template: str = ""
    id_field: str = "id"
    danger_pattern: str = r"删除|移除|清除|作废|delete|remove|trash|clear"


class DeclarativeSubmitPort:
    """Declarative row-action ``SubmitPort`` (page forms always use prefill handover)."""

    allow_form_submit = False

    def __init__(self, spec: SubmitSpec, client: Any, journal=(), reads=None):
        self.spec, self.client, self._journal, self._reads_source = spec, client, journal, reads
        self.armed, self._reads = None, ()
        self._danger = re.compile(spec.danger_pattern, re.IGNORECASE)

    def contract(self) -> SubmitConventions:
        return self.spec.conventions

    def recent_reads(self) -> tuple:
        return (*self._reads, *(self._reads_source or ()))

    def note_read(self, path: str, query: str = "") -> None:
        self._reads = (*self._reads, (path, query))

    def prefixes(self, reads: tuple, *, mode: str, record_id: str, source: str = "") -> tuple[str, ...]:
        return ()

    def row_prefixes(self, reads: tuple, row: dict) -> tuple[str, ...]:
        source = row.get("source")
        return (self.spec.prefix,) if source == self.spec.prefix and any(p == source for p, _ in reads) else ()

    def row_request(self, prefixes: tuple[str, ...], row: dict, element, record: dict | None):
        template = self.spec.row_action_template or self.spec.delete_template
        record_id = str((record or {}).get(self.spec.id_field) or row.get("record_id") or "")
        if not record_id or self.spec.prefix not in prefixes or not template:
            return None
        return ActionRequest(self.spec.row_action_method.upper(), template.format(id=record_id), {})

    def catalog(self, prefixes: tuple[str, ...]) -> frozenset:
        template = self.spec.row_action_template or self.spec.delete_template
        if self.spec.prefix not in prefixes or not template:
            return frozenset()
        method = self.spec.row_action_method.upper()
        return frozenset(
            (method, template.format(id=item[self.spec.id_field]))
            for item in self._records()
            if item.get(self.spec.id_field) not in (None, "")
        )

    def _records(self):
        data = self.client.get(self.spec.prefix)
        return (data.get(self.spec.collection_key) or ()) if isinstance(data, dict) else ()

    def read_record(self, prefixes: tuple[str, ...], record_id: str):
        if not record_id or self.spec.prefix not in prefixes:
            return None
        return next((item for item in self._records() if str(item.get(self.spec.id_field)) == str(record_id)), None)

    def dangerous(self, element) -> bool:
        labels = " ".join(str(v) for v in (element.name, element.attrs.get("title"), element.attrs.get("aria-label")) if v)
        return bool(self._danger.search(labels) or not labels.strip())

    def arm(self, permit) -> None:
        self.armed = permit

    def disarm(self) -> None:
        self.armed = None

    def settled(self) -> None:
        pass

    def user_write_mark(self):
        return len(self._journal)

    def user_write_event(self, prefixes: tuple[str, ...], record_id: str, mark, create: bool = False):
        for seq, entry in enumerate(self._journal):
            if seq < mark:
                continue
            path, method = str(entry.get("path") or ""), str(entry.get("method") or "").upper()
            if not any(path.startswith(prefix) for prefix in prefixes):
                continue
            wanted, created = str(entry.get("record_id") or ""), str(entry.get("created_id") or "")
            if record_id and record_id not in {wanted, created}:
                continue
            if create and (method, path.split("?", 1)[0]) != ("POST", self.spec.prefix):
                continue
            return {"seq": seq, "method": method, "path": path, "record_id": created or wanted or record_id}
        return None


class DeclarativeReconciler:
    """Conservative ``ApplicationMutationReconciler`` for uncertain writes."""

    def __init__(self, *, application_id: str, read_scope: str, executor: Any, supported_tools: frozenset[str], write_specs: dict[str, TypedWriteSpec] | None = None):
        self.application_id, self.read_scope, self._executor = application_id, read_scope, executor
        self.supported_tools, self._write_specs = frozenset(supported_tools), dict(write_specs or {})

    def reconcile(self, name: str, business_key: dict, *, evidence: dict, principal) -> MutationReconciliation:
        if name not in self.supported_tools or principal is None:
            raise ValueError("reconciliation requires an enabled tool and principal")
        principal.require_live(application_id=self.application_id)
        if self.read_scope and self.read_scope not in principal.scopes:
            raise PrincipalError("reconciliation requires the read scope")
        spec = self._write_specs.get(name)
        if spec is not None and spec.reconcile_fn is not None:
            return MutationReconciliation(outcome="unknown", requires_human=True, reason=spec.reconcile_fn(self._executor, business_key))
        return MutationReconciliation(outcome="unknown", requires_human=True, reason=f"Indeterminate {name} outcome; inspect workspace records under the acting user.")


def build_declarative_operations(
    *, namespace: str, application_id: str, skills_dir: Path | None, executor: Any, enabled: bool,
    write_specs: tuple[TypedWriteSpec, ...] = (), entities: tuple[EntitySpec, ...] = (),
    status_toggles: tuple[StatusToggleSpec, ...] = (), convention_updates: bool = False,
    identity_port: Any = None, write_scope: str = "",
) -> RegisteredOperations:
    """Build ``RegisteredOperations`` from Skills, typed writes, and convention updates."""
    artifacts = list(SkillRegistry.discover(skills_dir, namespace=namespace).artifacts) if skills_dir is not None and skills_dir.is_dir() else []
    definitions: list[ToolDefinition] = []
    if enabled:
        for wspec in write_specs:
            handler = DeclarativeTypedWriteHandler(wspec, executor, application_id=application_id, identity_port=identity_port, require_context=True)
            definitions.append(ToolDefinition(
                wspec.name, wspec.description, json.dumps(wspec.parameters), "write", wspec.required_scope,
                ToolOperation(wspec.entity, wspec.operation_action), handler,
                literal_inputs=wspec.literal_inputs, input_fields=wspec.input_fields, effect_contract=handler,
            ))
    if convention_updates and identity_port is not None:
        conv_entities = list(dict.fromkeys([*(e.name for e in entities if e.convention_update), *(t.entity for t in status_toggles)]))
        artifacts.append(SkillArtifact.from_files(namespace, {
            "SKILL.md": (
                b"---\nname: record-convention-maintenance\n"
                b"description: Update flat record fields and toggle two-state statuses.\n---\n"
                b"# Convention Maintenance\nUse update_record for flat updates and toggle_record_status for two-state status toggles.\n"
            ),
            "agents/woa.toml": f'schema_version = 1\nscope_entities = {json.dumps(conv_entities)}\nwrite_operations = ["update_record", "toggle_record_status"]\n'.encode("utf-8"),
        }))
        if enabled:
            port = DeclarativeConventionWritePort(
                application_id=application_id, entities=entities, status_toggles=status_toggles,
                read_executor=executor, identity_port=identity_port, write_scope=write_scope,
            )
            definitions.extend(convention_tool_definitions(port, scope=write_scope))
    return RegisteredOperations(ToolRegistry(tuple(definitions)), SkillRegistry(tuple(artifacts)), lambda entities: None, observed_mutation_operations=frozenset(d.name for d in definitions))


__all__ = [
    "DeclarativeConventionWritePort", "DeclarativeReconciler", "DeclarativeSubmitPort",
    "DeclarativeTypedWriteHandler", "StatusToggleSpec", "SubmitSpec", "TypedWriteSpec",
    "admit_armed_write", "build_declarative_operations",
]
