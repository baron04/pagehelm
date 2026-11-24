"""Application-neutral contract for symmetric flat record updates and two-state toggles.

An adapter that declares the ``"updates"`` capability may derive flat 1:1 record update
and two-state reversible status-toggle specs from its published schema and compiled rules.
Nothing here names an application, endpoint convention, or UI framework. Every proposal
runs through the shared ``BusinessWriteService`` chain:

1. Verify the live bound actor and resolve exactly one readable target record.
2. Check every changed value against the person's own words, safe append preservation,
   read candidates, or verified unit conversions before any card is shown.
3. Project the exact target and ``before -> after`` changes onto one visible approval card.
4. Re-read the full record under the same actor immediately before dispatch; refuse if
   any non-volatile field changed since preparation.
5. Dispatch at most once (never retrying uncertain transport outcomes) and read the full
   record back under the same actor, requiring changed fields to match and all other
   non-volatile fields to remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Callable, Literal, Protocol

from ..execution.submit_contract import same_value, scalar_text
from ..execution.value_source import (
    Entry,
    SourceLedger,
    check_entries,
    cn_number,
)
from ..goal.skill_literals import _existing_append_supplied, literal_supplied
from .effects import EffectProjection, RequestedEffect, canonical
from .errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolIdentityError,
    BusinessToolRequestError,
)
from .tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from .tools.registry import ToolDefinition
from .write_pipeline import (
    DEFAULT_SECRET_FIELD_PATTERN,
    ProposalDiff,
    change_detail,
    execute_verified_handler_write,
    non_volatile_snapshot,
    preserved_detail,
    require_append_preservation,
    restricted_snapshot_digests,
    side_effect_detail,
    verify_entry_provenance,
    verify_preservation_snapshot,
)

_SECRET_FIELD = DEFAULT_SECRET_FIELD_PATTERN
_FIELD_KINDS = frozenset({"string", "integer", "number", "boolean", "id_array", "string_array"})
_MISSING = object()
_STRIP_LABEL_UNIT_RE = re.compile(r"\s*[（(][^()（）]{1,24}[）)]\s*$")


@dataclass(frozen=True, slots=True)
class WriteRouteCapability:
    """Declarative write-routing capability driving prompts and fallback errors."""

    compiled_api_writes: bool = False
    inline_row_status_submit: bool = False
    page_form_mode: Literal["prefill_handoff", "disabled"] = "prefill_handoff"

    def __post_init__(self) -> None:
        if self.page_form_mode not in {"prefill_handoff", "disabled"}:
            raise ValueError(f"unsupported page_form_mode: {self.page_form_mode!r}")

    def form_fallback_phrase(self) -> str:
        if self.page_form_mode == "prefill_handoff":
            return "打开对应页面表单完成预填并用 then_highlight 标出提交按钮交由用户自行提交（禁止对表单提交按钮调用 request_submit）"
        return "在说明限制后停止，当前部署不开放页面表单提交"

    def unsupported_entity_message(self, entity: str, *, dedicated: str | None = None) -> str:
        if dedicated:
            return (
                f"实体「{entity}」已有已安装的专用修改操作「{dedicated}」，"
                f"请直接用 propose_operation(\"{dedicated}\", ...) 办理。"
            )
        return (
            f"实体「{entity}」不满足扁平对称约定更新条件（可能含明细行、非对称字段或无配对详情接口）；"
            f"请改用已安装的专用操作，或{self.form_fallback_phrase()}。"
        )

    def unsupported_field_message(self, entity: str, raw_field: str, supported: str) -> str:
        return (
            f"实体「{entity}」不支持通过约定更新修改字段「{raw_field}」"
            f"（支持字段（可传中文标签或字段键）：{supported}）。若是状态字段请改用 toggle_record_status 或专用状态操作；"
            f"若是其它表单专属字段请{self.form_fallback_phrase()}。"
        )

    def missing_required_field_message(self, field_name: str) -> str:
        return (
            f"目标记录当前缺少必填原字段「{field_name}」，无法安全构造整条更新请求；"
            f"请{self.form_fallback_phrase()}。"
        )

    def unsupported_status_message(self, entity: str, *, dedicated: str | None = None) -> str:
        if dedicated:
            return (
                f"实体「{entity}」已有已安装的专用状态操作「{dedicated}」，"
                f"请直接用 propose_operation(\"{dedicated}\", ...) 办理。"
            )
        if self.inline_row_status_submit:
            row_hint = "使用已安装的专用状态操作，或对该行按钮调用 request_submit"
        else:
            row_hint = f"使用已安装的专用状态操作，或{self.form_fallback_phrase()}"
        return f"实体「{entity}」不属于通用两态互逆状态开关范围（多态业务流转请{row_hint}）。"

    def unavailable_operation_message(
        self,
        op_name: str,
        *,
        entity: str = "",
        compiled_tools: tuple[str, ...] = (),
    ) -> str:
        if entity and compiled_tools:
            tools_str = " / ".join(compiled_tools)
            return (
                f"当前部署或账号下没有可用的专用写入操作「{op_name}」，"
                f"但实体「{entity}」已在已编译写目录中提供 {tools_str}；"
                f"请直接调用 {compiled_tools[0]}(entity=\"{entity}\", ...) 办理，不要误退回页面表单。"
            )
        return (
            f"当前部署或账号下没有可用的写入操作「{op_name}」；"
            f"请核对“已安装的写入操作”清单，或{self.form_fallback_phrase()}。"
        )

    def unsupported_create_message(
        self,
        entity: str,
        *,
        dedicated: str | None = None,
        excluded_reason: str = "",
    ) -> str:
        if dedicated:
            return (
                f"实体「{entity}」已有已安装的专用新建操作「{dedicated}」，"
                f"请直接用 propose_operation(\"{dedicated}\", ...) 办理。"
            )
        if excluded_reason:
            return f"实体「{entity}」不可通过通用 API 新建（{excluded_reason}）；请{self.form_fallback_phrase()}。"
        return f"实体「{entity}」暂未纳入通用编译新建契约；请改用已安装的专用操作，或{self.form_fallback_phrase()}。"

    def unsupported_relation_message(
        self,
        entity: str,
        relation: str = "",
        *,
        supported_relations: str = "",
    ) -> str:
        if supported_relations:
            return (
                f"实体「{entity}」不支持关联关系「{relation}」（当前支持：{supported_relations}）；"
                f"请确认关系名称，或{self.form_fallback_phrase()}。"
            )
        return f"实体「{entity}」暂未编译支持关联集合维护；请{self.form_fallback_phrase()}。"


def resolve_write_route_capability(source: object) -> WriteRouteCapability:
    """Resolve optional WriteRouteCapability from a port or profile object."""
    attr = getattr(source, "write_route_capability", None)
    if callable(attr):
        attr = attr()
    return attr if isinstance(attr, WriteRouteCapability) else WriteRouteCapability()


_OP_TOKEN_RE = re.compile(r"[A-Z]?[a-z]+\d*|[A-Z]+\d*(?=[A-Z][a-z]|\b)|\d+")
_COMPILED_ACTION_VERBS: dict[str, str] = {
    "create": "create_record",
    "add": "create_record",
    "new": "create_record",
    "update": "update_record",
    "edit": "update_record",
    "modify": "update_record",
    "status": "toggle_record_status",
    "toggle": "toggle_record_status",
    "enable": "toggle_record_status",
    "disable": "toggle_record_status",
}
_NON_COMPILED_ACTION_VERBS = frozenset({
    "delete", "remove", "approve", "audit", "reject", "cancel",
    "export", "import", "submit", "transfer", "lock", "unlock",
})
_COMPILED_TOOL_ORDER = ("create_record", "update_record", "toggle_record_status")


def _norm_entity_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())


def _compiled_entity_tools(available_definitions: object, goal_port: object = None) -> dict[str, tuple[str, ...]]:
    """Collect ``entity -> available compiled tool names`` from offered definitions and optional neutral port routes."""
    defs = tuple(available_definitions or ())
    offered_names = {getattr(d, "name", "") for d in defs}
    tool_map: dict[str, set[str]] = {}
    spec_getters = {
        "create_record": ("create_specs",),
        "update_record": ("update_specs", "aggregate_update_specs"),
        "toggle_record_status": ("status_specs",),
    }
    for defn in defs:
        tname = getattr(defn, "name", "")
        getters = spec_getters.get(tname)
        if not getters:
            continue
        port = getattr(getattr(defn, "handler", None), "port", None)
        if port is None:
            continue
        for getter_name in getters:
            getter = getattr(port, getter_name, None)
            if callable(getter):
                specs = getter() or {}
                if isinstance(specs, dict):
                    for ent in specs:
                        clean = str(ent).strip().strip("/")
                        if clean:
                            tool_map.setdefault(clean, set()).add(tname)
    routes_fn = getattr(goal_port, "compiled_write_routes", None) if goal_port is not None else None
    if callable(routes_fn):
        extra = routes_fn() or {}
        if isinstance(extra, dict):
            for ent, tools in extra.items():
                clean = str(ent).strip().strip("/")
                if not clean:
                    continue
                for tname in tools or ():
                    if tname in spec_getters and (not offered_names or tname in offered_names):
                        tool_map.setdefault(clean, set()).add(tname)
    return {
        ent: tuple(t for t in _COMPILED_TOOL_ORDER if t in tools)
        for ent, tools in tool_map.items()
        if tools
    }


def resolve_compiled_write_fallback(
    op_name: str,
    op_inputs: object,
    available_definitions: object,
    *,
    goal_port: object = None,
) -> tuple[str, tuple[str, ...]]:
    """Locate compiled write tools for an uninstalled ``propose_operation`` target entity."""
    entity_tools = _compiled_entity_tools(available_definitions, goal_port=goal_port)
    if not entity_tools:
        return "", ()

    tokens = [t.lower() for t in _OP_TOKEN_RE.findall(str(op_name or ""))]
    if tokens and (tokens[0] in _NON_COMPILED_ACTION_VERBS or tokens[-1] in _NON_COMPILED_ACTION_VERBS):
        return "", ()

    preferred_tool = ""
    stem_tokens = list(tokens)
    while len(stem_tokens) > 1 and stem_tokens[-1] in _COMPILED_ACTION_VERBS:
        verb = stem_tokens.pop()
        if not preferred_tool:
            preferred_tool = _COMPILED_ACTION_VERBS[verb]
    if not preferred_tool and len(stem_tokens) > 1 and stem_tokens[0] in _COMPILED_ACTION_VERBS:
        preferred_tool = _COMPILED_ACTION_VERBS[stem_tokens.pop(0)]

    matched_entity = ""
    raw_ent = op_inputs.get("entity") if isinstance(op_inputs, dict) else None
    if isinstance(raw_ent, str) and raw_ent.strip():
        clean_ent = raw_ent.strip().strip("/")
        if clean_ent in entity_tools:
            matched_entity = clean_ent
        else:
            norm_req = _norm_entity_key(clean_ent)
            hits = [e for e in entity_tools if _norm_entity_key(e) == norm_req]
            if len(hits) == 1:
                matched_entity = hits[0]

    if not matched_entity and stem_tokens:
        stem = "".join(stem_tokens)
        exact = [e for e in entity_tools if _norm_entity_key(e) == stem]
        if len(exact) == 1:
            matched_entity = exact[0]
        else:
            variants = {stem, f"{stem}s"}
            if stem.endswith("s") and len(stem) > 1:
                variants.add(stem[:-1])
            approx = [e for e in entity_tools if _norm_entity_key(e) in variants]
            if len(approx) == 1:
                matched_entity = approx[0]

    if not matched_entity:
        return "", ()
    tools = entity_tools[matched_entity]
    if preferred_tool:
        return (matched_entity, (preferred_tool,)) if preferred_tool in tools else ("", ())
    return matched_entity, tools


def effect_value(val: object) -> object:
    """Coerce an arbitrary input value into ``RequestedEffect``'s strict scalar/ref-list domain."""
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


def normalize_field_items(raw: object) -> object:
    """Coerce either ``{field: value}`` or ``[{field, value}]`` into a normalized item list."""
    if not isinstance(raw, dict):
        return raw
    if not raw:
        return []
    converted = []
    for k, v in raw.items():
        if isinstance(v, list):
            v = [str(item) for item in v]
        converted.append({"field": str(k), "value": v})
    return converted


def _normalize_create_line(line: object) -> object:
    if not isinstance(line, dict):
        return line
    out_line = dict(line)
    if isinstance(out_line.get("fields"), dict):
        out_line["fields"] = normalize_field_items(out_line["fields"])
    if isinstance(out_line.get("sublines"), list):
        out_line["sublines"] = [_normalize_create_line(sub) for sub in out_line["sublines"]]
    return out_line


def normalize_create_arguments(args: dict) -> dict:
    """Allow models to pass ``fields`` and nested ``lines`` as item lists or dicts."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    if "fields" in out and isinstance(out.get("fields"), dict):
        out["fields"] = normalize_field_items(out["fields"])
    if isinstance(out.get("lines"), list):
        out["lines"] = [_normalize_create_line(item) for item in out["lines"]]
    return out


def normalize_update_arguments(args: dict) -> dict:
    """Normalize ``changes`` and optional nested ``line_changes[*].changes`` dicts to item lists."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    if isinstance(out.get("changes"), dict):
        out["changes"] = normalize_field_items(out["changes"])
    line_changes = out.get("line_changes")
    if isinstance(line_changes, list):
        norm_lines = []
        changed_lines = False
        for item in line_changes:
            if isinstance(item, dict) and isinstance(item.get("changes"), dict):
                norm_lines.append({**item, "changes": normalize_field_items(item["changes"])})
                changed_lines = True
            else:
                norm_lines.append(item)
        if changed_lines:
            out["line_changes"] = norm_lines
    return out


def normalize_changes_argument(args: dict) -> dict:
    """Allow models to pass ``changes`` either as ``[{field, value}]`` or ``{field: value}``."""
    return normalize_update_arguments(args)


@dataclass(frozen=True, slots=True)
class CompanionWriteSpec:
    """Declared secondary write triggered alongside a primary write operation."""

    target: str
    action: str
    source_method: str
    condition: str = ""
    readback_entity: str = ""
    readback_query_field: str = ""
    description: str = ""
    #: True when this companion deletes existing child rows and rebuilds them on trigger.
    destructive_delete: bool = False
    #: Execution trigger condition: ``always``, ``on_field_change``, ``on_field_present``, or ``on_remove``.
    trigger_mode: str = "always"
    #: Field keys whose modification/presence/removal activates this companion write.
    trigger_fields: tuple[str, ...] = ()
    #: Optional expected field key on readback rows used to verify non-trivial companion persistence.
    readback_expect_field: str = ""

    def __post_init__(self) -> None:
        if not self.target or not self.action or not self.source_method:
            raise ValueError("companion write spec requires target, action, and source_method")
        if self.trigger_mode not in {"always", "on_field_change", "on_field_present", "on_remove"}:
            raise ValueError(f"unsupported companion trigger_mode: {self.trigger_mode!r}")

    def format_card_text(self, *, triggered: bool = True, delete_count: int | None = None) -> str:
        if not triggered:
            parts = [f"目标：{self.target}", f"动作：{self.action}", f"来源：{self.source_method}"]
            if self.condition:
                parts.append(f"条件：{self.condition}")
            parts.append("本次未触发（未触及触发字段）")
            if self.description:
                parts.append(self.description)
            return "；".join(parts)
        parts: list[str] = []
        if self.destructive_delete:
            if delete_count is not None:
                parts.append(f"⚠️ 删除并重建子表（写前同账号已读取受影响 {delete_count} 条记录）")
            else:
                parts.append("⚠️ 删除并重建子表（须写前读取受影响记录数）")
        parts.extend([f"目标：{self.target}", f"动作：{self.action}", f"来源：{self.source_method}"])
        if self.condition:
            parts.append(f"条件：{self.condition}")
        if self.readback_entity:
            parts.append(f"写后同账号回读：{self.readback_entity}")
        else:
            parts.append("写后未核验（无同账号回读接口）")
        if self.description:
            parts.append(self.description)
        return "；".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "action": self.action,
            "source_method": self.source_method,
            "condition": self.condition,
            "readback_entity": self.readback_entity,
            "readback_query_field": self.readback_query_field,
            "description": self.description,
            "destructive_delete": self.destructive_delete,
            "trigger_mode": self.trigger_mode,
            "trigger_fields": list(self.trigger_fields),
            "readback_expect_field": self.readback_expect_field,
        }


@dataclass(frozen=True, slots=True)
class ValidationRuleSpec:
    """Declarative pre-card validation rule extracted from form/service constraints."""

    kind: Literal["paired_required", "date_order", "not_equal", "conditional_required", "at_least_one"]
    fields: tuple[str, ...]
    message: str
    condition_field: str = ""
    condition_values: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"paired_required", "date_order", "not_equal", "conditional_required", "at_least_one"}:
            raise ValueError(f"unsupported validation rule kind: {self.kind!r}")
        if not self.fields or not self.message:
            raise ValueError("validation rule requires fields and message")
        if self.kind == "date_order" and len(self.fields) != 2:
            raise ValueError("date_order rule requires exactly 2 fields")
        if self.kind == "not_equal" and len(self.fields) != 2:
            raise ValueError("not_equal rule requires exactly 2 fields")
        if self.kind == "conditional_required" and not self.condition_field:
            raise ValueError("conditional_required rule requires condition_field")


def _is_empty_rule_val(val: object) -> bool:
    if val is None or val == "":
        return True
    if isinstance(val, (list, tuple, dict, set)) and not val:
        return True
    return False


def _sortable_date_value(val: object) -> tuple[int, int | str] | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return (0, int(val))
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return (0, int(datetime.strptime(cleaned, fmt).timestamp() * 1000))
            except ValueError:
                continue
        return (1, cleaned)
    return None


def _cond_value_matches(actual: object, expected: object) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if actual is None or expected is None:
        return actual is expected
    return same_value(actual, expected) or str(actual).strip() == str(expected).strip()


def evaluate_validation_rules(
    rules: tuple[ValidationRuleSpec, ...],
    payload: dict[str, object],
    field_lookup: Callable[[str], "ConventionFieldSpec | None"] | None = None,
) -> list[str]:
    """Evaluate pre-card declarative rules against assembled payload and return human errors."""
    if not rules:
        return []

    def _lbl(fname: str) -> str:
        f_sp = field_lookup(fname) if callable(field_lookup) else None
        if f_sp is not None and f_sp.label:
            return f"{f_sp.label}（{f_sp.name}）"
        return fname

    errors: list[str] = []
    for rule in rules:
        if rule.kind == "paired_required":
            present = [f for f in rule.fields if not _is_empty_rule_val(payload.get(f))]
            if 0 < len(present) < len(rule.fields):
                missing = "、".join(_lbl(f) for f in rule.fields if _is_empty_rule_val(payload.get(f)))
                errors.append(f"{rule.message}（请补充：{missing}）")
        elif rule.kind == "date_order":
            s_val = _sortable_date_value(payload.get(rule.fields[0]))
            e_val = _sortable_date_value(payload.get(rule.fields[1]))
            if s_val is not None and e_val is not None and s_val[0] == e_val[0] and e_val[1] < s_val[1]:
                errors.append(rule.message)
        elif rule.kind == "not_equal":
            v1, v2 = payload.get(rule.fields[0]), payload.get(rule.fields[1])
            if not _is_empty_rule_val(v1) and not _is_empty_rule_val(v2) and _cond_value_matches(v1, v2):
                errors.append(rule.message)
        elif rule.kind == "conditional_required":
            cval = payload.get(rule.condition_field)
            if rule.condition_values:
                triggered = any(_cond_value_matches(cval, cv) for cv in rule.condition_values)
            else:
                triggered = bool(cval) and not _is_empty_rule_val(cval)
            if triggered:
                missing_list = [_lbl(f) for f in rule.fields if _is_empty_rule_val(payload.get(f))]
                if missing_list:
                    errors.append(f"{rule.message}（缺少：{'、'.join(missing_list)}）")
        elif rule.kind == "at_least_one":
            if all(_is_empty_rule_val(payload.get(f)) for f in rule.fields):
                labels = "、".join(_lbl(f) for f in rule.fields)
                errors.append(f"{rule.message}（至少填写一项：{labels}）")
    return list(dict.fromkeys(errors))


def serialize_companion_specs(
    side_effects: tuple[CompanionWriteSpec | str, ...],
    *,
    evaluated: list[tuple[CompanionWriteSpec | str, bool, int | None]] | None = None,
) -> list[dict[str, object]]:
    """Serialize companion write specs to JSON-safe dicts for dispatch verification."""
    items = evaluated if evaluated is not None else [(item, True, None) for item in (side_effects or ())]
    out: list[dict[str, object]] = []
    for item, triggered, delete_count in items:
        if isinstance(item, CompanionWriteSpec):
            entry = item.to_dict()
            entry["triggered"] = triggered
            if delete_count is not None:
                entry["expected_delete_count"] = delete_count
            out.append(entry)
        elif isinstance(item, str) and item.strip():
            out.append({
                "target": item.strip(),
                "action": "伴生写入",
                "source_method": "declared",
                "condition": "",
                "readback_entity": "",
                "readback_query_field": "",
                "description": "",
                "destructive_delete": False,
                "trigger_mode": "always",
                "trigger_fields": [],
                "readback_expect_field": "",
                "triggered": triggered,
            })
    return out


def format_companion_details(
    side_effects: tuple[CompanionWriteSpec | str, ...],
    *,
    evaluated: list[tuple[CompanionWriteSpec | str, bool, int | None]] | None = None,
) -> list[ApprovalDetail]:
    """Render each declared companion write onto the visible approval card."""
    items = evaluated if evaluated is not None else [(item, True, None) for item in (side_effects or ())]
    out: list[ApprovalDetail] = []
    for item, triggered, delete_count in items:
        if isinstance(item, CompanionWriteSpec):
            lbl = f"伴生写入（{item.target}）"[:200]
            out.append(side_effect_detail(lbl, item.format_card_text(triggered=triggered, delete_count=delete_count)))
        elif isinstance(item, str) and item.strip():
            out.append(side_effect_detail("伴生写入", item.strip()))
    return out


def _companion_is_triggered(
    item: CompanionWriteSpec,
    port: object,
    spec: "ConventionUpdateSpec",
    payload: dict,
    *,
    before_record: dict,
    changed: dict,
) -> bool:
    mode = item.trigger_mode or "always"
    if mode == "always":
        return True
    if not item.trigger_fields:
        return True
    if mode == "on_field_change":
        return any(tf in changed for tf in item.trigger_fields)
    if mode == "on_field_present":
        return any(not _is_empty_rule_val(payload.get(tf)) for tf in item.trigger_fields)
    if mode == "on_remove":
        for tf in item.trigger_fields:
            f_sp = spec.resolve_field(tf)
            b_val = (
                extract_projected_field_value(port, spec, f_sp, before_record)
                if f_sp is not None
                else before_record.get(tf)
            )
            if b_val is _MISSING or b_val is None:
                b_list: list[object] = []
            elif isinstance(b_val, (list, tuple, set)):
                b_list = list(b_val)
            else:
                b_list = [b_val]
            a_val = payload.get(tf)
            a_list = (
                list(a_val)
                if isinstance(a_val, (list, tuple, set))
                else ([] if a_val is None else [a_val])
            )
            if {str(x) for x in b_list} - {str(x) for x in a_list}:
                return True
        return False
    return True


def evaluate_update_companions(
    port: object,
    spec: "ConventionUpdateSpec",
    record_id: object,
    payload: dict,
    *,
    before_record: dict,
    changed: dict,
) -> list[tuple[CompanionWriteSpec | str, bool, int | None]]:
    """Evaluate companion write triggers and enforce pre-dispatch child row count reads on destructive deletes."""
    evaluated: list[tuple[CompanionWriteSpec | str, bool, int | None]] = []
    has_cap = "companion_readback" in write_capabilities_of(port)
    count_fn = getattr(port, "count_companion_rows", None) if has_cap else None
    for item in spec.side_effects or ():
        if not isinstance(item, CompanionWriteSpec):
            if isinstance(item, str) and item.strip():
                evaluated.append((item, True, None))
            continue
        triggered = _companion_is_triggered(
            item,
            port,
            spec,
            payload,
            before_record=before_record,
            changed=changed,
        )
        delete_count: int | None = None
        if triggered and item.destructive_delete:
            if not callable(count_fn):
                raise BusinessToolRequestError(
                    f"修改「{spec.label}」会触发对「{item.target}」的删除并重建，"
                    "但当前端口未声明同账号写前子表行数读取能力（companion_readback），"
                    "无法核验受影响记录数；请改用页面表单确认或仅修改不触发重建的字段。"
                )
            delete_count = count_fn(
                spec,
                record_id,
                payload,
                before_record=before_record,
                side_effect=item.to_dict(),
                changed=changed,
            )
            if delete_count is None or isinstance(delete_count, bool) or not isinstance(delete_count, int) or delete_count < 0:
                raise BusinessToolRequestError(
                    f"修改「{spec.label}」会触发对「{item.target}」的删除并重建，"
                    f"但当前账号无法写前读取受影响子表记录数（{item.readback_entity or '无只读子表接口'}），"
                    "已按 fail-closed 拦截；请改用页面表单确认或仅修改不触发重建的字段。"
                )
        evaluated.append((item, triggered, delete_count))
    return evaluated


def run_companion_readback(
    port: object,
    spec: object,
    record_id: object,
    payload: dict,
    *,
    before_record: dict | None,
    after_record: dict,
    side_effects: list[dict] | tuple,
) -> list[dict]:
    """Verify declared companion writes via capability-gated port readback or mark as unverified."""
    if not side_effects:
        return []
    pending: list[object] = []
    results: list[dict] = []
    for se in side_effects:
        if isinstance(se, dict) and se.get("triggered") is False:
            results.append({
                "effect": "companion_write",
                "target": se.get("target", ""),
                "verified": True,
                "not_triggered": True,
                "note": "本次修改未触及触发字段，未执行伴生写入",
            })
        else:
            pending.append(se)
    if not pending:
        return results
    verify_companion_fn = (
        getattr(port, "verify_companion_readback", None)
        if "companion_readback" in write_capabilities_of(port)
        else None
    )
    if callable(verify_companion_fn):
        results.extend(
            verify_companion_fn(
                spec,
                record_id,
                payload,
                before_record=before_record,
                after_record=after_record,
                side_effects=pending,
            )
            or ()
        )
        return results
    results.extend(
        {
            "effect": "companion_write",
            "target": se.get("target", "") if isinstance(se, dict) else str(se),
            "verified": False,
            "note": "写后未核验（未声明或无同账号回读接口）",
        }
        for se in pending
    )
    return results


def resolve_spec_field(fields: tuple["ConventionFieldSpec", ...], token: str) -> "ConventionFieldSpec | None":
    """Resolve a field by exact key, case-insensitive key, exact label, or unit-stripped label."""
    cleaned = token.strip().rstrip(":：").strip()
    if not cleaned:
        return None
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
    stripped_tok = _STRIP_LABEL_UNIT_RE.sub("", cleaned).strip()
    if stripped_tok:
        by_stripped = [
            f for f in fields
            if f.label and _STRIP_LABEL_UNIT_RE.sub("", f.label.strip().rstrip(":：").strip()).strip() == stripped_tok
        ]
        if len(by_stripped) == 1:
            return by_stripped[0]
    by_label_case = [
        f for f in fields if f.label and f.label.strip().rstrip(":：").strip().casefold() == folded
    ]
    if len(by_label_case) == 1:
        return by_label_case[0]
    return None


@dataclass(frozen=True, slots=True)
class ConventionFieldSpec:
    """One flat top-level field verified to round-trip 1:1 between update and readback."""

    name: str
    kind: str
    label: str = ""
    required: bool = False
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    enum_type: str = ""
    reference_entity: str = ""
    #: Multiplicative integer scale from human unit to stored integer (e.g. 100 for cents/fen).
    unit_scale: int = 1
    display_unit: str = ""
    #: Optional server timestamp representation ("epoch_ms", "datetime", "date").
    date_kind: str = ""
    #: True when this field links a tree node to its parent node (e.g., hierarchy parent ID).
    is_hierarchy_parent: bool = False
    #: Optional ``(detail_collection_field, item_id_key)`` for 1:N projected ID arrays.
    readback_projection: tuple[str, str] = ()
    #: Optional server-computed display columns derived from this field (e.g. keeperName from keeperUserId).
    derived_display_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", self.name):
            raise ValueError(f"invalid convention field name: {self.name!r}")
        if _SECRET_FIELD.search(self.name) or (self.label and _SECRET_FIELD.search(self.label)):
            raise ValueError(f"credential/secret field forbidden in convention updates: {self.name}")
        if self.kind not in _FIELD_KINDS:
            raise ValueError(f"unsupported convention field kind: {self.kind!r}")
        if type(self.unit_scale) is not int or self.unit_scale < 1:
            raise ValueError("unit_scale must be a positive integer")
        if self.readback_projection and (
            len(self.readback_projection) != 2 or not all(isinstance(x, str) and x for x in self.readback_projection)
        ):
            raise ValueError(f"invalid readback_projection: {self.readback_projection!r}")
        if self.derived_display_fields and not all(
            isinstance(x, str) and bool(x.strip()) for x in self.derived_display_fields
        ):
            raise ValueError(f"invalid derived_display_fields: {self.derived_display_fields!r}")


@dataclass(frozen=True, slots=True)
class ConventionUpdateSpec:
    """Flat symmetric update contract for one entity."""

    entity: str
    label: str
    fields: tuple[ConventionFieldSpec, ...]
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    volatile_fields: frozenset[str] = frozenset()
    side_effects: tuple[CompanionWriteSpec | str, ...] = ()
    validation_rules: tuple[ValidationRuleSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.entity or not self.fields:
            raise ValueError("convention update spec requires entity and fields")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names) or self.id_field in names:
            raise ValueError(f"duplicate or id field in convention update spec: {self.entity}")

    def resolve_field(self, token: str) -> ConventionFieldSpec | None:
        return resolve_spec_field(self.fields, token)


@dataclass(frozen=True, slots=True)
class TwoStateStatusSpec:
    """Reversible two-state status toggle contract for one entity."""

    entity: str
    label: str
    states: tuple[tuple[str, int | str | bool], tuple[str, int | str | bool]]
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    status_field: str = "status"
    volatile_fields: frozenset[str] = frozenset()
    synonyms: tuple[tuple[str, str], ...] = ()
    action_path: str = ""
    second_action_path: str = ""
    param_mode: str = "body"
    id_param: str = "id"
    status_param: str = "status"
    read_path: str = ""
    read_list_path: str = ""
    side_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.entity or len(self.states) != 2:
            raise ValueError("two-state status spec requires entity and exactly 2 states")
        if self.param_mode not in {"body", "query"}:
            raise ValueError(f"unsupported status param_mode: {self.param_mode!r}")
        (l1, c1), (l2, c2) = self.states
        if not l1 or not l2 or l1 == l2 or (type(c1) is type(c2) and c1 == c2):
            raise ValueError(f"two-state status spec states must be distinct: {self.entity}")

    def resolve_state(self, label: str) -> tuple[str, int | str | bool] | None:
        wanted = label.strip()
        alias_map = dict(self.synonyms)
        canonical_label = alias_map.get(wanted, wanted)
        for state_label, code in self.states:
            if state_label == canonical_label:
                return state_label, code
        return None

    def label_for_code(self, code) -> str | None:
        for state_label, state_code in self.states:
            if type(state_code) is type(code) and state_code == code:
                return state_label
            if not isinstance(state_code, bool) and not isinstance(code, bool) and str(state_code) == str(code):
                return state_label
        return None


#: Optional write-port capabilities declared via ``ConventionWritePort.capabilities()``.
WRITE_PORT_OPTIONAL = frozenset({
    "companion_readback",
    "preserved_field_extraction",
    "reference_variants",
})


def write_capabilities_of(port: object) -> frozenset[str]:
    """Optional write-port capabilities declared via ``port.capabilities()``, bounded to ``WRITE_PORT_OPTIONAL``."""
    method = getattr(port, "capabilities", None)
    if not callable(method):
        return frozenset()
    return frozenset(str(name) for name in (method() or ())) & WRITE_PORT_OPTIONAL


class ConventionWritePort(Protocol):
    """Adapter-supplied runtime behavior for flat convention updates and two-state toggles.

    Optional hooks (``verify_companion_readback`` / ``count_companion_rows``,
    ``extract_preserved_field``, ``reference_provenance_variants``) are activated
    only when declared in ``capabilities()`` (a subset of ``WRITE_PORT_OPTIONAL``).
    """

    def capabilities(self) -> frozenset[str]: ...
    def update_specs(self) -> dict[str, ConventionUpdateSpec]: ...
    def status_specs(self) -> dict[str, TwoStateStatusSpec]: ...
    def verify_actor(self, context) -> str: ...
    def resolve_target(self, entity: str, target: str, *, spec) -> dict: ...
    def read_record(self, entity: str, record_id) -> dict | None: ...
    def encode_field(
        self,
        spec: ConventionUpdateSpec,
        field: ConventionFieldSpec,
        raw_value,
        current_record: dict,
    ) -> tuple[object, str, str, dict | None]:
        """Return ``(api_value, old_display, new_display, optional_recheck_binding)``."""
        ...

    def recheck_bindings(self, bindings: list[dict], *, actor_id: str) -> None: ...
    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None: ...
    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None: ...
    def invalidate_reads(self, entities: frozenset[str]) -> None: ...


def _record_display_name(record: dict, name_fields: tuple[str, ...], id_field: str) -> str:
    for key in name_fields:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return str(record.get(id_field, ""))


def _non_volatile_snapshot(record: dict, volatile: frozenset[str]) -> dict:
    return non_volatile_snapshot(record, volatile, secret_pattern=_SECRET_FIELD)


def _restricted_snapshot_digests(record: dict, volatile: frozenset[str]) -> dict[str, str]:
    """Digest excluded sensitive/config fields by hashed field name without exposing keys or values."""
    return restricted_snapshot_digests(record, volatile, secret_pattern=_SECRET_FIELD)


def _values_equal(left, right, *, kind: str = "") -> bool:
    if kind in {"id_array", "string_array"}:
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and [str(x) for x in left] == [str(x) for x in right]
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return same_value(left, right)


def extract_projected_field_value(port, spec, f_spec: ConventionFieldSpec, record: dict):
    """Read a flat or 1:N-projected field value from a target record, returning ``_MISSING`` if absent."""
    if not isinstance(record, dict):
        return _MISSING
    if f_spec.name in record:
        val = record[f_spec.name]
        if val is not None or not f_spec.readback_projection:
            return val
    extract_fn = (
        getattr(port, "extract_preserved_field", None)
        if "preserved_field_extraction" in write_capabilities_of(port)
        else None
    )
    if callable(extract_fn):
        custom = extract_fn(spec, f_spec, record)
        if custom is not _MISSING and custom is not None:
            return custom
    if f_spec.readback_projection:
        col_name, item_key = f_spec.readback_projection
        if col_name in record and isinstance(record[col_name], list):
            return [
                row[item_key]
                for row in record[col_name]
                if isinstance(row, dict) and row.get(item_key) is not None
            ]
    return _MISSING


def _snapshot_preserved(before: dict, after: dict, *, ignore: frozenset[str]) -> list[str]:
    return verify_preservation_snapshot(before, after, ignore=ignore, values_equal=_values_equal)


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


def _entry_provenance_ok(
    entry: Entry,
    scoped_cands: list[tuple[str, str]],
    texts: tuple[str, ...] | list[str],
    *,
    receipts: tuple[str, ...] = (),
) -> bool:
    """Validate one field entry against user texts, scoped candidates, and turn receipts."""
    return verify_entry_provenance(entry, scoped_cands, texts, receipts=receipts)


def collect_provenance_context(
    existing_values: dict[str, tuple[str, ...]] | None,
) -> tuple[SourceLedger, tuple[str, ...]]:
    """Populate a ``SourceLedger`` and receipt strings from ``existing_values``."""
    ledger = SourceLedger()
    receipt_vals: tuple[str, ...] = ()
    if existing_values:
        receipt_vals = tuple(
            str(v).strip() for v in existing_values.get("__receipt__", ())
            if isinstance(v, str) and v.strip()
        )
        for binding, vals in existing_values.items():
            if binding == "__receipt__":
                continue
            for v in vals:
                ledger.add_candidates([(binding, v)])
    return ledger, receipt_vals


_PAREN_REF_RE = re.compile(r"^(.+?)\s*[（(]([^()（）]+)[）)]\s*$")
_ORDINAL_STAGE_RE = re.compile(r"^第?\s*([0-9]+|[零〇一二两三四五六七八九十百千万]+)\s*([期批轮次])$")
_TEXT_ORDINAL_RE = re.compile(r"第\s*([0-9]+|[零〇一二两三四五六七八九十百千万]+)\s*([期批轮次])")


def _parse_ordinal_int(token: str) -> int | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return cn_number(raw)


def _reference_token_variants(token: str) -> set[str]:
    """Expand safe ID/label, parenthesized display, and ordinal period forms for FK matching."""
    out: set[str] = set()
    queue: list[str] = [str(token or "").strip()]
    while queue:
        cur = queue.pop().strip()
        if not cur or cur in out:
            continue
        out.add(cur)
        if "/" in cur:
            leaf = cur.rsplit("/", 1)[-1].strip()
            if leaf and leaf not in out:
                queue.append(leaf)
        m_paren = _PAREN_REF_RE.match(cur)
        if m_paren is not None:
            for part in (m_paren.group(1).strip(), m_paren.group(2).strip()):
                if part and part not in out:
                    queue.append(part)
        m_ord = _ORDINAL_STAGE_RE.match(cur)
        if m_ord is not None:
            num = _parse_ordinal_int(m_ord.group(1))
            unit = m_ord.group(2)
            if num is not None and num > 0:
                out.add(str(num))
                out.add(f"第{num}{unit}")
                out.add(f"{num}{unit}")
    return out


def _boolean_field_stems(f_spec: ConventionFieldSpec) -> tuple[str, ...]:
    """Derive clean semantic stems from a boolean field label/name without matching bare 是否 prefixes."""
    raw_candidates: list[str] = []
    if f_spec.label:
        raw_candidates.extend(re.split(r"[（(\[/\s,，;；:：]+", f_spec.label))
    if f_spec.name:
        raw_candidates.append(f_spec.name)
    stems: list[str] = []
    skip_generic = {"true", "false", "boolean", "bool", "status", "flag", "state", "状态", "开关", "标识", "标志"}
    for raw in raw_candidates:
        cleaned = str(raw or "").strip()
        if not cleaned:
            continue
        stripped = re.sub(r"^(?:是否|能否|可否|有无)", "", cleaned)
        stripped = re.sub(r"(?:状态|开关|标识|标志)$", "", stripped).strip()
        cands = (stripped,) if (cleaned != stripped and stripped) else (cleaned,)
        for cand in cands:
            if not cand or cand.lower() in skip_generic:
                continue
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", cand))
            if (has_cjk and len(cand) >= 2) or (re.fullmatch(r"[A-Za-z0-9_-]{3,}", cand)):
                if cand not in stems:
                    stems.append(cand)
    stems.sort(key=len, reverse=True)
    return tuple(stems)


def _infer_boolean_intent(raw_val: object, stems: tuple[str, ...]) -> bool | None:
    if isinstance(raw_val, bool):
        return raw_val
    s = str(raw_val or "").strip()
    sl = s.lower()
    if sl in {"true", "是", "开启", "启用", "打开", "允许", "设为默认"}:
        return True
    if sl in {"false", "否", "关闭", "停用", "禁用", "取消", "不启用", "不允许"}:
        return False
    if not stems:
        return None
    norm = re.sub(r"^(?:设为|改为|置为|变成|选为)\s*", "", s)
    neg_head = re.compile(
        r"^(?:不允许|不可|不能|不要|不得|不用|不准|不予|禁止|严禁|拒绝|取消|关闭|停用|禁用|未|免|非|别|勿|不|not\s+|no\s+|non-?)(?:启用|开启|设为|做)?\s*",
        re.I,
    )
    m_neg = neg_head.match(norm)
    if m_neg:
        rest = norm[m_neg.end():].strip()
        if any(rest.casefold() == st.casefold() for st in stems):
            return False
    pos_head = re.sub(r"^(?:允许|启用|开启|要)\s*", "", norm).strip()
    if any(pos_head.casefold() == st.casefold() for st in stems):
        return True
    return None


def _stem_pattern(stem: str) -> str:
    esc = re.escape(stem)
    if re.fullmatch(r"[A-Za-z0-9_-]+", stem):
        return rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])"
    return esc


def validate_single_field_provenance(
    f_spec: ConventionFieldSpec,
    raw_val: object,
    *,
    old_val: object = None,
    texts: tuple[str, ...],
    prompt: str,
    joined_words: str,
    ledger: SourceLedger,
    receipt_vals: tuple[str, ...],
    existing_values: dict[str, tuple[str, ...]] | None,
    allow_append: bool = True,
    allow_existing_candidates: bool = True,
    allow_fk_candidates: bool = True,
    enum_tokens_fn=None,
    reference_variants_fn=None,
) -> None:
    """Validate provenance for one field input against user texts, scoped candidates, and receipts."""
    label = f_spec.label or f_spec.name

    # 1. Check append overwrite protection on strings (including clearing to None)
    if allow_append and (isinstance(raw_val, str) or raw_val is None):
        val_for_append = raw_val if isinstance(raw_val, str) else ""
        require_append_preservation(
            prompt,
            (((label, f_spec.name), old_val, val_for_append),),
            error_factory=lambda _overwritten, _lbl=label: BusinessToolRequestError(
                f"用户要求在「{_lbl}」中追加或补充内容，当前修改覆盖或清空了原有内容；"
                "请先读取并保留目标记录该字段原值，再在其后追加本次内容。"
            ),
        )

    field_cands = (
        {
            *existing_values.get(label, ()),
            *existing_values.get(f_spec.name, ()),
        }
        if allow_existing_candidates and existing_values else set()
    )
    scoped_cands = [
        (cand.binding, cand.value) for cand in ledger.candidates
        if (
            (allow_existing_candidates and cand.binding in {label, f_spec.name})
            or (f_spec.reference_entity and (allow_fk_candidates or allow_existing_candidates) and cand.binding == f_spec.reference_entity)
            or (allow_existing_candidates and cand.binding and cand.binding.endswith("." + f_spec.name))
        )
    ]

    # 2. Boolean fields
    if f_spec.kind == "boolean":
        if raw_val is not None:
            if str(raw_val) in field_cands:
                return
            stems = _boolean_field_stems(f_spec)
            bool_intent = _infer_boolean_intent(raw_val, stems)
            if bool_intent is not None:
                neg_prefix = re.compile(
                    r"(?:不允许|不可|不能|不要|不得|不用|不准|不予|禁止|严禁|拒绝|取消|别|勿)|"
                    r"(?:关闭|停用|禁用|未|免|非|不|\bnot|\bno|\bnon-?)\s*$",
                    re.I,
                )
                pos_pat = re.compile(
                    r"(?<![A-Za-z0-9_])true(?![A-Za-z0-9_])|开启|启用|打开|允许|设为默认|"
                    r"(?:设为|改为|置为|变成|选为|[：:=]\s*)是(?=$|[\s）)])",
                    re.I,
                )
                neg_pat = re.compile(
                    r"(?<![A-Za-z0-9_])false(?![A-Za-z0-9_])|关闭|停用|禁用|取消默认|取消启用|"
                    r"(?:设为|改为|置为|变成|选为|[：:=]\s*)否(?=$|[\s）)])",
                    re.I,
                )
                stem_pat = (
                    re.compile("|".join(_stem_pattern(st) for st in stems), re.I)
                    if stems
                    else None
                )
                positive_bool = False
                negative_bool = False
                for clause in re.split(r"[，,。；;！!\n]+", joined_words):
                    for m in pos_pat.finditer(clause):
                        if neg_prefix.search(clause[:m.start()]):
                            negative_bool = True
                        else:
                            positive_bool = True
                    for m in neg_pat.finditer(clause):
                        if not neg_prefix.search(clause[:m.start()]):
                            negative_bool = True
                    if stem_pat is not None:
                        for m in stem_pat.finditer(clause):
                            prefix = clause[:m.start()].rstrip()
                            if prefix.endswith(("是否", "能否", "可否", "有无")):
                                continue
                            if neg_prefix.search(prefix):
                                negative_bool = True
                            else:
                                positive_bool = True
                if (bool_intent is True and positive_bool) or (bool_intent is False and negative_bool):
                    return
            raise BusinessToolRequestError(
                f"布尔字段「{label}」的值「{raw_val}」未在用户原话中找到依据。"
            )
        return

    # 3. Reference fields (single FK or ID array)
    if f_spec.reference_entity and raw_val is not None:
        tokens = raw_val if isinstance(raw_val, list) else [raw_val]
        text_ordinals: set[str] = set()
        for t in texts:
            for m_ord in _TEXT_ORDINAL_RE.finditer(str(t or "")):
                num = _parse_ordinal_int(m_ord.group(1))
                unit = m_ord.group(2)
                if num is not None and num > 0:
                    text_ordinals.add(str(num))
                    text_ordinals.add(f"第{num}{unit}")
                    text_ordinals.add(f"{num}{unit}")
        cand_variants: set[str] = set()
        cand_exact: set[str] = set()
        for _, c_val in scoped_cands:
            s_cand = str(c_val or "").strip()
            if not s_cand:
                continue
            cand_exact.add(s_cand)
            cand_variants.update(_reference_token_variants(s_cand))
            if callable(reference_variants_fn):
                cand_variants.update(str(v).strip() for v in (reference_variants_fn(f_spec, s_cand) or ()) if str(v).strip())
        for tok in tokens:
            s_tok = str(tok).strip()
            leaf = s_tok.rsplit("/", 1)[-1].strip()
            if s_tok == "根节点" and re.search(r"根|顶层|顶级|一级", joined_words):
                continue
            if (literal_supplied(s_tok, texts)
                    or (leaf and literal_supplied(leaf, texts))
                    or any(c_val in {s_tok, leaf} for _, c_val in scoped_cands)):
                continue
            tok_variants = _reference_token_variants(s_tok)
            if callable(reference_variants_fn):
                tok_variants.update(str(v).strip() for v in (reference_variants_fn(f_spec, s_tok) or ()) if str(v).strip())
            if tok_variants & cand_variants:
                continue
            if any(variant in cand_exact or literal_supplied(variant, texts) for variant in tok_variants):
                continue
            if tok_variants & text_ordinals and (cand_variants or _ORDINAL_STAGE_RE.match(s_tok)):
                continue
            raise BusinessToolRequestError(
                f"关联字段「{label}」的目标「{s_tok[:60]}」未在用户原话或已读候选中找到依据。"
            )
        return

    # 4. Non-reference ID / string arrays
    if f_spec.kind in {"id_array", "string_array"} and raw_val is not None:
        if not isinstance(raw_val, list):
            raise BusinessToolRequestError(f"数组字段「{label}」须传数组。")
        for elem in raw_val:
            s_elem = scalar_text(elem).strip()
            if not s_elem:
                continue
            if literal_supplied(s_elem, texts):
                continue
            if not _entry_provenance_ok(Entry(label, s_elem, kind="text"), scoped_cands, texts):
                raise BusinessToolRequestError(
                    f"字段「{label}」的元素「{s_elem[:60]}」未在用户原话或已读候选中找到依据。"
                )
        return

    # 5. Dictionary enum fields
    if f_spec.enum_type and raw_val is not None:
        s_tok = str(raw_val).strip()
        synonyms = {
            "开启": ("开启", "启用", "正常", "有效"),
            "关闭": ("关闭", "停用", "禁用"),
        }
        accepted_list: list[str] = [s_tok, *synonyms.get(s_tok, ())]
        if callable(enum_tokens_fn):
            for tok in enum_tokens_fn(f_spec, raw_val) or ():
                tok_s = str(tok).strip()
                if tok_s:
                    accepted_list.append(tok_s)
                    accepted_list.extend(synonyms.get(tok_s, ()))
        accepted = tuple(dict.fromkeys(accepted_list))
        if any(literal_supplied(w, texts) for w in accepted) or any(w in field_cands for w in accepted):
            return
        raise BusinessToolRequestError(
            f"枚举字段「{label}」的选项「{s_tok[:60]}」未在用户原话中找到依据。"
        )

    # 6. Scaled integer fields (e.g. unit_scale=100 fen, unit_scale=10 zhe)
    if f_spec.unit_scale > 1 and raw_val is not None:
        _verify_scaled_number_provenance(f_spec, raw_val, texts, receipts=receipt_vals)
        return

    # 7. Date / datetime inputs (both string dates and integer epoch-ms timestamps)
    if f_spec.date_kind and raw_val is not None and not isinstance(raw_val, bool):
        val_str = raw_val.strip() if isinstance(raw_val, str) else scalar_text(raw_val).strip()
        if val_str:
            if not _entry_provenance_ok(Entry(label, val_str, kind="text"), scoped_cands, texts, receipts=receipt_vals):
                raise BusinessToolRequestError(
                    f"字段「{label}」的日期时间值「{val_str}」未在用户原话或本轮时间换算中找到依据，请向用户确认。"
                )
            return

    # 8. Plain strings: literal in user text, safe append over old_val, or scoped candidate
    if f_spec.kind == "string" and isinstance(raw_val, str) and raw_val:
        if literal_supplied(raw_val, texts):
            return
        if allow_append:
            allowed_old = (old_val,) if isinstance(old_val, str) and old_val else ()
            unquoted_prompt = re.sub(
                r"「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'",
                " ",
                str(prompt or ""),
            )
            if isinstance(old_val, str) and old_val:
                unquoted_prompt = unquoted_prompt.replace(old_val, " ")
            has_overwrite_verb = bool(
                re.search(r"改成|改为|设为|设置为|替换为?|覆盖为?|重设为|更新为|\breplace\b", unquoted_prompt, re.I)
            )
            has_append_verb = bool(
                re.search(r"追加|补充|附上|加上|续写|后面加|末尾加|在原.*基础上|\bappend\b", unquoted_prompt, re.I)
            )
            if has_overwrite_verb and not has_append_verb:
                if _existing_append_supplied(raw_val, texts, allowed_old):
                    raise BusinessToolRequestError(
                        f"用户本轮要求将字段「{label}」直接改为指定新内容，请只传本次要求的新值，不要拼接原有旧值前缀。"
                    )
            elif _existing_append_supplied(raw_val, texts, allowed_old):
                return
        if allow_existing_candidates and raw_val in field_cands and re.search(r"同上|一样|相同|沿用|复制|照旧", joined_words):
            return
        raise BusinessToolRequestError(
            f"字段「{label}」的值「{raw_val[:60]}」不是用户原话提供的内容（若为追加请保留原值前缀），不能自行编造或改写。"
        )

    # 9. Numbers / integers (unit_scale == 1)
    if f_spec.kind in {"integer", "number"} and raw_val is not None and not isinstance(raw_val, bool):
        if not _entry_provenance_ok(Entry(label, scalar_text(raw_val), kind="text"), scoped_cands, texts, receipts=receipt_vals):
            raise BusinessToolRequestError(
                f"字段「{label}」的数值「{raw_val}」未在用户原话或本轮 SQL 计算回执中找到依据"
                "（若该数值由本轮已读原值与用户给定的增减额/比例计算得出，请先调用 sql 执行算式如 "
                "SELECT 原值 + 变动值 留存计算回执后再重新调用写工具；若确系用户未提供的数值，请用 need_input 向用户确认）。"
            )


class ConventionWriteEffects:
    def __init__(self, tool_name: str, port: ConventionWritePort):
        self.tool_name = tool_name
        self.port = port

    def project_inputs(self, inputs: dict) -> RequestedEffect:
        entity = str(inputs["entity"]).strip().strip("/")
        target = str(inputs["target"]).strip()
        if self.tool_name == "toggle_record_status":
            spec = self.port.status_specs().get(entity)
            resolved = spec.resolve_state(str(inputs["status"])) if spec is not None else None
            label = resolved[0] if resolved is not None else str(inputs["status"]).strip()
            field_name = spec.status_field if spec is not None else "status"
            return RequestedEffect(entity, "state_change", entity, target, canonical({field_name: label}))
        spec = self.port.update_specs().get(entity)
        mapped: dict[str, object] = {}
        for item in inputs.get("changes") or ():
            raw_field = str(item["field"]).strip()
            f_spec = spec.resolve_field(raw_field) if spec is not None else None
            key = f_spec.name if f_spec is not None else raw_field
            mapped[key] = effect_value(item["value"])
        return RequestedEffect(entity, "update", entity, target, canonical(mapped))


class ConventionUpdateHandler:
    """Shared prepare / provenance-check / reread / single-dispatch / full-readback handler."""

    def __init__(self, port: ConventionWritePort):
        self.port = port

    def execute(self, args: dict, *, context=None) -> BusinessToolResult:
        raise BusinessToolRequestError("update_record is a write proposal tool")

    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec:
        actor_id = self.port.verify_actor(context)
        entity = str(args.get("entity") or "").strip().strip("/")
        specs = self.port.update_specs()
        spec = specs.get(entity)
        cap = resolve_write_route_capability(self.port)
        if spec is None:
            dedicated_fn = getattr(self.port, "dedicated_tool_for", None)
            dedicated = dedicated_fn(entity, "update") if callable(dedicated_fn) else None
            raise BusinessToolRequestError(cap.unsupported_entity_message(entity, dedicated=dedicated))
        raw_changes = args.get("changes") or ()
        if not raw_changes:
            raise BusinessToolRequestError("changes 不能为空，请提供用户明确要求修改的字段。")
        record = self.port.resolve_target(entity, str(args["target"]).strip(), spec=spec)
        record_id = record.get(spec.id_field)
        if record_id is None or record_id == "":
            raise BusinessToolRequestError("目标记录缺少唯一主键，无法执行约定修改。")

        seen_fields: set[str] = set()
        encoded_updates: dict[str, object] = {}
        input_projection: dict[str, object] = {}
        provenance_items: list[dict] = []
        bindings: list[dict] = []
        details: list[ApprovalDetail] = []
        match_fn = getattr(self.port, "matches_readback_value", None)

        for item in raw_changes:
            raw_field = str(item.get("field") or "").strip()
            f_spec = spec.resolve_field(raw_field)
            if f_spec is None:
                supported = "、".join(
                    f"{f.label}（{f.name}）" if f.label else f.name for f in spec.fields[:25]
                )
                raise BusinessToolRequestError(
                    cap.unsupported_field_message(entity, raw_field, supported)
                )
            if f_spec.name in seen_fields:
                raise BusinessToolRequestError(f"字段「{f_spec.name}」在同一次修改中重复出现。")
            seen_fields.add(f_spec.name)
            cur_val = extract_projected_field_value(self.port, spec, f_spec, record)
            encode_rec = (
                {**record, f_spec.name: cur_val}
                if cur_val is not _MISSING and f_spec.name not in record
                else record
            )
            api_val, old_disp, new_disp, binding = self.port.encode_field(
                spec, f_spec, item.get("value"), encode_rec
            )
            if isinstance(api_val, (int, float)) and not isinstance(api_val, bool):
                if f_spec.minimum is not None and api_val < f_spec.minimum:
                    raise BusinessToolRequestError(f"字段「{f_spec.label or f_spec.name}」不能小于 {f_spec.minimum}。")
                if f_spec.maximum is not None and api_val > f_spec.maximum:
                    raise BusinessToolRequestError(f"字段「{f_spec.label or f_spec.name}」不能大于 {f_spec.maximum}。")
            if isinstance(api_val, str) and f_spec.min_length is not None and len(api_val) < f_spec.min_length:
                raise BusinessToolRequestError(f"字段「{f_spec.label or f_spec.name}」长度不能少于 {f_spec.min_length}。")
            if isinstance(api_val, str) and f_spec.max_length is not None and len(api_val) > f_spec.max_length:
                raise BusinessToolRequestError(f"字段「{f_spec.label or f_spec.name}」长度超过上限 {f_spec.max_length}。")
            existing_val = None if cur_val is _MISSING else cur_val
            same_now = (
                match_fn(spec, f_spec, existing_val, api_val)
                if callable(match_fn)
                else _values_equal(existing_val, api_val, kind=f_spec.kind)
            )
            if same_now:
                continue
            encoded_updates[f_spec.name] = api_val
            input_projection[f_spec.name] = item.get("value")
            if binding is not None:
                bindings.append(binding)
            shown_label = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
            details.append(change_detail(shown_label, str(old_disp), str(new_disp)))
            provenance_items.append({
                "field": f_spec.name,
                "label": f_spec.label or f_spec.name,
                "kind": f_spec.kind,
                "unit_scale": f_spec.unit_scale,
                "display_unit": f_spec.display_unit,
                "raw_input": item.get("value"),
                "api_value": api_val,
                "new_display": new_disp,
                "bound_choice": binding is not None,
            })

        if not encoded_updates:
            raise BusinessMutationRefusedError(
                "目标记录相关字段已是请求的目标值，无需重复修改。",
                reason="already_in_target_state",
            )

        # Construct complete flat 1:1 payload preserving every unmentioned writable field
        payload: dict[str, object] = {spec.id_field: record_id}
        encode_preserved_fn = getattr(self.port, "encode_preserved_field", None)
        for f_spec in spec.fields:
            if f_spec.name in encoded_updates:
                payload[f_spec.name] = encoded_updates[f_spec.name]
            else:
                rec_val = extract_projected_field_value(self.port, spec, f_spec, record)
                if rec_val is not _MISSING:
                    if not f_spec.required and _violates_optional_bounds(f_spec, rec_val):
                        continue
                    payload[f_spec.name] = (
                        encode_preserved_fn(spec, f_spec, rec_val)
                        if callable(encode_preserved_fn)
                        else rec_val
                    )
                elif f_spec.required:
                    raise BusinessToolRequestError(
                        cap.missing_required_field_message(f_spec.name)
                    )

        rule_errors = evaluate_validation_rules(spec.validation_rules, payload, spec.resolve_field)
        if rule_errors:
            raise BusinessToolRequestError("；".join(rule_errors))

        snapshot = _non_volatile_snapshot(record, spec.volatile_fields)
        restricted_digests = _restricted_snapshot_digests(record, spec.volatile_fields)
        disp_name = _record_display_name(record, spec.name_fields, spec.id_field)
        target_label = f"{disp_name}（{spec.label} #{record_id}）" if disp_name != str(record_id) else f"{spec.label} #{record_id}"
        preserved_count = len([k for k in payload if k != spec.id_field and k not in encoded_updates])
        details.append(
            preserved_detail("其余字段保留", f"保持同账号读取的其余 {preserved_count} 个字段原值不变")
        )
        evaluated_companions = evaluate_update_companions(
            self.port,
            spec,
            record_id,
            payload,
            before_record=record,
            changed=encoded_updates,
        )
        details.extend(format_companion_details(spec.side_effects, evaluated=evaluated_companions))
        diff = ProposalDiff.from_details((target_label,), details)
        # Project ALL requested inputs from `args` so EffectProjection.require(expected) matches 1:1
        all_projected = {}
        for item in raw_changes:
            f_spec = spec.resolve_field(str(item["field"]).strip())
            all_projected[f_spec.name if f_spec else str(item["field"]).strip()] = effect_value(item["value"])
        before_projected = {}
        for k in all_projected:
            if _SECRET_FIELD.search(k):
                continue
            f_sp = spec.resolve_field(k)
            if f_sp is not None:
                v = extract_projected_field_value(self.port, spec, f_sp, record)
                if v is not _MISSING:
                    before_projected[k] = v
            elif k in record:
                before_projected[k] = record.get(k)
        change = RequestedEffect(entity, "update", entity, str(args["target"]).strip(), canonical(all_projected))
        projection = EffectProjection(
            change,
            canonical(bindings),
            canonical(snapshot),
            canonical({}),
            ("record", "unrelated_field_preservation"),
            preserves_unmentioned=True,
            before_json=canonical(before_projected),
        )
        private = {
            "actor": actor_id,
            "entity": entity,
            "record_id": record_id,
            "payload": payload,
            "changed": encoded_updates,
            "snapshot": snapshot,
            "restricted_field_digests": restricted_digests,
            "bindings": bindings,
            "provenance": provenance_items,
            "side_effects": serialize_companion_specs(spec.side_effects, evaluated=evaluated_companions),
        }
        key = hashlib.sha256(canonical([entity, record_id, encoded_updates]).encode()).hexdigest()
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
        """Verify every changed value has an honest user/candidate/unit source before showing a card."""
        if proposal.effect_projection is None:
            return
        before_map = json.loads(proposal.effect_projection.before_json)
        spec = self.port.update_specs().get(str(inputs.get("entity") or "").strip().strip("/"))
        if spec is None:
            return
        prompt = texts[-1] if texts else ""
        joined_words = "\n".join(texts)
        ledger, receipt_vals = collect_provenance_context(existing_values)

        for item in inputs.get("changes") or ():
            f_spec = spec.resolve_field(str(item.get("field") or "").strip())
            if f_spec is None:
                continue
            raw_val = item.get("value")
            old_val = before_map.get(f_spec.name)
            validate_single_field_provenance(
                f_spec,
                raw_val,
                old_val=old_val,
                texts=texts,
                prompt=prompt,
                joined_words=joined_words,
                ledger=ledger,
                receipt_vals=receipt_vals,
                existing_values=existing_values,
                allow_append=True,
                allow_existing_candidates=True,
                enum_tokens_fn=getattr(self.port, "enum_provenance_tokens", None),
                reference_variants_fn=(
                    getattr(self.port, "reference_provenance_variants", None)
                    if "reference_variants" in write_capabilities_of(self.port)
                    else None
                ),
            )

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        entity = args["entity"]
        record_id = args["record_id"]

        def verify_actor():
            actor_id = self.port.verify_actor(context)
            if actor_id != args.get("actor"):
                raise BusinessToolIdentityError("prepared convention update belongs to a different actor")
            spec = self.port.update_specs().get(entity)
            if spec is None:
                raise BusinessToolRequestError("convention update spec is no longer available")
            return actor_id, spec

        def pre_dispatch_reread(actor_state):
            actor_id, spec = actor_state
            self.port.recheck_bindings(args.get("bindings") or [], actor_id=actor_id)
            current = self.port.read_record(entity, record_id)
            if not isinstance(current, dict):
                raise BusinessMutationRefusedError(
                    "target record is no longer readable before dispatch",
                    reason="precondition_changed",
                )
            current_snap = _non_volatile_snapshot(current, spec.volatile_fields)
            if verify_preservation_snapshot(
                args["snapshot"],
                current_snap,
                ignore=spec.volatile_fields,
                restricted_before=args.get("restricted_field_digests", {}),
                restricted_after=_restricted_snapshot_digests(current, spec.volatile_fields),
                values_equal=_values_equal,
            ):
                raise BusinessMutationRefusedError(
                    "current record changed after approval; prepare again",
                    reason="precondition_changed",
                )
            destructive = [
                se
                for se in (args.get("side_effects") or ())
                if isinstance(se, dict) and se.get("destructive_delete") and se.get("triggered", True)
            ]
            if destructive:
                if "companion_readback" not in write_capabilities_of(self.port):
                    raise BusinessMutationRefusedError(
                        "companion row count capability unavailable before destructive companion write",
                        reason="precondition_changed",
                    )
                count_fn = getattr(self.port, "count_companion_rows", None)
                if not callable(count_fn):
                    raise BusinessMutationRefusedError(
                        "companion row count reader unavailable before destructive companion write",
                        reason="precondition_changed",
                    )
                for se in destructive:
                    now_count = count_fn(
                        spec,
                        record_id,
                        args["payload"],
                        before_record=current,
                        side_effect=se,
                        changed=args.get("changed"),
                    )
                    if (
                        now_count is None
                        or isinstance(now_count, bool)
                        or not isinstance(now_count, int)
                        or now_count != se.get("expected_delete_count")
                    ):
                        raise BusinessMutationRefusedError(
                            "待删除重建的子表记录数在确认后发生变化，请重新查询并确认。",
                            reason="precondition_changed",
                        )
            return current_snap, current

        def dispatch_once(actor_state, _reread_state):
            _actor_id, spec = actor_state
            self.port.dispatch_update(spec, record_id, args["payload"])

        def verify_readback(actor_state, reread_state, _dispatch_result, _uncertain):
            _actor_id, spec = actor_state
            current_snap, current_record = reread_state
            after = self.port.read_record(entity, record_id)
            if not isinstance(after, dict) or str(after.get(spec.id_field)) != str(record_id):
                raise BusinessMutationIndeterminate(
                    "convention update readback missed target record",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id},
                )
            changed: dict = args["changed"]
            field_kinds = {f.name: f.kind for f in spec.fields}
            spec_fields = {f.name: f for f in spec.fields}
            match_fn = getattr(self.port, "matches_readback_value", None)
            projected_cols_ignored: set[str] = set()
            for key, expected_val in changed.items():
                f_sp = spec_fields.get(key)
                actual_val = extract_projected_field_value(self.port, spec, f_sp, after) if f_sp is not None else after.get(key)
                if actual_val is _MISSING:
                    actual_val = None
                if f_sp is not None and f_sp.readback_projection:
                    projected_cols_ignored.add(f_sp.readback_projection[0])
                if f_sp is not None and f_sp.derived_display_fields:
                    projected_cols_ignored.update(f_sp.derived_display_fields)
                matched = (
                    match_fn(spec, spec_fields[key], actual_val, expected_val)
                    if callable(match_fn) and key in spec_fields
                    else _values_equal(actual_val, expected_val, kind=field_kinds.get(key, ""))
                )
                if not matched:
                    raise BusinessMutationIndeterminate(
                        f"convention update readback did not confirm field {key}",
                        phase="postcondition",
                        reconciliation={"entity": entity, "id": record_id, "field": key},
                    )
            ignore = frozenset(spec.volatile_fields) | frozenset(changed.keys()) | frozenset(projected_cols_ignored)
            drifted = verify_preservation_snapshot(
                current_snap,
                _non_volatile_snapshot(after, spec.volatile_fields),
                ignore=ignore,
                restricted_before=args.get("restricted_field_digests", {}),
                restricted_after=_restricted_snapshot_digests(after, spec.volatile_fields),
                values_equal=_values_equal,
            )
            if drifted:
                raise BusinessMutationIndeterminate(
                    "convention update altered unmentioned fields: " + ", ".join(drifted[:6]),
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id, "unverified_effect": "unrelated_field_preservation"},
                )
            disp_name = _record_display_name(after, spec.name_fields, spec.id_field)
            reference = f"{disp_name}（ID={record_id}）" if disp_name and disp_name != str(record_id) else f"ID={record_id}"
            effects = [
                {"effect": "record", "verified": True, "id": record_id},
                {"effect": "unrelated_field_preservation", "verified": True, "id": record_id},
            ]
            effects.extend(
                run_companion_readback(
                    self.port,
                    spec,
                    record_id,
                    args["payload"],
                    before_record=current_record,
                    after_record=after,
                    side_effects=args.get("side_effects") or (),
                )
            )
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
            readback_failure_message="convention update acknowledged; same-actor readback failed",
            readback_reconciliation=lambda _rs, _dr: {"entity": entity, "id": record_id},
        )


def _verify_scaled_number_provenance(
    f_spec: ConventionFieldSpec,
    raw_val,
    texts: tuple[str, ...],
    *,
    receipts: tuple[str, ...] = (),
) -> None:
    joined = "\n".join(texts)
    label = f_spec.label or f_spec.name
    s_raw = str(raw_val).strip()
    receipt_set = {str(r).strip() for r in receipts if isinstance(r, str) and r.strip()}
    if f_spec.unit_scale == 100:
        if s_raw in {"0", "0.0", "0.00", "0元", "0块", "0分"} and re.search(r"无门槛|不限|免费|零|(?<![\d.])0(?![\d.])", joined):
            return
        m_unit = re.fullmatch(r"(-?\d+(?:\.\d{1,2})?)\s*(元|块|分)", s_raw)
        if m_unit:
            num_str, unit = m_unit.group(1), m_unit.group(2)
            dec = Decimal(num_str)
            if dec == 0 and re.search(r"无门槛|不限|免费|零|(?<![\d.])0(?![\d.])", joined):
                return
            if unit in ("元", "块"):
                fen_str = scalar_text(int((dec * Decimal(100)).to_integral_value()))
                yuan_clean = scalar_text(dec)
                if yuan_clean in receipt_set or num_str in receipt_set or fen_str in receipt_set:
                    return
                if (re.search(r"(?<![\d.])" + re.escape(fen_str) + r"\s*分", joined)
                        or re.search(r"(?<![\d.])" + re.escape(num_str) + r"\s*(?:元|块)", joined)
                        or re.search(r"(?<![\d.])" + re.escape(yuan_clean) + r"\s*(?:元|块)", joined)):
                    return
                if (check_entries(SourceLedger(entries=[Entry(label, yuan_clean, kind="text")]), texts)[0].ok
                        and not re.search(r"(?<![\d.])" + re.escape(yuan_clean) + r"\s*分", joined)):
                    return
                raise BusinessToolRequestError(
                    f"字段「{label}」的金额「{s_raw}」未在用户原话或本轮 SQL 计算回执中找到同单位依据"
                    "（若由原值按增减额算得，请先调 sql 执行算式留存计算回执后再提交）。"
                )
            # unit == "分"
            fen_clean = scalar_text(int(dec))
            yuan_clean = scalar_text(dec / Decimal(100))
            if fen_clean in receipt_set or yuan_clean in receipt_set:
                return
            if (re.search(r"(?<![\d.])" + re.escape(fen_clean) + r"\s*分", joined)
                    or re.search(r"(?<![\d.])" + re.escape(yuan_clean) + r"\s*(?:元|块)", joined)):
                return
            raise BusinessToolRequestError(
                f"字段「{label}」的金额「{s_raw}」未在用户原话或本轮 SQL 计算回执中找到依据"
                "（若由原值按增减额算得，请先调 sql 执行算式留存计算回执后再提交）。"
            )
        raise BusinessToolRequestError(
            f"字段「{label}」在接口中按分存储，请传带明确单位的字符串（如 '25元' 或 '2500分'），并确保数值来自用户原话。"
        )
    if f_spec.unit_scale == 10:
        m_zhe = re.fullmatch(r"(\d+(?:\.\d)?)\s*折", s_raw)
        m_pct = re.fullmatch(r"(\d+)\s*%", s_raw)
        if m_zhe:
            num = Decimal(m_zhe.group(1))
            zhe_clean = scalar_text(num)
            pct_clean = scalar_text(num * Decimal(10))
            if (re.search(r"(?<![\d.])" + re.escape(zhe_clean) + r"\s*折", joined)
                    or re.search(r"(?<![\d.])" + re.escape(pct_clean) + r"\s*%", joined)):
                return
            if (check_entries(SourceLedger(entries=[Entry(label, zhe_clean, kind="text")]), texts)[0].ok
                    and not re.search(r"(?<![\d.])" + re.escape(zhe_clean) + r"\s*%", joined)):
                return
            raise BusinessToolRequestError(f"字段「{label}」的折扣值「{s_raw}」未在用户原话中找到依据。")
        if m_pct:
            num = Decimal(m_pct.group(1))
            pct_clean = scalar_text(num)
            zhe_clean = scalar_text(num / Decimal(10))
            if (re.search(r"(?<![\d.])" + re.escape(pct_clean) + r"\s*%", joined)
                    or re.search(r"(?<![\d.])" + re.escape(zhe_clean) + r"\s*折", joined)):
                return
            raise BusinessToolRequestError(f"字段「{label}」的折扣百分比「{s_raw}」未在用户原话中找到依据。")
        raise BusinessToolRequestError(
            f"字段「{label}」请传带明确单位的折扣字符串（如 '8.5折' 或 '85%'），并确保数值来自用户原话。"
        )


class ConventionStatusHandler:
    """Shared handler for reversible two-state status toggles."""

    def __init__(self, port: ConventionWritePort):
        self.port = port

    def execute(self, args: dict, *, context=None) -> BusinessToolResult:
        raise BusinessToolRequestError("toggle_record_status is a write proposal tool")

    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec:
        actor_id = self.port.verify_actor(context)
        entity = str(args.get("entity") or "").strip().strip("/")
        spec = self.port.status_specs().get(entity)
        cap = resolve_write_route_capability(self.port)
        if spec is None:
            dedicated_fn = getattr(self.port, "dedicated_tool_for", None)
            dedicated = dedicated_fn(entity, "status") if callable(dedicated_fn) else None
            if not dedicated:
                update_spec = self.port.update_specs().get(entity)
                if update_spec is not None and update_spec.resolve_field("status") is not None:
                    raise BusinessToolRequestError(
                        f"实体「{entity}」无独立两态状态接口，但其 status 字段可通过 propose_operation(\"update_record\", ...) 直接修改。"
                    )
            raise BusinessToolRequestError(cap.unsupported_status_message(entity, dedicated=dedicated))
        resolved = spec.resolve_state(str(args.get("status") or ""))
        if resolved is None:
            allowed = " / ".join(label for label, _ in spec.states)
            raise BusinessToolRequestError(
                f"实体「{entity}」仅支持两态切换：{allowed}（收到：{args.get('status')!r}）。"
            )
        after_label, target_code = resolved
        record = self.port.resolve_target(entity, str(args["target"]).strip(), spec=spec)
        record_id = record.get(spec.id_field)
        if record_id is None or record_id == "":
            raise BusinessToolRequestError("目标记录缺少唯一主键。")
        current_code = record.get(spec.status_field)
        before_label = spec.label_for_code(current_code)
        if before_label is None:
            raise BusinessToolRequestError(
                f"目标记录当前状态值（{current_code!r}）不在两态可逆范围内，拒绝通过两态开关修改。"
            )
        if before_label == after_label:
            raise BusinessMutationRefusedError(
                f"{spec.label}当前已是「{after_label}」状态，无需重复操作。",
                reason="already_in_target_state",
            )
        snapshot = _non_volatile_snapshot(record, spec.volatile_fields)
        restricted_digests = _restricted_snapshot_digests(record, spec.volatile_fields)
        disp_name = _record_display_name(record, spec.name_fields, spec.id_field)
        target_label = f"{disp_name}（{spec.label} #{record_id}）" if disp_name != str(record_id) else f"{spec.label} #{record_id}"
        diff = ProposalDiff.from_details(
            (target_label,),
            (
                preserved_detail(spec.label, disp_name),
                change_detail("状态", before_label, after_label),
                *(side_effect_detail("连带影响", note) for note in spec.side_effects if note),
            ),
        )
        change = RequestedEffect(
            entity, "state_change", entity, str(args["target"]).strip(), canonical({spec.status_field: after_label})
        )
        projection = EffectProjection(
            change,
            canonical([]),
            canonical(snapshot),
            canonical({}),
            ("record", "unrelated_field_preservation"),
            preserves_unmentioned=True,
        )
        private = {
            "actor": actor_id,
            "entity": entity,
            "record_id": record_id,
            "target_code": target_code,
            "after_label": after_label,
            "snapshot": snapshot,
            "restricted_field_digests": restricted_digests,
        }
        key = hashlib.sha256(canonical([entity, record_id, spec.status_field, target_code]).encode()).hexdigest()
        return BusinessMutationSpec(
            private,
            f"将{spec.label}「{disp_name}」状态设为{after_label}",
            diff.targets,
            details=diff.details(),
            reconcile_key=key,
            reconciliation={"entity": entity, "id": record_id, "operation": "toggle_record_status", "body_digest": key},
            effect_projection=projection,
        )

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        entity = args["entity"]
        record_id = args["record_id"]

        def verify_actor():
            actor_id = self.port.verify_actor(context)
            if actor_id != args.get("actor"):
                raise BusinessToolIdentityError("prepared status toggle belongs to a different actor")
            spec = self.port.status_specs().get(entity)
            if spec is None:
                raise BusinessToolRequestError("two-state status spec is no longer available")
            return spec

        def pre_dispatch_reread(spec):
            current = self.port.read_record(entity, record_id)
            if not isinstance(current, dict):
                raise BusinessMutationRefusedError(
                    "target record is no longer readable before dispatch",
                    reason="precondition_changed",
                )
            current_snap = _non_volatile_snapshot(current, spec.volatile_fields)
            if verify_preservation_snapshot(
                args["snapshot"],
                current_snap,
                ignore=spec.volatile_fields,
                restricted_before=args.get("restricted_field_digests", {}),
                restricted_after=_restricted_snapshot_digests(current, spec.volatile_fields),
                values_equal=_values_equal,
            ):
                raise BusinessMutationRefusedError(
                    "current record changed after approval; prepare again",
                    reason="precondition_changed",
                )
            return current_snap

        def dispatch_once(spec, _current_snap):
            self.port.dispatch_status(spec, record_id, args["target_code"])

        def verify_readback(spec, current_snap, _dispatch_result, _uncertain):
            after = self.port.read_record(entity, record_id)
            if not isinstance(after, dict) or str(after.get(spec.id_field)) != str(record_id):
                raise BusinessMutationIndeterminate(
                    "status toggle readback missed target record",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id},
                )
            if spec.label_for_code(after.get(spec.status_field)) != args["after_label"]:
                raise BusinessMutationIndeterminate(
                    "status toggle readback did not confirm target status",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id},
                )
            ignore = frozenset(spec.volatile_fields) | {spec.status_field}
            drifted = verify_preservation_snapshot(
                current_snap,
                _non_volatile_snapshot(after, spec.volatile_fields),
                ignore=ignore,
                restricted_before=args.get("restricted_field_digests", {}),
                restricted_after=_restricted_snapshot_digests(after, spec.volatile_fields),
                values_equal=_values_equal,
            )
            if drifted:
                raise BusinessMutationIndeterminate(
                    "status toggle altered unmentioned fields: " + ", ".join(drifted[:6]),
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
                {"verified": True, "operation": "toggle_record_status", "effects": effects},
            )

        return execute_verified_handler_write(
            verify_actor=verify_actor,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            invalidate_reads=lambda: self.port.invalidate_reads(frozenset({entity})),
            allow_readback_recovery=True,
            use_verification_interval=False,
            readback_failure_message="status toggle acknowledged; same-actor readback failed",
            readback_reconciliation=lambda _rs, _dr: {"entity": entity, "id": record_id},
        )


def convention_tool_definitions(
    port: ConventionWritePort,
    *,
    scope: str,
    entity_example: str = "records",
) -> tuple[ToolDefinition, ...]:
    """Build bounded typed ``ToolDefinition`` entries for ``update_record`` and ``toggle_record_status``."""
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
                "description": "仅列出用户明确要求修改的字段及新值；未列出的字段程序自动按同账号现值原样保留。按分存储的金额字段请传带单位字符串（如 '25元' 或 '2500分'），按十分比存储的折扣字段传如 '8.5折'。",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 80,
                            "description": "接口字段名或表单中文标签。",
                        },
                        "value": {
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
                        },
                    },
                    "required": ["field", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["entity", "target", "changes"],
        "additionalProperties": False,
    }
    status_schema = {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": f"支持两态互逆状态切换的实体路径键（如 {entity_example}）。",
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
    update_handler = ConventionUpdateHandler(port)
    status_handler = ConventionStatusHandler(port)
    return (
        ToolDefinition(
            "update_record",
            "修改满足扁平对称约定的单条现有记录字段（同账号读取全记录→仅替换指定字段→批准后单次提交并全字段回读核验；不含明细行实体与密码字段）。",
            json.dumps(update_schema, ensure_ascii=False),
            "write",
            scope,
            ToolOperation("convention_record", "update"),
            update_handler,
            reference_inputs=("target",),
            effect_contract=ConventionWriteEffects("update_record", port),
            preserves_unmentioned=True,
        ),
        ToolDefinition(
            "toggle_record_status",
            "切换两态互逆状态开关（开启/关闭、启用/停用、上架/下架；批准后单次提交并全字段回读核验，不含多态审批流转）。",
            json.dumps(status_schema, ensure_ascii=False),
            "write",
            scope,
            ToolOperation("convention_record", "state_change"),
            status_handler,
            reference_inputs=("target",),
            effect_contract=ConventionWriteEffects("toggle_record_status", port),
            preserves_unmentioned=True,
        ),
    )


__all__ = [
    "CompanionWriteSpec",
    "ConventionFieldSpec",
    "ConventionStatusHandler",
    "ConventionUpdateHandler",
    "ConventionUpdateSpec",
    "ConventionWriteEffects",
    "ConventionWritePort",
    "TwoStateStatusSpec",
    "ValidationRuleSpec",
    "WRITE_PORT_OPTIONAL",
    "WriteRouteCapability",
    "collect_provenance_context",
    "convention_tool_definitions",
    "effect_value",
    "evaluate_update_companions",
    "evaluate_validation_rules",
    "extract_projected_field_value",
    "format_companion_details",
    "normalize_changes_argument",
    "normalize_create_arguments",
    "normalize_field_items",
    "normalize_update_arguments",
    "resolve_compiled_write_fallback",
    "resolve_spec_field",
    "resolve_write_route_capability",
    "run_companion_readback",
    "serialize_companion_specs",
    "validate_single_field_provenance",
    "write_capabilities_of",
]
