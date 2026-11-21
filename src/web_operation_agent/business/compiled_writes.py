"""Application-neutral contract for four-category compiled API writes.

Extends the symmetric flat convention-write layer with:
1. Compiled record creation (``create_record``) with D1/D2 explicit approval-card page defaults
   (annotated with ``（页面默认值）``) and D3 placeholder sentinel refusal.
2. Unified single-record & 1–2 level aggregate-root updates (``update_record``) merging
   untouched parent fields and untouched nested child lines verbatim before single dispatch
   and same-actor readback verification.
3. Reversible status toggles (``toggle_record_status``) across parameterized endpoints.
4. Idempotent relation set assignments (``assign_record_relations``) over same-actor readback.

Zero application names, URL conventions, or UI framework strings live here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import re
from typing import Any, Callable, Literal, Protocol

from ..execution.submit_contract import scalar_text
from ..execution.value_source import Entry
from ..goal.skill_literals import literal_supplied
from .convention_writes import (
    ConventionFieldSpec,
    ConventionStatusHandler,
    ConventionUpdateHandler,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
    collect_provenance_context,
    normalize_create_arguments,
    normalize_update_arguments,
    resolve_write_route_capability,
    validate_single_field_provenance,
)
from .effects import EffectProjection, RequestedEffect, canonical
from .errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolIdentityError,
    BusinessToolReferenceError,
    BusinessToolRequestError,
)
from .tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from .tools.registry import ToolDefinition
from .write_pipeline import (
    DEFAULT_SECRET_FIELD_PATTERN,
    ProposalDiff,
    change_detail,
    create_detail,
    derived_detail,
    execute_verified_handler_write,
    non_volatile_snapshot,
    preserved_detail,
    restricted_snapshot_digests,
    verify_preservation_snapshot,
)
from ..execution.submit_contract import same_value

PAGE_DEFAULT_ANNOTATION = "（页面默认值）"
DefaultValueKind = Literal["actor", "literal"]
_SECRET_FIELD = DEFAULT_SECRET_FIELD_PATTERN
_INDEX_MATCH = re.compile(r"^(?:#|第\s*)(\d+)(?:\s*[行条项])?$")
_CANDIDATE_ID_SUFFIX = re.compile(r"^(.+?)\s*[（(](?:ID\s*=|编号\s*|#)\s*([^）)]+)\s*[）)]$")


def is_rejected_ui_sentinel(
    value: object,
    *,
    is_fk_or_enum: bool = False,
    is_hierarchy_parent: bool = False,
) -> bool:
    """Return True when a UI/schema initial value is a sentinel that must never fill silently."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str):
        cleaned = value.strip().casefold()
        if cleaned in {"", "undefined", "null", "nan"}:
            return True
        if (is_fk_or_enum or is_hierarchy_parent) and cleaned in {"0", "-1"}:
            return True
        return False
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        if (is_fk_or_enum or is_hierarchy_parent) and value in (0, -1):
            return True
    return False


def _resolve_field_from(fields: tuple[ConventionFieldSpec, ...], token: str) -> ConventionFieldSpec | None:
    cleaned = token.strip().rstrip(":：").strip()
    for f in fields:
        if f.name == cleaned:
            return f
    folded = cleaned.casefold()
    by_case = [f for f in fields if f.name.casefold() == folded]
    if len(by_case) == 1:
        return by_case[0]
    by_label = [f for f in fields if f.label and f.label.strip().rstrip(":：").strip() == cleaned]
    if len(by_label) == 1:
        return by_label[0]
    return None


def _record_display_name(record: dict, name_fields: tuple[str, ...], id_field: str) -> str:
    for key in name_fields:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return str(record.get(id_field, ""))


def _values_equal(left, right, *, kind: str = "") -> bool:
    if kind == "id_array":
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and [str(x) for x in left] == [str(x) for x in right]
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return same_value(left, right)


def _violates_optional_bounds(f_spec: ConventionFieldSpec, val: object) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        if f_spec.minimum is not None and val < f_spec.minimum:
            return True
        if f_spec.maximum is not None and val > f_spec.maximum:
            return True
    if isinstance(val, str):
        if f_spec.min_length is not None and len(val) < f_spec.min_length:
            return True
        if f_spec.max_length is not None and len(val) > f_spec.max_length:
            return True
    return False


def _check_explicit_bounds(f_spec: ConventionFieldSpec, api_val: object) -> None:
    label = f_spec.label or f_spec.name
    if isinstance(api_val, (int, float)) and not isinstance(api_val, bool):
        if f_spec.minimum is not None and api_val < f_spec.minimum:
            raise BusinessToolRequestError(f"字段「{label}」不能小于 {f_spec.minimum}。")
        if f_spec.maximum is not None and api_val > f_spec.maximum:
            raise BusinessToolRequestError(f"字段「{label}」不能大于 {f_spec.maximum}。")
    if isinstance(api_val, str):
        if f_spec.min_length is not None and len(api_val) < f_spec.min_length:
            raise BusinessToolRequestError(f"字段「{label}」长度不能少于 {f_spec.min_length}。")
        if f_spec.max_length is not None and len(api_val) > f_spec.max_length:
            raise BusinessToolRequestError(f"字段「{label}」长度超过上限 {f_spec.max_length}。")


def _effect_value(val: object) -> object:
    """Coerce an arbitrary input value into RequestedEffect's strict scalar/ref-list domain."""
    if val is None or type(val) in (str, int, float, bool):
        return val
    if isinstance(val, list):
        items: list[str] = []
        seen: set[str] = set()
        for elem in val:
            if not isinstance(elem, str) or not 0 < len(elem) <= 500 or elem in seen:
                return canonical(val)
            seen.add(elem)
            items.append(elem)
        if len(items) <= 80:
            return items
    return canonical(val)


def _strip_secrets_deep(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            k: _strip_secrets_deep(v)
            for k, v in obj.items()
            if isinstance(k, str) and not _SECRET_FIELD.search(k)
        }
    if isinstance(obj, list):
        return [_strip_secrets_deep(item) for item in obj]
    return obj


@dataclass(frozen=True, slots=True)
class CompiledFieldDefault:
    """Explicit create-form default surfaced on the visible approval card."""

    field: str
    kind: DefaultValueKind
    value: object = None
    display: str = ""


@dataclass(frozen=True, slots=True)
class CompiledCreateSpec:
    """Compiled flat creation contract with D1–D3 default rules."""

    entity: str
    label: str
    fields: tuple[ConventionFieldSpec, ...]
    defaults: tuple[CompiledFieldDefault, ...] = ()
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    server_computed_fields: frozenset[str] = frozenset()
    write_only_fields: frozenset[str] = frozenset()
    conditional_required_fn: Callable[[dict], tuple[tuple[str, str], ...]] | None = None

    def __post_init__(self) -> None:
        if not self.entity or not self.fields:
            raise ValueError("compiled create spec requires entity and fields")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate field in compiled create spec: {self.entity}")

    def resolve_field(self, token: str) -> ConventionFieldSpec | None:
        return _resolve_field_from(self.fields, token)

    def default_map(self) -> dict[str, CompiledFieldDefault]:
        return {d.field: d for d in self.defaults}


@dataclass(frozen=True, slots=True)
class CompiledChildCollectionSpec:
    """One 1st- or 2nd-level child line schema within an aggregate-root update."""

    name: str
    label: str
    fields: tuple[ConventionFieldSpec, ...]
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    min_items: int = 0
    required_on_add: tuple[str, ...] = ()
    server_computed_fields: frozenset[str] = frozenset()
    allow_add: bool = True
    allow_remove: bool = True
    subcollections: tuple["CompiledChildCollectionSpec", ...] = ()
    volatile_fields: frozenset[str] = frozenset()

    def resolve_field(self, token: str) -> ConventionFieldSpec | None:
        return _resolve_field_from(self.fields, token)

    def resolve_subcollection(self, token: str) -> "CompiledChildCollectionSpec | None":
        cleaned = token.strip().casefold()
        for sub in self.subcollections:
            if sub.name.casefold() == cleaned or (sub.label and sub.label.strip().casefold() == cleaned):
                return sub
        return None


@dataclass(frozen=True, slots=True)
class CompiledAggregateUpdateSpec:
    """Master-detail aggregate-root update contract supporting 1–2 levels of child lines."""

    entity: str
    label: str
    fields: tuple[ConventionFieldSpec, ...]
    collections: tuple[CompiledChildCollectionSpec, ...]
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    volatile_fields: frozenset[str] = frozenset()
    server_computed_fields: frozenset[str] = frozenset()
    omit_root_id_in_payload: bool = False
    composite_id_fields: tuple[str, ...] = ()
    readback_strategy: Literal["same_record", "change_record_id"] = "same_record"

    def __post_init__(self) -> None:
        if not self.entity or not self.collections:
            raise ValueError("compiled aggregate update spec requires entity and child collections")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate field in compiled aggregate update spec: {self.entity}")

    def resolve_field(self, token: str) -> ConventionFieldSpec | None:
        return _resolve_field_from(self.fields, token)

    def resolve_collection(self, token: str | None) -> CompiledChildCollectionSpec | None:
        if token is None or not str(token).strip():
            return self.collections[0] if len(self.collections) == 1 else None
        cleaned = str(token).strip().casefold()
        for col in self.collections:
            if col.name.casefold() == cleaned or (col.label and col.label.strip().casefold() == cleaned):
                return col
        return None


@dataclass(frozen=True, slots=True)
class CompiledRelationSpec:
    """Idempotent many-to-many / batch relation assignment contract."""

    entity: str
    label: str
    relation: str
    relation_label: str
    target_entity: str
    action_path: str
    read_path: str
    target_param: str
    items_param: str
    param_mode: str = "body"
    http_method: str = "POST"
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name", "nickname", "username")
    target_name_fields: tuple[str, ...] = ("name",)
    aliases: tuple[str, ...] = ()
    single_value: bool = False
    read_field: str = ""
    target_id_param: str = ""

    def __post_init__(self) -> None:
        if self.param_mode not in {"body", "query"} or self.http_method not in {"POST", "PUT"}:
            raise ValueError(f"invalid compiled relation transport: {self.action_path}")

    def matches_relation(self, token: str) -> bool:
        cleaned = token.strip().casefold()
        candidates = {self.relation, self.relation_label, self.items_param, *self.aliases}
        return any(c and c.strip().casefold() == cleaned for c in candidates)


class CompiledWritePort(Protocol):
    """Extended adapter protocol for 4-category compiled writes (backwards-compatible with ConventionWritePort)."""

    def update_specs(self) -> dict[str, ConventionUpdateSpec]: ...
    def status_specs(self) -> dict[str, TwoStateStatusSpec]: ...
    def create_specs(self) -> dict[str, CompiledCreateSpec]: ...
    def aggregate_update_specs(self) -> dict[str, CompiledAggregateUpdateSpec]: ...
    def relation_specs(self) -> dict[str, tuple[CompiledRelationSpec, ...]]: ...
    def verify_actor(self, context) -> str: ...
    def resolve_target(self, entity: str, target: str, *, spec) -> dict: ...
    def read_record(self, entity: str, record_id) -> dict | None: ...
    def encode_field(self, spec, field: ConventionFieldSpec, raw_value, current_record: dict) -> tuple[object, str, str, dict | None]: ...
    def recheck_bindings(self, bindings: list[dict], *, actor_id: str) -> None: ...
    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None: ...
    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None: ...
    def dispatch_create(self, spec: CompiledCreateSpec, payload: dict) -> object: ...
    def dispatch_aggregate_update(self, spec: CompiledAggregateUpdateSpec, record_id, payload: dict, *, current_record: dict) -> object: ...
    def resolve_relation_items(self, spec: CompiledRelationSpec, items: list[str]) -> tuple[list, list[str], list[dict]]: ...
    def read_relation_ids(self, spec: CompiledRelationSpec, target_id) -> list: ...
    def relation_labels_for_ids(self, spec: CompiledRelationSpec, ids: list) -> list[str]: ...
    def dispatch_relation(self, spec: CompiledRelationSpec, target_id, item_ids: list) -> None: ...
    def invalidate_reads(self, entities: frozenset[str]) -> None: ...


def _project_create_values(spec: CompiledCreateSpec | None, raw_fields: list | tuple) -> dict[str, object]:
    mapped: dict[str, object] = {}
    for item in raw_fields or ():
        raw_field = str(item.get("field") or "").strip()
        f_spec = spec.resolve_field(raw_field) if spec is not None else None
        key = (f_spec.name if f_spec is not None else raw_field)[:80]
        if key:
            mapped[key] = _effect_value(item.get("value"))
    return mapped


def _line_change_effect_key(
    col_name: str,
    idx: int,
    match_token: str,
    subcollection: str,
    submatch: str,
    field_name: str,
) -> str:
    base = f"{col_name}[{idx}:{match_token[:14]}]"
    if subcollection:
        base = f"{base}.{subcollection[:14]}[{submatch[:10]}]"
    candidate = f"{base}.{field_name}"
    if len(candidate) <= 80:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
    return f"{candidate[:71]}#{digest}"


def _project_aggregate_values(
    spec: CompiledAggregateUpdateSpec | None,
    raw_changes: list | tuple,
    raw_line_changes: list | tuple,
) -> dict[str, object]:
    mapped: dict[str, object] = {}
    for item in raw_changes or ():
        raw_field = str(item.get("field") or "").strip()
        f_spec = spec.resolve_field(raw_field) if spec is not None else None
        key = (f_spec.name if f_spec is not None else raw_field)[:80]
        if key:
            mapped[key] = _effect_value(item.get("value"))
    for idx, lc in enumerate(raw_line_changes or ()):
        raw_col = lc.get("collection")
        col_spec = spec.resolve_collection(raw_col) if spec is not None else None
        col_name = col_spec.name if col_spec is not None else str(raw_col or "items").strip()
        match_token = str(lc.get("match") or "").strip()
        subcol_raw = str(lc.get("subcollection") or "").strip()
        subcol_spec = col_spec.resolve_subcollection(subcol_raw) if (col_spec and subcol_raw) else None
        subcol_name = subcol_spec.name if subcol_spec is not None else subcol_raw
        submatch = str(lc.get("submatch") or "").strip()
        target_spec = subcol_spec or col_spec
        for item in lc.get("changes") or ():
            raw_field = str(item.get("field") or "").strip()
            f_spec = target_spec.resolve_field(raw_field) if target_spec is not None else None
            fname = f_spec.name if f_spec is not None else raw_field
            ekey = _line_change_effect_key(col_name, idx, match_token, subcol_name, submatch, fname)
            mapped[ekey] = _effect_value(item.get("value"))
    return mapped


class CompiledWriteEffects:
    """EffectContract projecting ``create_record``, ``update_record``, ``toggle_record_status``, and ``assign_record_relations``."""

    def __init__(self, tool_name: str, port: CompiledWritePort):
        self.tool_name = tool_name
        self.port = port

    def project_inputs(self, inputs: dict) -> RequestedEffect:
        entity = str(inputs["entity"]).strip().strip("/")
        if self.tool_name == "create_record":
            create_fn = getattr(self.port, "create_specs", None)
            spec = create_fn().get(entity) if callable(create_fn) else None
            return RequestedEffect(
                entity,
                "create",
                "",
                "",
                canonical(_project_create_values(spec, inputs.get("fields") or ())),
            )
        target = str(inputs["target"]).strip()
        if self.tool_name == "toggle_record_status":
            spec = self.port.status_specs().get(entity)
            resolved = spec.resolve_state(str(inputs["status"])) if spec is not None else None
            label = resolved[0] if resolved is not None else str(inputs["status"]).strip()
            field_name = spec.status_field if spec is not None else "status"
            return RequestedEffect(entity, "state_change", entity, target, canonical({field_name: label}))
        if self.tool_name == "assign_record_relations":
            rel_name = str(inputs["relation"]).strip()
            rel_spec = _find_relation_spec(self.port, entity, rel_name)
            rel_key = (rel_spec.items_param if rel_spec is not None else rel_name)[:80]
            raw_items = [str(x).strip() for x in (inputs.get("items") or ()) if isinstance(x, str) and str(x).strip()]
            return RequestedEffect(
                entity,
                "update",
                entity,
                target,
                canonical({rel_key: _effect_value(raw_items)}),
            )
        # update_record: flat or aggregate
        agg_fn = getattr(self.port, "aggregate_update_specs", None)
        agg_spec = agg_fn().get(entity) if callable(agg_fn) else None
        if agg_spec is not None:
            values = _project_aggregate_values(
                agg_spec,
                inputs.get("changes") or (),
                inputs.get("line_changes") or (),
            )
            return RequestedEffect(entity, "update", entity, target, canonical(values))
        spec = self.port.update_specs().get(entity)
        mapped: dict[str, object] = {}
        for item in inputs.get("changes") or ():
            raw_field = str(item["field"]).strip()
            f_spec = spec.resolve_field(raw_field) if spec is not None else None
            key = f_spec.name if f_spec is not None else raw_field
            mapped[key] = _effect_value(item["value"])
        return RequestedEffect(entity, "update", entity, target, canonical(mapped))


class CompiledCreateHandler:
    """Prepare / provenance-check / single-dispatch / readback handler for ``create_record``."""

    def __init__(self, port: CompiledWritePort):
        self.port = port

    def execute(self, args: dict, *, context=None) -> BusinessToolResult:
        raise BusinessToolRequestError("create_record is a write proposal tool")

    def _spec_for(self, entity: str) -> CompiledCreateSpec:
        create_fn = getattr(self.port, "create_specs", None)
        spec = create_fn().get(entity) if callable(create_fn) else None
        if spec is None:
            cap = resolve_write_route_capability(self.port)
            dedicated_fn = getattr(self.port, "dedicated_tool_for", None)
            dedicated = dedicated_fn(entity, "create") if callable(dedicated_fn) else None
            excluded_fn = getattr(self.port, "excluded_create_reason", None)
            excluded = excluded_fn(entity) if callable(excluded_fn) else ""
            raise BusinessToolRequestError(
                cap.unsupported_create_message(entity, dedicated=dedicated, excluded_reason=excluded)
            )
        return spec

    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec:
        actor_id = self.port.verify_actor(context)
        entity = str(args.get("entity") or "").strip().strip("/")
        spec = self._spec_for(entity)
        cap = resolve_write_route_capability(self.port)
        raw_fields = list(args.get("fields") or ())
        if not raw_fields:
            raise BusinessToolRequestError("fields 不能为空，请提供新建记录所需的字段值。")

        seen_fields: set[str] = set()
        payload: dict[str, object] = {}
        expected_created: dict[str, object] = {}
        bindings: list[dict] = []
        details = []
        default_lookup = spec.default_map()
        indexed_items: list[tuple[int, ConventionFieldSpec, object]] = []

        for idx, item in enumerate(raw_fields):
            raw_field = str(item.get("field") or "").strip()
            f_spec = spec.resolve_field(raw_field)
            if f_spec is None:
                supported = "、".join(
                    f"{f.name}({f.label})" if f.label else f.name for f in spec.fields[:25]
                )
                raise BusinessToolRequestError(
                    f"实体「{entity}」不支持在新建时传入字段「{raw_field}」（支持字段：{supported}）；"
                    f"如需其他表单专属字段，请{cap.form_fallback_phrase()}。"
                )
            if f_spec.name in seen_fields:
                raise BusinessToolRequestError(f"字段「{f_spec.name}」在同一次新建中重复出现。")
            seen_fields.add(f_spec.name)
            raw_val = item.get("value")
            if raw_val is None or raw_val == "":
                if f_spec.required:
                    shown = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
                    raise BusinessToolRequestError(f"新建{spec.label}必填字段「{shown}」不能为空或 null，请向用户确认后再提交。")
                continue
            indexed_items.append((idx, f_spec, raw_val))

        encode_create_fn = getattr(self.port, "encode_create_field", None)
        accepts_resolved = False
        if callable(encode_create_fn):
            try:
                sig = inspect.signature(encode_create_fn)
                accepts_resolved = "resolved_fields" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
            except (ValueError, TypeError):
                accepts_resolved = False

        resolved_fields: dict[str, object] = {}

        def _encode_one(f_spec: ConventionFieldSpec, raw_val: object):
            if callable(encode_create_fn):
                if accepts_resolved:
                    return encode_create_fn(
                        spec,
                        f_spec,
                        raw_val,
                        actor_id=actor_id,
                        resolved_fields=dict(resolved_fields),
                    )
                return encode_create_fn(spec, f_spec, raw_val, actor_id=actor_id)
            api_val, _old, disp, binding = self.port.encode_field(spec, f_spec, raw_val, {})
            return api_val, disp, binding

        resolved_entries: list[tuple[int, ConventionFieldSpec, object, object, dict | None]] = []
        deferred: list[tuple[int, ConventionFieldSpec, object]] = []
        for pos, (idx, f_spec, raw_val) in enumerate(indexed_items):
            try:
                api_val, disp, binding = _encode_one(f_spec, raw_val)
            except (BusinessToolRequestError, BusinessToolReferenceError):
                if f_spec.reference_entity and pos + 1 < len(indexed_items):
                    deferred.append((idx, f_spec, raw_val))
                    continue
                raise
            _check_explicit_bounds(f_spec, api_val)
            resolved_fields[f_spec.name] = api_val
            resolved_entries.append((idx, f_spec, api_val, disp, binding))

        while deferred:
            progress = False
            next_deferred: list[tuple[int, ConventionFieldSpec, object]] = []
            for idx, f_spec, raw_val in deferred:
                try:
                    api_val, disp, binding = _encode_one(f_spec, raw_val)
                except (BusinessToolRequestError, BusinessToolReferenceError):
                    next_deferred.append((idx, f_spec, raw_val))
                    continue
                _check_explicit_bounds(f_spec, api_val)
                resolved_fields[f_spec.name] = api_val
                resolved_entries.append((idx, f_spec, api_val, disp, binding))
                progress = True
            if not progress:
                # Re-run first unresolved item to surface its original typed error cleanly.
                _encode_one(next_deferred[0][1], next_deferred[0][2])
            deferred = next_deferred

        for _idx, f_spec, api_val, disp, binding in sorted(resolved_entries, key=lambda entry: entry[0]):
            payload[f_spec.name] = api_val
            expected_created[f_spec.name] = api_val
            if binding is not None:
                bindings.append(binding)
            shown_label = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
            details.append(create_detail(shown_label, str(disp)))

        # Apply static D1/D2 defaults ONLY for omitted fields and always surface with （页面默认值）
        derived_defaults: dict[str, object] = {}
        for d_spec in spec.defaults:
            if d_spec.field in seen_fields:
                continue
            f_spec = spec.resolve_field(d_spec.field)
            if f_spec is None:
                continue
            if d_spec.kind != "actor" and is_rejected_ui_sentinel(
                d_spec.value,
                is_fk_or_enum=bool(f_spec.reference_entity or f_spec.enum_type),
                is_hierarchy_parent=(f_spec.name == "parentId"),
            ):
                continue
            encode_default_fn = getattr(self.port, "encode_create_default", None)
            if callable(encode_default_fn):
                api_val, disp, binding = encode_default_fn(spec, f_spec, d_spec, actor_id=actor_id)
            elif d_spec.kind == "actor":
                api_val, disp, binding = int(actor_id) if str(actor_id).isdigit() else actor_id, str(d_spec.display or f"当前账号({actor_id})"), None
            else:
                api_val, _old, disp, binding = self.port.encode_field(spec, f_spec, d_spec.value, {})
            if api_val is None or api_val == "":
                continue
            payload[f_spec.name] = api_val
            expected_created[f_spec.name] = api_val
            if binding is not None:
                bindings.append(binding)
            annotated = f"{disp}{PAGE_DEFAULT_ANNOTATION}"
            shown_label = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
            derived_defaults[f_spec.name] = annotated
            details.append(create_detail(shown_label, annotated))

        # Fail closed when required fields (or conditional required form rules) remain missing
        missing: list[str] = []
        for f_spec in spec.fields:
            if not f_spec.required or f_spec.name in payload or f_spec.name in spec.server_computed_fields:
                continue
            lbl = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
            hint = "（页面为占位值，不静默代填）" if f_spec.name in default_lookup else ""
            missing.append(lbl + hint)
        if spec.conditional_required_fn is not None:
            for req_field, reason in spec.conditional_required_fn(payload):
                if payload.get(req_field) in (None, ""):
                    missing.append(reason)
        if missing:
            raise BusinessToolRequestError(
                f"新建{spec.label}还缺少以下必填信息，请先向用户确认：{'、'.join(missing)}。"
            )

        diff = ProposalDiff.from_details((f"新建{spec.label}",), details)
        change = RequestedEffect(
            entity,
            "create",
            "",
            "",
            canonical(_project_create_values(spec, raw_fields)),
        )
        projection = EffectProjection(
            change,
            canonical(bindings),
            canonical({}),
            canonical(derived_defaults),
            ("record",),
            preserves_unmentioned=False,
        )
        key = hashlib.sha256(canonical([entity, "create", payload]).encode()).hexdigest()
        private = {
            "actor": actor_id,
            "entity": entity,
            "payload": payload,
            "expected": expected_created,
            "bindings": bindings,
        }
        return BusinessMutationSpec(
            private,
            f"新建{spec.label}",
            diff.targets,
            details=diff.details(),
            reconcile_key=key,
            reconciliation={"entity": entity, "operation": "create_record", "body_digest": key},
            effect_projection=projection,
        )

    def validate_provenance(
        self,
        inputs: dict,
        texts: tuple[str, ...],
        proposal: BusinessMutationProposal,
        *,
        existing_values: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Verify user-supplied create inputs without borrowing scalar fields from other records."""
        if proposal.effect_projection is None:
            return
        entity = str(inputs.get("entity") or "").strip().strip("/")
        create_fn = getattr(self.port, "create_specs", None)
        spec = create_fn().get(entity) if callable(create_fn) else None
        if spec is None:
            return
        prompt = texts[-1] if texts else ""
        joined_words = "\n".join(texts)
        ledger, receipt_vals = collect_provenance_context(existing_values)

        for item in inputs.get("fields") or ():
            f_spec = spec.resolve_field(str(item.get("field") or "").strip())
            if f_spec is None:
                continue
            validate_single_field_provenance(
                f_spec,
                item.get("value"),
                old_val=None,
                texts=texts,
                prompt=prompt,
                joined_words=joined_words,
                ledger=ledger,
                receipt_vals=receipt_vals,
                existing_values=existing_values,
                allow_append=False,
                allow_existing_candidates=False,
                allow_fk_candidates=bool(f_spec.reference_entity),
            )

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        entity = args["entity"]

        def verify_actor():
            actor_id = self.port.verify_actor(context)
            if actor_id != args.get("actor"):
                raise BusinessToolIdentityError("prepared compiled create belongs to a different actor")
            spec = self._spec_for(entity)
            return actor_id, spec

        def pre_dispatch_reread(actor_state):
            actor_id, _spec = actor_state
            self.port.recheck_bindings(args.get("bindings") or [], actor_id=actor_id)
            return None

        def dispatch_once(actor_state, _reread):
            _actor_id, spec = actor_state
            return self.port.dispatch_create(spec, args["payload"])

        def verify_readback(actor_state, _reread, dispatch_result, _uncertain):
            _actor_id, spec = actor_state
            created_id = dispatch_result
            if created_id is None or created_id == "":
                raise BusinessMutationIndeterminate(
                    "compiled create dispatch did not return a record id",
                    phase="postcondition",
                    reconciliation={"entity": entity},
                )
            read_created_fn = getattr(self.port, "read_created_record", None)
            after = (
                read_created_fn(spec, created_id, args["payload"])
                if callable(read_created_fn)
                else self.port.read_record(entity, created_id)
            )
            if not isinstance(after, dict):
                raise BusinessMutationIndeterminate(
                    "compiled create readback returned no record",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": created_id},
                )
            field_kinds = {f.name: f.kind for f in spec.fields}
            spec_fields = {f.name: f for f in spec.fields}
            match_fn = getattr(self.port, "matches_readback_value", None)
            ignored = spec.server_computed_fields | spec.write_only_fields
            for key, expected_val in (args.get("expected") or {}).items():
                if key in ignored:
                    continue
                matched = (
                    match_fn(spec, spec_fields[key], after.get(key), expected_val)
                    if callable(match_fn) and key in spec_fields
                    else _values_equal(after.get(key), expected_val, kind=field_kinds.get(key, ""))
                )
                if not matched:
                    raise BusinessMutationIndeterminate(
                        f"compiled create readback did not confirm field {key}",
                        phase="postcondition",
                        reconciliation={"entity": entity, "id": created_id, "field": key},
                    )
            disp_name = _record_display_name(after, spec.name_fields, spec.id_field)
            reference = f"{disp_name}（ID={created_id}）" if disp_name and disp_name != str(created_id) else f"ID={created_id}"
            effects = [{"effect": "record", "verified": True, "id": created_id}]
            return BusinessToolResult(
                {"verified": True, "record_id": str(created_id), "entity": entity, "reference": reference, "effects": effects},
                {"verified": True, "operation": "create_record", "effects": effects},
            )

        return execute_verified_handler_write(
            verify_actor=verify_actor,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            invalidate_reads=lambda: self.port.invalidate_reads(frozenset({entity})),
            allow_readback_recovery=False,
            use_verification_interval=False,
            readback_failure_message="compiled create acknowledged; same-actor readback failed",
            readback_reconciliation=lambda _rs, dr: {"entity": entity, "id": dr},
        )


def _match_child_line(
    col_spec: CompiledChildCollectionSpec,
    items: list[dict],
    match_token: str,
) -> tuple[int, dict]:
    token = match_token.strip()
    if not token:
        raise BusinessToolRequestError(f"修改「{col_spec.label}」明细行时 match 不能为空。")
    m_idx = _INDEX_MATCH.fullmatch(token)
    if m_idx:
        idx = int(m_idx.group(1)) - 1
        if 0 <= idx < len(items):
            return idx, items[idx]
        raise BusinessToolRequestError(
            f"「{col_spec.label}」共有 {len(items)} 条明细，行号「{match_token}」超出范围。"
        )
    m_cand = _CANDIDATE_ID_SUFFIX.fullmatch(token)
    clean_name = m_cand.group(1).strip() if m_cand else token
    hint_id = m_cand.group(2).strip() if m_cand else None
    folded = clean_name.casefold()
    matches: list[tuple[int, dict]] = []
    for idx, row in enumerate(items):
        rid = row.get(col_spec.id_field)
        if hint_id is not None and str(rid) != hint_id:
            continue
        names = [
            str(row.get(nf)).strip()
            for nf in (*col_spec.name_fields, "code", "name", "digest", "subjectName", "productName")
            if row.get(nf) not in (None, "")
        ]
        if any(n.casefold() == folded for n in names):
            matches.append((idx, row))
        elif hint_id is None and rid is not None and str(rid) == clean_name:
            matches.append((idx, row))
    if len(matches) == 1:
        return matches[0]
    choices = [
        f"#{i + 1}: {_record_display_name(row, col_spec.name_fields, col_spec.id_field)}"
        for i, row in enumerate(items[:8])
    ]
    if len(matches) > 1:
        raise BusinessToolRequestError(
            f"「{col_spec.label}」中有多条匹配「{match_token}」，请改用明确行号（如 '#1'）或带 ID 后缀：{'；'.join(choices)}。"
        )
    raise BusinessToolRequestError(
        f"在「{col_spec.label}」中未找到匹配「{match_token}」的明细行（当前明细：{'；'.join(choices) or '空'}）。"
    )


def _project_child_item(
    col_spec: CompiledChildCollectionSpec,
    raw_row: dict,
    *,
    encode_preserved_fn=None,
    agg_spec: CompiledAggregateUpdateSpec | None = None,
) -> dict:
    out: dict[str, object] = {}
    if col_spec.id_field in raw_row and raw_row[col_spec.id_field] is not None:
        out[col_spec.id_field] = raw_row[col_spec.id_field]
    for f_spec in col_spec.fields:
        if f_spec.name in raw_row and raw_row[f_spec.name] is not None:
            val = raw_row[f_spec.name]
            if not f_spec.required and _violates_optional_bounds(f_spec, val):
                continue
            out[f_spec.name] = (
                encode_preserved_fn(agg_spec, f_spec, val)
                if callable(encode_preserved_fn)
                else val
            )
    for sub in col_spec.subcollections:
        sub_list = raw_row.get(sub.name)
        if isinstance(sub_list, list):
            out[sub.name] = [
                _project_child_item(sub, r, encode_preserved_fn=encode_preserved_fn, agg_spec=agg_spec)
                for r in sub_list
                if isinstance(r, dict)
            ]
    return out


class CompiledAggregateUpdateHandler:
    """1–2 level master-detail aggregate-root merge, verification, and dispatch."""

    def __init__(self, port: CompiledWritePort):
        self.port = port

    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec:
        actor_id = self.port.verify_actor(context)
        entity = str(args.get("entity") or "").strip().strip("/")
        agg_fn = getattr(self.port, "aggregate_update_specs", None)
        spec = agg_fn().get(entity) if callable(agg_fn) else None
        cap = resolve_write_route_capability(self.port)
        if spec is None:
            dedicated_fn = getattr(self.port, "dedicated_tool_for", None)
            dedicated = dedicated_fn(entity, "update") if callable(dedicated_fn) else None
            raise BusinessToolRequestError(cap.unsupported_entity_message(entity, dedicated=dedicated))
        raw_changes = list(args.get("changes") or ())
        raw_line_changes = list(args.get("line_changes") or ())
        if not raw_changes and not raw_line_changes:
            raise BusinessToolRequestError("changes 与 line_changes 不能同时为空。")

        record = self.port.resolve_target(entity, str(args["target"]).strip(), spec=spec)
        record_id = record.get(spec.id_field)
        if record_id in (None, ""):
            raise BusinessToolRequestError("目标记录缺少唯一主键，无法执行聚合修改。")

        encode_preserved_fn = getattr(self.port, "encode_preserved_field", None)
        match_fn = getattr(self.port, "matches_readback_value", None)

        seen_parent: set[str] = set()
        changed_parent: dict[str, object] = {}
        bindings: list[dict] = []
        details = []

        # 1. Encode requested parent field changes
        for item in raw_changes:
            raw_field = str(item.get("field") or "").strip()
            f_spec = spec.resolve_field(raw_field)
            if f_spec is None:
                supported = "、".join(f"{f.name}({f.label})" if f.label else f.name for f in spec.fields[:20])
                raise BusinessToolRequestError(cap.unsupported_field_message(entity, raw_field, supported))
            if f_spec.name in seen_parent:
                raise BusinessToolRequestError(f"主表字段「{f_spec.name}」在同一次修改中重复出现。")
            seen_parent.add(f_spec.name)
            api_val, old_disp, new_disp, binding = self.port.encode_field(spec, f_spec, item.get("value"), record)
            _check_explicit_bounds(f_spec, api_val)
            same_now = (
                match_fn(spec, f_spec, record.get(f_spec.name), api_val)
                if callable(match_fn)
                else _values_equal(record.get(f_spec.name), api_val, kind=f_spec.kind)
            )
            if same_now:
                continue
            changed_parent[f_spec.name] = api_val
            if binding is not None:
                bindings.append(binding)
            shown_label = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
            details.append(change_detail(shown_label, str(old_disp), str(new_disp)))

        # 2. Construct full preserved parent payload & deep-projected child collections
        payload: dict[str, object] = {}
        if not spec.omit_root_id_in_payload:
            payload[spec.id_field] = record_id
        for cid_f in spec.composite_id_fields:
            if cid_f != spec.id_field and cid_f in record and record[cid_f] is not None:
                payload[cid_f] = record[cid_f]
        for f_spec in spec.fields:
            if f_spec.name in changed_parent:
                payload[f_spec.name] = changed_parent[f_spec.name]
            elif f_spec.name in record:
                rec_val = record[f_spec.name]
                if not f_spec.required and _violates_optional_bounds(f_spec, rec_val):
                    continue
                payload[f_spec.name] = (
                    encode_preserved_fn(spec, f_spec, rec_val)
                    if callable(encode_preserved_fn)
                    else rec_val
                )
            elif f_spec.required:
                raise BusinessToolRequestError(cap.missing_required_field_message(f_spec.name))

        projected_collections: dict[str, list[dict]] = {}
        raw_collections: dict[str, list[dict]] = {}
        for col in spec.collections:
            orig_list = [r for r in (record.get(col.name) or ()) if isinstance(r, dict)]
            raw_collections[col.name] = orig_list
            projected_collections[col.name] = [
                _project_child_item(col, r, encode_preserved_fn=encode_preserved_fn, agg_spec=spec)
                for r in orig_list
            ]

        # 3. Apply 1st- and 2nd-level child line changes while keeping untouched sibling items/lines
        line_changed = False
        touched_cols: set[str] = set()
        for lc in raw_line_changes:
            col_spec = spec.resolve_collection(lc.get("collection"))
            if col_spec is None:
                avail = "、".join(c.name for c in spec.collections)
                raise BusinessToolRequestError(
                    f"实体「{entity}」包含多个明细集合，请通过 collection 明确指定（可选：{avail}）。"
                )
            touched_cols.add(col_spec.name)
            mode = str(lc.get("mode") or "update").strip()
            if mode != "remove" and not (lc.get("changes") or ()):
                raise BusinessToolRequestError(f"明细「{col_spec.label}」非删除操作必须提供非空的 changes。")
            subcol_tok = str(lc.get("subcollection") or "").strip()
            items = projected_collections[col_spec.name]
            raw_items = raw_collections[col_spec.name]

            if subcol_tok:
                sub_spec = col_spec.resolve_subcollection(subcol_tok)
                if sub_spec is None:
                    raise BusinessToolRequestError(f"明细「{col_spec.label}」不存在二级子表「{subcol_tok}」。")
                p_idx, _p_proj = _match_child_line(col_spec, raw_items, str(lc.get("match") or ""))
                parent_raw = raw_items[p_idx]
                parent_proj = items[p_idx]
                sub_raw_list = [r for r in (parent_raw.get(sub_spec.name) or ()) if isinstance(r, dict)]
                sub_proj_list = parent_proj.setdefault(sub_spec.name, [])
                if mode == "remove":
                    s_idx, _ = _match_child_line(sub_spec, sub_raw_list, str(lc.get("submatch") or ""))
                    if len(sub_proj_list) - 1 < sub_spec.min_items:
                        raise BusinessToolRequestError(f"二级明细「{sub_spec.label}」至少需保留 {sub_spec.min_items} 条。")
                    sub_proj_list.pop(s_idx)
                    raw_sub_name = _record_display_name(sub_raw_list[s_idx], sub_spec.name_fields, sub_spec.id_field)
                    details.append(change_detail(f"{col_spec.label}#{p_idx + 1}.{sub_spec.label}", raw_sub_name, "（已移除）"))
                    line_changed = True
                elif mode == "add":
                    if not sub_spec.allow_add:
                        raise BusinessToolRequestError(f"二级明细「{sub_spec.label}」不支持新增行。")
                    new_sub: dict[str, object] = {}
                    for ch in lc.get("changes") or ():
                        f_spec = sub_spec.resolve_field(str(ch.get("field") or "").strip())
                        if f_spec is None:
                            raise BusinessToolRequestError(f"二级明细「{sub_spec.label}」不支持字段「{ch.get('field')}」。")
                        api_val, _o, new_d, b = self.port.encode_field(sub_spec, f_spec, ch.get("value"), {})
                        _check_explicit_bounds(f_spec, api_val)
                        new_sub[f_spec.name] = api_val
                        if b is not None:
                            bindings.append(b)
                        details.append(create_detail(f"{col_spec.label}#{p_idx + 1}.新增{sub_spec.label}.{f_spec.label or f_spec.name}", str(new_d)))
                    for req_f in sub_spec.required_on_add:
                        if new_sub.get(req_f) is None:
                            raise BusinessToolRequestError(f"新增二级明细「{sub_spec.label}」缺少必填字段「{req_f}」。")
                    sub_proj_list.append(new_sub)
                    line_changed = True
                else:
                    s_idx, s_raw = _match_child_line(sub_spec, sub_raw_list, str(lc.get("submatch") or ""))
                    s_proj = sub_proj_list[s_idx]
                    for ch in lc.get("changes") or ():
                        f_spec = sub_spec.resolve_field(str(ch.get("field") or "").strip())
                        if f_spec is None:
                            raise BusinessToolRequestError(f"二级明细「{sub_spec.label}」不支持字段「{ch.get('field')}」。")
                        api_val, old_d, new_d, b = self.port.encode_field(sub_spec, f_spec, ch.get("value"), s_raw)
                        _check_explicit_bounds(f_spec, api_val)
                        if not _values_equal(s_raw.get(f_spec.name), api_val, kind=f_spec.kind):
                            s_proj[f_spec.name] = api_val
                            line_changed = True
                            if b is not None:
                                bindings.append(b)
                            details.append(change_detail(f"{col_spec.label}#{p_idx + 1}.{sub_spec.label}#{s_idx + 1}.{f_spec.label or f_spec.name}", str(old_d), str(new_d)))
                continue

            if mode == "remove":
                if not col_spec.allow_remove:
                    raise BusinessToolRequestError(f"明细「{col_spec.label}」不支持删除行。")
                l_idx, l_raw = _match_child_line(col_spec, raw_items, str(lc.get("match") or ""))
                if len(items) - 1 < col_spec.min_items:
                    raise BusinessToolRequestError(f"「{col_spec.label}」至少需保留 {col_spec.min_items} 条明细。")
                items.pop(l_idx)
                raw_items.pop(l_idx)
                details.append(change_detail(f"{col_spec.label}#{l_idx + 1}", _record_display_name(l_raw, col_spec.name_fields, col_spec.id_field), "（已移除）"))
                line_changed = True
            elif mode == "add":
                if not col_spec.allow_add:
                    raise BusinessToolRequestError(f"明细「{col_spec.label}」不支持新增行。")
                new_line: dict[str, object] = {}
                for ch in lc.get("changes") or ():
                    f_spec = col_spec.resolve_field(str(ch.get("field") or "").strip())
                    if f_spec is None:
                        raise BusinessToolRequestError(f"明细「{col_spec.label}」不支持字段「{ch.get('field')}」。")
                    api_val, _o, new_d, b = self.port.encode_field(col_spec, f_spec, ch.get("value"), {})
                    _check_explicit_bounds(f_spec, api_val)
                    new_line[f_spec.name] = api_val
                    if b is not None:
                        bindings.append(b)
                    details.append(create_detail(f"新增{col_spec.label}.{f_spec.label or f_spec.name}", str(new_d)))
                for req_f in col_spec.required_on_add:
                    if new_line.get(req_f) is None:
                        raise BusinessToolRequestError(f"新增「{col_spec.label}」缺少必填字段「{req_f}」。")
                items.append(new_line)
                line_changed = True
            else:
                l_idx, l_raw = _match_child_line(col_spec, raw_items, str(lc.get("match") or ""))
                l_proj = items[l_idx]
                for ch in lc.get("changes") or ():
                    f_spec = col_spec.resolve_field(str(ch.get("field") or "").strip())
                    if f_spec is None:
                        supported_line = "、".join(f"{f.name}({f.label})" if f.label else f.name for f in col_spec.fields[:20])
                        raise BusinessToolRequestError(f"明细「{col_spec.label}」不支持字段「{ch.get('field')}」（支持：{supported_line}）。")
                    api_val, old_d, new_d, b = self.port.encode_field(col_spec, f_spec, ch.get("value"), l_raw)
                    _check_explicit_bounds(f_spec, api_val)
                    if callable(match_fn) and match_fn(col_spec, f_spec, l_raw.get(f_spec.name), api_val):
                        continue
                    if not callable(match_fn) and _values_equal(l_raw.get(f_spec.name), api_val, kind=f_spec.kind):
                        continue
                    l_proj[f_spec.name] = api_val
                    line_changed = True
                    if b is not None:
                        bindings.append(b)
                    details.append(change_detail(f"{col_spec.label}#{l_idx + 1}.{f_spec.label or f_spec.name}", str(old_d), str(new_d)))

        if not changed_parent and not line_changed:
            raise BusinessMutationRefusedError(
                "目标记录及其明细字段已是请求的目标值，无需重复修改。",
                reason="already_in_target_state",
            )

        for col in spec.collections:
            payload[col.name] = projected_collections[col.name]

        preserved_lines = sum(len(projected_collections[c.name]) for c in spec.collections)
        details.append(
            preserved_detail("主子表其余字段保留", f"同账号拉取完整聚合根，原样保留未改主表字段及共 {preserved_lines} 条明细行")
        )

        # Strip secrets before persisting proposal args/snapshots
        clean_record = _strip_secrets_deep(record)
        snapshot = non_volatile_snapshot(clean_record, spec.volatile_fields, secret_pattern=_SECRET_FIELD)
        restricted_digests = restricted_snapshot_digests(record, spec.volatile_fields, secret_pattern=_SECRET_FIELD)
        disp_name = _record_display_name(record, spec.name_fields, spec.id_field)
        target_label = f"{disp_name}（{spec.label} #{record_id}）" if disp_name != str(record_id) else f"{spec.label} #{record_id}"
        diff = ProposalDiff.from_details((target_label,), details)

        effect_values = _project_aggregate_values(spec, raw_changes, raw_line_changes)
        before_projected = {
            k: clean_record.get(k)
            for k in changed_parent
            if k in clean_record
        }
        change = RequestedEffect(entity, "update", entity, str(args["target"]).strip(), canonical(effect_values))
        projection = EffectProjection(
            change,
            canonical(bindings),
            canonical(snapshot),
            canonical({}),
            ("record", "unrelated_field_preservation"),
            preserves_unmentioned=True,
            before_json=canonical(before_projected),
        )
        key = hashlib.sha256(canonical([entity, record_id, changed_parent, projected_collections]).encode()).hexdigest()
        private = {
            "_handler_kind": "aggregate",
            "actor": actor_id,
            "entity": entity,
            "record_id": record_id,
            "composite_id": {k: record.get(k) for k in spec.composite_id_fields if k in record},
            "payload": payload,
            "changed_parent": changed_parent,
            "expected_collections": projected_collections,
            "touched_collections": sorted(touched_cols),
            "snapshot": snapshot,
            "restricted_field_digests": restricted_digests,
            "bindings": bindings,
        }
        return BusinessMutationSpec(
            private,
            f"修改{spec.label}「{disp_name}」",
            diff.targets,
            details=diff.details(),
            reconcile_key=key,
            reconciliation={"entity": entity, "id": record_id, "operation": "update_record", "body_digest": key},
            effect_projection=projection,
        )

    def validate_provenance(
        self,
        inputs: dict,
        texts: tuple[str, ...],
        proposal: BusinessMutationProposal,
        *,
        existing_values: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        if proposal.effect_projection is None:
            return
        entity = str(inputs.get("entity") or "").strip().strip("/")
        agg_fn = getattr(self.port, "aggregate_update_specs", None)
        spec = agg_fn().get(entity) if callable(agg_fn) else None
        if spec is None:
            return
        before_map = json.loads(proposal.effect_projection.before_json)
        prompt = texts[-1] if texts else ""
        joined_words = "\n".join(texts)
        ledger, receipt_vals = collect_provenance_context(existing_values)

        for item in inputs.get("changes") or ():
            f_spec = spec.resolve_field(str(item.get("field") or "").strip())
            if f_spec is None:
                continue
            validate_single_field_provenance(
                f_spec,
                item.get("value"),
                old_val=before_map.get(f_spec.name),
                texts=texts,
                prompt=prompt,
                joined_words=joined_words,
                ledger=ledger,
                receipt_vals=receipt_vals,
                existing_values=existing_values,
                allow_append=True,
                allow_existing_candidates=True,
            )

        for lc in inputs.get("line_changes") or ():
            col_spec = spec.resolve_collection(lc.get("collection"))
            if col_spec is None:
                continue
            subcol = str(lc.get("subcollection") or "").strip()
            target_spec = col_spec.resolve_subcollection(subcol) if subcol else col_spec
            if target_spec is None:
                continue
            for item in lc.get("changes") or ():
                f_spec = target_spec.resolve_field(str(item.get("field") or "").strip())
                if f_spec is None:
                    continue
                validate_single_field_provenance(
                    f_spec,
                    item.get("value"),
                    old_val=None,
                    texts=texts,
                    prompt=prompt,
                    joined_words=joined_words,
                    ledger=ledger,
                    receipt_vals=receipt_vals,
                    existing_values=existing_values,
                    allow_append=False,
                    allow_existing_candidates=True,
                )

    def _verify_collection_match(
        self,
        spec: CompiledAggregateUpdateSpec,
        col: CompiledChildCollectionSpec,
        expected_rows: list[dict],
        actual_rows: object,
    ) -> None:
        if not isinstance(actual_rows, list) or len(actual_rows) != len(expected_rows):
            raise BusinessMutationIndeterminate(
                f"aggregate update readback count mismatch on collection {col.name}",
                phase="postcondition",
                reconciliation={"entity": spec.entity, "collection": col.name},
            )
        match_fn = getattr(self.port, "matches_readback_value", None)
        col_ignored = col.server_computed_fields | col.volatile_fields | {col.id_field}
        for idx, (exp_row, act_row) in enumerate(zip(expected_rows, actual_rows)):
            if not isinstance(act_row, dict):
                raise BusinessMutationIndeterminate(
                    f"aggregate update readback row #{idx + 1} invalid on {col.name}",
                    phase="postcondition",
                    reconciliation={"entity": spec.entity, "collection": col.name},
                )
            for f_spec in col.fields:
                if f_spec.name in col_ignored or f_spec.name not in exp_row:
                    continue
                matched = (
                    match_fn(col, f_spec, act_row.get(f_spec.name), exp_row[f_spec.name])
                    if callable(match_fn)
                    else _values_equal(act_row.get(f_spec.name), exp_row[f_spec.name], kind=f_spec.kind)
                )
                if not matched:
                    raise BusinessMutationIndeterminate(
                        f"aggregate update readback mismatch on {col.name}[{idx}].{f_spec.name}",
                        phase="postcondition",
                        reconciliation={"entity": spec.entity, "collection": col.name, "field": f_spec.name},
                    )
            for sub in col.subcollections:
                if sub.name in exp_row:
                    self._verify_collection_match(spec, sub, exp_row[sub.name], act_row.get(sub.name))

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        entity = args["entity"]
        record_id = args["record_id"]

        def verify_actor():
            actor_id = self.port.verify_actor(context)
            if actor_id != args.get("actor"):
                raise BusinessToolIdentityError("prepared aggregate update belongs to a different actor")
            agg_fn = getattr(self.port, "aggregate_update_specs", None)
            spec = agg_fn().get(entity) if callable(agg_fn) else None
            if spec is None:
                raise BusinessToolRequestError("compiled aggregate update spec is no longer available")
            return actor_id, spec

        def pre_dispatch_reread(actor_state):
            actor_id, spec = actor_state
            self.port.recheck_bindings(args.get("bindings") or [], actor_id=actor_id)
            read_agg_fn = getattr(self.port, "read_aggregate_record", None)
            current = (
                read_agg_fn(spec, record_id, composite_id=args.get("composite_id") or {})
                if callable(read_agg_fn)
                else self.port.read_record(entity, record_id)
            )
            if not isinstance(current, dict):
                raise BusinessMutationRefusedError(
                    "target aggregate record is no longer readable before dispatch",
                    reason="precondition_changed",
                )
            clean_cur = _strip_secrets_deep(current)
            current_snap = non_volatile_snapshot(clean_cur, spec.volatile_fields, secret_pattern=_SECRET_FIELD)
            if verify_preservation_snapshot(
                args["snapshot"],
                current_snap,
                ignore=spec.volatile_fields,
                restricted_before=args.get("restricted_field_digests", {}),
                restricted_after=restricted_snapshot_digests(current, spec.volatile_fields, secret_pattern=_SECRET_FIELD),
                values_equal=_values_equal,
            ):
                raise BusinessMutationRefusedError(
                    "target aggregate record changed after approval; prepare again",
                    reason="precondition_changed",
                )
            return current_snap, current

        def dispatch_once(actor_state, reread_state):
            _actor_id, spec = actor_state
            _snap, current_record = reread_state
            dispatch_agg_fn = getattr(self.port, "dispatch_aggregate_update", None)
            if callable(dispatch_agg_fn):
                return dispatch_agg_fn(spec, record_id, args["payload"], current_record=current_record)
            return self.port.dispatch_update(spec, record_id, args["payload"])

        def verify_readback(actor_state, reread_state, dispatch_result, _uncertain):
            _actor_id, spec = actor_state
            current_snap, _current_record = reread_state
            read_after_fn = getattr(self.port, "read_aggregate_after_update", None)
            after = (
                read_after_fn(spec, record_id, dispatch_result, args["payload"], composite_id=args.get("composite_id") or {})
                if callable(read_after_fn)
                else self.port.read_record(entity, record_id)
            )
            if not isinstance(after, dict):
                raise BusinessMutationIndeterminate(
                    "aggregate update readback missed target record",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id},
                )
            changed_parent: dict = args.get("changed_parent") or {}
            spec_fields = {f.name: f for f in spec.fields}
            match_fn = getattr(self.port, "matches_readback_value", None)
            for key, expected_val in changed_parent.items():
                if key in spec.server_computed_fields:
                    continue
                f_spec = spec_fields.get(key)
                matched = (
                    match_fn(spec, f_spec, after.get(key), expected_val)
                    if callable(match_fn) and f_spec is not None
                    else _values_equal(after.get(key), expected_val, kind=(f_spec.kind if f_spec else ""))
                )
                if not matched:
                    raise BusinessMutationIndeterminate(
                        f"aggregate update readback did not confirm field {key}",
                        phase="postcondition",
                        reconciliation={"entity": entity, "id": record_id, "field": key},
                    )
            expected_collections = args.get("expected_collections") or {}
            for col in spec.collections:
                if col.name in expected_collections:
                    self._verify_collection_match(spec, col, expected_collections[col.name], after.get(col.name))

            col_names = {c.name for c in spec.collections}
            ignore = (
                frozenset(spec.volatile_fields)
                | frozenset(spec.server_computed_fields)
                | frozenset(changed_parent.keys())
                | col_names
            )
            clean_after = _strip_secrets_deep(after)
            drifted = verify_preservation_snapshot(
                current_snap,
                non_volatile_snapshot(clean_after, spec.volatile_fields, secret_pattern=_SECRET_FIELD),
                ignore=ignore,
                restricted_before=args.get("restricted_field_digests", {}),
                restricted_after=restricted_snapshot_digests(after, spec.volatile_fields, secret_pattern=_SECRET_FIELD),
                values_equal=_values_equal,
            )
            if drifted:
                raise BusinessMutationIndeterminate(
                    "aggregate update altered unmentioned fields: " + ", ".join(drifted[:6]),
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id, "unverified_effect": "unrelated_field_preservation"},
                )
            disp_name = _record_display_name(after, spec.name_fields, spec.id_field)
            reference = f"{disp_name}（ID={record_id}）" if disp_name and disp_name != str(record_id) else f"ID={record_id}"
            effects = [
                {"effect": "record", "verified": True, "id": record_id},
                {"effect": "unrelated_field_preservation", "verified": True, "id": record_id},
            ]
            return BusinessToolResult(
                {"verified": True, "record_id": str(record_id), "entity": entity, "reference": reference, "effects": effects},
                {"verified": True, "operation": "update_record", "effects": effects},
            )

        return execute_verified_handler_write(
            verify_actor=verify_actor,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            invalidate_reads=lambda: self.port.invalidate_reads(frozenset({entity})),
            allow_readback_recovery=True,
            use_verification_interval=False,
            readback_failure_message="aggregate update acknowledged; same-actor readback failed",
            readback_reconciliation=lambda _rs, _dr: {"entity": entity, "id": record_id},
        )


class UnifiedUpdateHandler:
    """Router between symmetric flat ``update_record`` and 1–2 level aggregate-root updates."""

    def __init__(self, port: CompiledWritePort):
        self.port = port
        self._flat = ConventionUpdateHandler(port)
        self._agg = CompiledAggregateUpdateHandler(port)

    def execute(self, args: dict, *, context=None) -> BusinessToolResult:
        return self._flat.execute(args, context=context)

    def _is_aggregate(self, entity: str) -> bool:
        agg_fn = getattr(self.port, "aggregate_update_specs", None)
        return callable(agg_fn) and entity in agg_fn()

    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec:
        entity = str(args.get("entity") or "").strip().strip("/")
        if self._is_aggregate(entity):
            return self._agg.prepare(args, context=context)
        if args.get("line_changes"):
            if entity in self.port.update_specs():
                raise BusinessToolRequestError(
                    f"实体「{entity}」仅支持扁平主表字段修改（changes），不支持明细行 line_changes。"
                )
        return self._flat.prepare(args, context=context)

    def validate_provenance(
        self,
        inputs: dict,
        texts: tuple[str, ...],
        proposal: BusinessMutationProposal,
        *,
        existing_values: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        entity = str(inputs.get("entity") or "").strip().strip("/")
        if self._is_aggregate(entity):
            self._agg.validate_provenance(inputs, texts, proposal, existing_values=existing_values)
            return
        self._flat.validate_provenance(inputs, texts, proposal, existing_values=existing_values)

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        if args.get("_handler_kind") == "aggregate":
            return self._agg.dispatch(args, context=context)
        return self._flat.dispatch(args, context=context)


def _find_relation_spec(port: object, entity: str, relation: str) -> CompiledRelationSpec | None:
    rel_fn = getattr(port, "relation_specs", None)
    if not callable(rel_fn):
        return None
    specs = rel_fn().get(entity) or ()
    matches = [s for s in specs if s.matches_relation(relation)]
    if len(matches) == 1:
        return matches[0]
    if not relation and len(specs) == 1:
        return specs[0]
    return None


class CompiledRelationHandler:
    """Shared prepare / provenance-check / reread / single-dispatch / readback handler for ``assign_record_relations``."""

    def __init__(self, port: CompiledWritePort):
        self.port = port

    def execute(self, args: dict, *, context=None) -> BusinessToolResult:
        raise BusinessToolRequestError("assign_record_relations is a write proposal tool")

    def _spec_for(self, entity: str, relation: str) -> CompiledRelationSpec:
        rel_fn = getattr(self.port, "relation_specs", None)
        specs = rel_fn().get(entity) if callable(rel_fn) else None
        spec = _find_relation_spec(self.port, entity, relation)
        if spec is None:
            cap = resolve_write_route_capability(self.port)
            supported = "、".join(f"{s.relation}({s.relation_label})" for s in (specs or ()))
            raise BusinessToolRequestError(
                cap.unsupported_relation_message(entity, relation, supported_relations=supported)
            )
        return spec

    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec:
        actor_id = self.port.verify_actor(context)
        entity = str(args.get("entity") or "").strip().strip("/")
        relation = str(args.get("relation") or "").strip()
        spec = self._spec_for(entity, relation)

        target_token = str(args.get("target") or "").strip()
        record = self.port.resolve_target(entity, target_token, spec=spec)
        record_id = record.get(spec.id_field)
        if record_id in (None, ""):
            raise BusinessToolRequestError("目标记录缺少唯一主键，无法分配关联集合。")

        raw_items = [str(x).strip() for x in (args.get("items") or ()) if str(x).strip()]
        if spec.single_value and len(raw_items) > 1:
            raise BusinessToolRequestError(
                f"关系「{spec.relation_label}」一次仅支持关联 1 个目标对象，请只传入 1 项。"
            )
        after_ids, after_labels, bindings = self.port.resolve_relation_items(spec, raw_items)
        before_ids = list(self.port.read_relation_ids(spec, record_id))
        if {str(x) for x in before_ids} == {str(x) for x in after_ids}:
            raise BusinessMutationRefusedError(
                f"{spec.label}当前的「{spec.relation_label}」集合已与目标一致，无需重复操作。",
                reason="already_in_target_state",
            )
        before_labels = list(self.port.relation_labels_for_ids(spec, before_ids))
        disp_name = _record_display_name(record, spec.name_fields, spec.id_field)
        target_label = f"{disp_name}（{spec.label} #{record_id}）" if disp_name != str(record_id) else f"{spec.label} #{record_id}"
        before_text = "、".join(before_labels) if before_labels else "（空）"
        after_text = "、".join(after_labels) if after_labels else "（空）"
        diff = ProposalDiff.from_details(
            (target_label,),
            (
                preserved_detail(spec.label, disp_name),
                change_detail(spec.relation_label, before_text, after_text),
            ),
        )
        change = RequestedEffect(
            entity,
            "update",
            entity,
            target_token,
            canonical({spec.items_param: _effect_value(raw_items)}),
        )
        projection = EffectProjection(
            change,
            canonical(bindings),
            canonical({"before_ids": before_ids}),
            canonical({}),
            ("record",),
            preserves_unmentioned=True,
        )
        key = hashlib.sha256(canonical([entity, record_id, spec.items_param, after_ids]).encode()).hexdigest()
        private = {
            "actor": actor_id,
            "entity": entity,
            "relation": spec.relation,
            "record_id": record_id,
            "before_ids": before_ids,
            "after_ids": after_ids,
            "bindings": bindings,
        }
        return BusinessMutationSpec(
            private,
            f"设置{spec.label}「{disp_name}」的{spec.relation_label}",
            diff.targets,
            details=diff.details(),
            reconcile_key=key,
            reconciliation={"entity": entity, "id": record_id, "operation": "assign_record_relations", "body_digest": key},
            effect_projection=projection,
        )

    def validate_provenance(
        self,
        inputs: dict,
        texts: tuple[str, ...],
        proposal: BusinessMutationProposal,
        *,
        existing_values: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        if proposal.effect_projection is None:
            return
        entity = str(inputs.get("entity") or "").strip().strip("/")
        spec = _find_relation_spec(self.port, entity, str(inputs.get("relation") or "").strip())
        if spec is None:
            return
        ledger, _receipts = collect_provenance_context(existing_values)
        scoped_vals = {
            cand.value for cand in ledger.candidates
            if cand.binding in {spec.target_entity, spec.relation, spec.relation_label, spec.items_param}
        }
        for tok in inputs.get("items") or ():
            s_tok = str(tok).strip()
            if not s_tok:
                continue
            m_cand = _CANDIDATE_ID_SUFFIX.fullmatch(s_tok)
            clean = m_cand.group(1).strip() if m_cand else s_tok
            if literal_supplied(s_tok, texts) or literal_supplied(clean, texts) or s_tok in scoped_vals or clean in scoped_vals:
                continue
            raise BusinessToolRequestError(
                f"关联项「{s_tok[:60]}」未在用户原话或已读候选中找到依据。"
            )

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        entity = args["entity"]
        relation = args["relation"]
        record_id = args["record_id"]

        def verify_actor():
            actor_id = self.port.verify_actor(context)
            if actor_id != args.get("actor"):
                raise BusinessToolIdentityError("prepared relation assignment belongs to a different actor")
            return self._spec_for(entity, relation)

        def pre_dispatch_reread(spec: CompiledRelationSpec):
            self.port.recheck_bindings(args.get("bindings") or [], actor_id=args["actor"])
            current_ids = list(self.port.read_relation_ids(spec, record_id))
            if {str(x) for x in current_ids} != {str(x) for x in (args.get("before_ids") or ())}:
                raise BusinessMutationRefusedError(
                    "current relation set changed after approval; prepare again",
                    reason="precondition_changed",
                )
            return current_ids

        def dispatch_once(spec: CompiledRelationSpec, _before):
            self.port.dispatch_relation(spec, record_id, list(args.get("after_ids") or ()))

        def verify_readback(spec: CompiledRelationSpec, _before, _disp, _uncertain):
            after_ids = list(self.port.read_relation_ids(spec, record_id))
            if {str(x) for x in after_ids} != {str(x) for x in (args.get("after_ids") or ())}:
                raise BusinessMutationIndeterminate(
                    "relation assignment readback did not confirm target set",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id, "relation": spec.relation},
                )
            effects = [{"effect": "record", "verified": True, "id": record_id}]
            return BusinessToolResult(
                {"verified": True, "record_id": str(record_id), "entity": entity, "effects": effects},
                {"verified": True, "operation": "assign_record_relations", "effects": effects},
            )

        return execute_verified_handler_write(
            verify_actor=verify_actor,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            invalidate_reads=lambda: self.port.invalidate_reads(frozenset({entity})),
            allow_readback_recovery=True,
            use_verification_interval=False,
            readback_failure_message="relation assignment acknowledged; same-actor readback failed",
            readback_reconciliation=lambda _rs, _dr: {"entity": entity, "id": record_id, "relation": relation},
        )


_VALUE_ANYOF = {
    "anyOf": [
        {"type": "string", "maxLength": 20000},
        {"type": "integer"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "maxLength": 200},
        },
    ]
}

_FIELD_CHANGE_ITEM = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "接口字段名或表单中文标签。",
        },
        "value": _VALUE_ANYOF,
    },
    "required": ["field", "value"],
    "additionalProperties": False,
}


def compiled_write_tool_definitions(
    port: CompiledWritePort,
    *,
    scope: str,
    entity_example: str = "records",
) -> tuple[ToolDefinition, ...]:
    """Build all 4 compiled write ``ToolDefinition`` entries (create, flat/aggregate update, status, relation)."""
    create_schema = {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": f"支持编译新建的实体路径键（如 {entity_example}，不含 /create 后缀）。",
            },
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": 40,
                "description": "新建字段列表（或传 {字段名: 值} 对象）。仅填写用户明确要求的内容；页面合法默认初始值（如当前负责人、默认类型）会自动填入并标为「（页面默认值）」；占位哨兵及缺失必填项会拒批提示补充。",
                "items": _FIELD_CHANGE_ITEM,
            },
        },
        "required": ["entity", "fields"],
        "additionalProperties": False,
    }
    update_schema = {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": f"目标实体路径键（如 {entity_example}，不含详情或更新动作后缀）。",
            },
            "target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "目标记录的确切业务名称、单据号或唯一候选显示名（如 '名称（ID=123）'）。",
            },
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "description": "主表要修改的字段及新值；未列出字段自动保留同账号现值。按分金额传带单位字符串（如 '25元'），折扣传如 '8.5折'。",
                "items": _FIELD_CHANGE_ITEM,
            },
            "line_changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "description": "仅用于含 1–2 层明细子表的聚合单据修改：逐行指定 match（如 '#1' 或行名称），未提及的明细字段及其他明细行原样保留。",
                "items": {
                    "type": "object",
                    "properties": {
                        "collection": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 80,
                            "description": "明细集合字段名（单子表可省略，多子表必传如 items / entries / salaryOptions）。",
                        },
                        "match": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "目标明细行匹配标识（如 '#1'、编码或名称；新增可传 '+'）。",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["update", "add", "remove"],
                            "description": "行操作模式：update（默认修改匹配行）、add（新增行）或 remove（移除匹配行）。",
                        },
                        "subcollection": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 80,
                            "description": "二级子表集合名（如 entries 下的 auxiliaries，仅修改二级明细时填写）。",
                        },
                        "submatch": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "二级子行匹配标识（如 '#1' 或项目名）。",
                        },
                        "changes": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "description": "该行要修改或新增的字段及新值。",
                            "items": _FIELD_CHANGE_ITEM,
                        },
                    },
                    "required": ["match"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["entity", "target"],
        "additionalProperties": False,
    }
    status_schema = {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": f"支持同步两态互逆状态切换的实体路径键（如 {entity_example}）。",
            },
            "target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "目标记录的确切业务名称或唯一候选显示名。",
            },
            "status": {
                "type": "string",
                "minLength": 1,
                "maxLength": 40,
                "description": "目标状态标签（如 开启/关闭、启用/停用、上架/下架）。",
            },
        },
        "required": ["entity", "target", "status"],
        "additionalProperties": False,
    }
    relation_schema = {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": "主实体路径键（如 system/user、system/role）。",
            },
            "target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "目标记录的确切业务名称或唯一候选显示名。",
            },
            "relation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "关联关系键名或中文标签（如 分配角色 / roleIds / 分配菜单 / menuIds）。",
            },
            "items": {
                "type": "array",
                "minItems": 0,
                "maxItems": 50,
                "description": "目标完整关联对象名称集合（全量替换；清空传 []）。",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
            },
        },
        "required": ["entity", "target", "relation", "items"],
        "additionalProperties": False,
    }

    create_handler = CompiledCreateHandler(port)
    update_handler = UnifiedUpdateHandler(port)
    status_handler = ConventionStatusHandler(port)
    relation_handler = CompiledRelationHandler(port)

    return (
        ToolDefinition(
            "create_record",
            "通过已编译 API 创建扁平业务记录（自动补充表单合法页面默认值并显式标出「（页面默认值）」；占位哨兵或缺失必填值将追问；严禁代填含密码新建接口，新建不借用本轮其他记录字段值）。",
            json.dumps(create_schema, ensure_ascii=False),
            "write",
            scope,
            ToolOperation("convention_record", "create"),
            create_handler,
            effect_contract=CompiledWriteEffects("create_record", port),
            preserves_unmentioned=False,
        ),
        ToolDefinition(
            "update_record",
            "修改单条现有记录（覆盖扁平对称记录与 1–2 层嵌套明细单据：同账号拉取完整聚合根→仅替换指定主/子表字段并原样保留其余主表字段及未改明细行→自动剥离密码→批准后单次提交并全量回读核验）。",
            json.dumps(update_schema, ensure_ascii=False),
            "write",
            scope,
            ToolOperation("convention_record", "update"),
            update_handler,
            reference_inputs=("target",),
            effect_contract=CompiledWriteEffects("update_record", port),
            preserves_unmentioned=True,
        ),
        ToolDefinition(
            "toggle_record_status",
            "切换同步两态互逆状态开关（开启/关闭、启用/停用、上架/下架；批准后单次提交并全字段回读核验，不含多态审批流转）。",
            json.dumps(status_schema, ensure_ascii=False),
            "write",
            scope,
            ToolOperation("convention_record", "state_change"),
            status_handler,
            reference_inputs=("target",),
            effect_contract=CompiledWriteEffects("toggle_record_status", port),
            preserves_unmentioned=True,
        ),
        ToolDefinition(
            "assign_record_relations",
            "维护记录的多对多关联集合（如为用户分配角色或为角色分配菜单：按确切名称解析关联项、展示前后集合变化、批准后单次提交并同账号回读集合一致性核验）。",
            json.dumps(relation_schema, ensure_ascii=False),
            "write",
            scope,
            ToolOperation("convention_record", "update"),
            relation_handler,
            reference_inputs=("target", "items"),
            effect_contract=CompiledWriteEffects("assign_record_relations", port),
            preserves_unmentioned=True,
        ),
    )


__all__ = [
    "CompiledAggregateUpdateHandler",
    "CompiledAggregateUpdateSpec",
    "CompiledChildCollectionSpec",
    "CompiledCreateHandler",
    "CompiledCreateSpec",
    "CompiledFieldDefault",
    "CompiledRelationHandler",
    "CompiledRelationSpec",
    "CompiledWriteEffects",
    "CompiledWritePort",
    "DefaultValueKind",
    "PAGE_DEFAULT_ANNOTATION",
    "UnifiedUpdateHandler",
    "compiled_write_tool_definitions",
    "is_rejected_ui_sentinel",
    "normalize_create_arguments",
    "normalize_update_arguments",
]
