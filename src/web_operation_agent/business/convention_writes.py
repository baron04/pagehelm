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
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Protocol

from ..execution.submit_contract import same_value, scalar_text
from ..execution.value_source import (
    Entry,
    SourceLedger,
    append_overwrites_existing,
    check_entries,
)
from ..goal.skill_literals import _existing_append_supplied, literal_supplied
from ..schemas.approval_details import ApprovalDetail
from .dispatch_evidence import verified_after_readback
from .effects import EffectProjection, RequestedEffect, canonical
from .errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolIdentityError,
    BusinessToolRequestError,
)
from .tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from .tools.registry import ToolDefinition

_SECRET_FIELD = re.compile(
    r"pass(?:word)?|pwd|secret|private_?key|api[_-]?key|aes_?key|sign_?key|mch_?key|"
    r"token|authorization|credential|^config$|config$|密码|密钥|令牌",
    re.I,
)
_FIELD_KINDS = frozenset({"string", "integer", "number", "boolean", "id_array"})


def normalize_changes_argument(args: dict) -> dict:
    """Allow models to pass ``changes`` either as ``[{field, value}]`` or ``{field: value}``."""
    if not isinstance(args, dict) or not isinstance(args.get("changes"), dict):
        return args
    raw = args["changes"]
    if not raw:
        return {**args, "changes": []}
    converted = []
    for k, v in raw.items():
        if isinstance(v, list):
            v = [str(item) for item in v]
        converted.append({"field": str(k), "value": v})
    return {**args, "changes": converted}


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

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", self.name):
            raise ValueError(f"invalid convention field name: {self.name!r}")
        if _SECRET_FIELD.search(self.name) or (self.label and _SECRET_FIELD.search(self.label)):
            raise ValueError(f"credential/secret field forbidden in convention updates: {self.name}")
        if self.kind not in _FIELD_KINDS:
            raise ValueError(f"unsupported convention field kind: {self.kind!r}")
        if type(self.unit_scale) is not int or self.unit_scale < 1:
            raise ValueError("unit_scale must be a positive integer")


@dataclass(frozen=True, slots=True)
class ConventionUpdateSpec:
    """Flat symmetric update contract for one entity."""

    entity: str
    label: str
    fields: tuple[ConventionFieldSpec, ...]
    id_field: str = "id"
    name_fields: tuple[str, ...] = ("name",)
    volatile_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.entity or not self.fields:
            raise ValueError("convention update spec requires entity and fields")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names) or self.id_field in names:
            raise ValueError(f"duplicate or id field in convention update spec: {self.entity}")

    def resolve_field(self, token: str) -> ConventionFieldSpec | None:
        cleaned = token.strip().rstrip(":：").strip()
        for f in self.fields:
            if f.name == cleaned:
                return f
        folded = cleaned.casefold()
        by_case = [f for f in self.fields if f.name.casefold() == folded]
        if len(by_case) == 1:
            return by_case[0]
        by_label = [f for f in self.fields if f.label and f.label.strip().rstrip(":：").strip() == cleaned]
        if len(by_label) == 1:
            return by_label[0]
        return None


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

    def __post_init__(self) -> None:
        if not self.entity or len(self.states) != 2:
            raise ValueError("two-state status spec requires entity and exactly 2 states")
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


class ConventionWritePort(Protocol):
    """Adapter-supplied runtime behavior for flat convention updates and two-state toggles."""

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
    return {
        k: v for k, v in record.items()
        if k not in volatile and not _SECRET_FIELD.search(k)
    }


def _restricted_snapshot_digests(record: dict, volatile: frozenset[str]) -> dict[str, str]:
    """Digest excluded sensitive/config fields by hashed field name without exposing keys or values."""
    return {
        hashlib.sha256(k.encode()).hexdigest()[:16]: hashlib.sha256(canonical(record[k]).encode()).hexdigest()
        for k in sorted(record)
        if k not in volatile and _SECRET_FIELD.search(k)
    }


def _values_equal(left, right, *, kind: str = "") -> bool:
    if kind == "id_array":
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and [str(x) for x in left] == [str(x) for x in right]
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return same_value(left, right)


def _snapshot_preserved(before: dict, after: dict, *, ignore: frozenset[str]) -> list[str]:
    drifted: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key in ignore:
            continue
        b_val, a_val = before.get(key), after.get(key)
        if isinstance(b_val, list) and isinstance(a_val, list):
            if len(b_val) != len(a_val) or any(not same_value(x, y) for x, y in zip(b_val, a_val)):
                drifted.append(key)
        elif isinstance(b_val, dict) or isinstance(a_val, dict):
            if canonical(b_val) != canonical(a_val):
                drifted.append(key)
        elif not _values_equal(b_val, a_val):
            drifted.append(key)
    return drifted


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
            mapped[key] = item["value"]
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
        if spec is None:
            dedicated_fn = getattr(self.port, "dedicated_tool_for", None)
            dedicated = dedicated_fn(entity, "update") if callable(dedicated_fn) else None
            if dedicated:
                raise BusinessToolRequestError(
                    f"实体「{entity}」已有已安装的专用修改操作「{dedicated}」，请直接用 propose_operation(\"{dedicated}\", ...) 办理。"
                )
            raise BusinessToolRequestError(
                f"实体「{entity}」不满足扁平对称约定更新条件（可能含明细行、非对称字段或无配对详情接口）；"
                "请改用已安装的专用操作，或打开对应页面编辑表单配合 request_submit 办理。"
            )
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
                    f"{f.name}({f.label})" if f.label else f.name for f in spec.fields[:25]
                )
                raise BusinessToolRequestError(
                    f"实体「{entity}」不支持通过约定更新修改字段「{raw_field}」"
                    f"（支持字段：{supported}）。若是状态字段请改用 toggle_record_status 或专用状态操作；"
                    "若是其它表单专属字段请改走页面表单 request_submit。"
                )
            if f_spec.name in seen_fields:
                raise BusinessToolRequestError(f"字段「{f_spec.name}」在同一次修改中重复出现。")
            seen_fields.add(f_spec.name)
            api_val, old_disp, new_disp, binding = self.port.encode_field(
                spec, f_spec, item.get("value"), record
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
            same_now = (
                match_fn(spec, f_spec, record.get(f_spec.name), api_val)
                if callable(match_fn)
                else _values_equal(record.get(f_spec.name), api_val, kind=f_spec.kind)
            )
            if same_now:
                continue
            encoded_updates[f_spec.name] = api_val
            input_projection[f_spec.name] = item.get("value")
            if binding is not None:
                bindings.append(binding)
            shown_label = f"{f_spec.label}（{f_spec.name}）" if f_spec.label else f_spec.name
            details.append(ApprovalDetail("change", shown_label, f"{old_disp} → {new_disp}"))
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
                raise BusinessToolRequestError(
                    f"目标记录当前缺少必填原字段「{f_spec.name}」，无法安全构造整条更新请求；请改走页面表单。"
                )

        snapshot = _non_volatile_snapshot(record, spec.volatile_fields)
        restricted_digests = _restricted_snapshot_digests(record, spec.volatile_fields)
        disp_name = _record_display_name(record, spec.name_fields, spec.id_field)
        target_label = f"{disp_name}（{spec.label} #{record_id}）" if disp_name != str(record_id) else f"{spec.label} #{record_id}"
        preserved_count = len([k for k in payload if k != spec.id_field and k not in encoded_updates])
        details.append(
            ApprovalDetail("preserved", "其余字段保留", f"保持同账号读取的其余 {preserved_count} 个字段原值不变")
        )
        # Project ALL requested inputs from `args` so EffectProjection.require(expected) matches 1:1
        all_projected = {}
        for item in raw_changes:
            f_spec = spec.resolve_field(str(item["field"]).strip())
            all_projected[f_spec.name if f_spec else str(item["field"]).strip()] = item["value"]
        before_projected = {
            k: record.get(k)
            for k in all_projected
            if k in record and not _SECRET_FIELD.search(k)
        }
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
        }
        key = hashlib.sha256(canonical([entity, record_id, encoded_updates]).encode()).hexdigest()
        return BusinessMutationSpec(
            private,
            f"修改{spec.label}「{disp_name}」",
            (target_label,),
            details=tuple(details),
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

        ledger = SourceLedger()
        if existing_values:
            for binding, vals in existing_values.items():
                for v in vals:
                    ledger.add_candidates([(binding, v)])

        for item in inputs.get("changes") or ():
            f_spec = spec.resolve_field(str(item.get("field") or "").strip())
            if f_spec is None:
                continue
            raw_val = item.get("value")
            old_val = before_map.get(f_spec.name)
            label = f_spec.label or f_spec.name

            # 1. Check append overwrite protection on strings (including clearing to None)
            if isinstance(raw_val, str) or raw_val is None:
                val_for_append = raw_val if isinstance(raw_val, str) else ""
                if (append_overwrites_existing(prompt, label, old_val, val_for_append)
                        or append_overwrites_existing(prompt, f_spec.name, old_val, val_for_append)):
                    raise BusinessToolRequestError(
                        f"用户要求在「{label}」中追加或补充内容，当前修改覆盖或清空了原有内容；"
                        "请先读取并保留目标记录该字段原值，再在其后追加本次内容。"
                    )

            field_cands = (
                {
                    *existing_values.get(label, ()),
                    *existing_values.get(f_spec.name, ()),
                }
                if existing_values else set()
            )
            scoped_cands = [
                (b, v) for b, v in ledger.candidates
                if b in {label, f_spec.name, f_spec.reference_entity} or b.endswith("." + f_spec.name)
            ]

            # 2. Boolean fields
            if f_spec.kind == "boolean":
                if raw_val is not None:
                    if str(raw_val) in field_cands:
                        continue
                    bool_intent = (
                        raw_val if isinstance(raw_val, bool)
                        else (
                            True if str(raw_val).strip().lower() in {"true", "是", "开启", "启用", "打开", "允许", "设为默认"}
                            else (
                                False if str(raw_val).strip().lower() in {"false", "否", "关闭", "停用", "禁用", "取消", "不启用", "不允许"}
                                else None
                            )
                        )
                    )
                    if bool_intent is not None:
                        neg_prefix = re.compile(r"(?:不允许|不可|不能|不要|不得|不用|不准|不予|禁止|严禁|拒绝|取消|别|勿|非|不$)")
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
                        if (bool_intent is True and positive_bool) or (bool_intent is False and negative_bool):
                            continue
                    raise BusinessToolRequestError(
                        f"布尔字段「{label}」的值「{raw_val}」未在用户原话中找到依据。"
                    )
                continue

            # 3. Reference fields (single FK or ID array)
            if f_spec.reference_entity and raw_val is not None:
                tokens = raw_val if isinstance(raw_val, list) else [raw_val]
                for tok in tokens:
                    s_tok = str(tok).strip()
                    leaf = s_tok.rsplit("/", 1)[-1].strip()
                    if s_tok == "根节点" and re.search(r"根|顶层|顶级|一级", joined_words):
                        continue
                    if (literal_supplied(s_tok, texts)
                            or (leaf and literal_supplied(leaf, texts))
                            or any(c_val in {s_tok, leaf} for _, c_val in scoped_cands)):
                        continue
                    raise BusinessToolRequestError(
                        f"关联字段「{label}」的目标「{s_tok[:60]}」未在用户原话或已读候选中找到依据。"
                    )
                continue

            # 4. Non-reference ID arrays
            if f_spec.kind == "id_array" and raw_val is not None:
                if not isinstance(raw_value := raw_val, list):
                    raise BusinessToolRequestError(f"数组字段「{label}」须传数组。")
                for elem in raw_value:
                    s_elem = scalar_text(elem).strip()
                    if not s_elem:
                        continue
                    if literal_supplied(s_elem, texts):
                        continue
                    verdict = check_entries(
                        SourceLedger(entries=[Entry(label, s_elem, kind="text")], candidates=scoped_cands),
                        texts,
                    )[0]
                    if not verdict.ok:
                        raise BusinessToolRequestError(
                            f"字段「{label}」的元素「{s_elem[:60]}」未在用户原话或已读候选中找到依据。"
                        )
                continue

            # 5. Dictionary enum fields
            if f_spec.enum_type and raw_val is not None:
                s_tok = str(raw_val).strip()
                synonyms = {
                    "开启": ("开启", "启用", "正常", "有效"),
                    "关闭": ("关闭", "停用", "禁用"),
                }
                accepted = synonyms.get(s_tok, (s_tok,))
                if any(literal_supplied(w, texts) for w in accepted) or s_tok in field_cands:
                    continue
                raise BusinessToolRequestError(
                    f"枚举字段「{label}」的选项「{s_tok[:60]}」未在用户原话中找到依据。"
                )

            # 6. Scaled integer fields (e.g. unit_scale=100 fen, unit_scale=10 zhe)
            if f_spec.unit_scale > 1 and raw_val is not None:
                _verify_scaled_number_provenance(f_spec, raw_val, texts)
                continue

            # 7. Date / datetime string inputs (for both string dates and epoch-ms timestamps)
            if f_spec.date_kind and isinstance(raw_val, str) and raw_val:
                entry = Entry(label, raw_val, kind="text")
                verdict = check_entries(
                    SourceLedger(entries=[entry], candidates=scoped_cands),
                    texts,
                )[0]
                if not verdict.ok:
                    raise BusinessToolRequestError(
                        f"字段「{label}」的日期时间值「{raw_val}」未在用户原话或本轮时间换算中找到依据，请向用户确认。"
                    )
                continue

            # 8. Plain strings: literal in user text, safe append over old_val, or scoped candidate
            if f_spec.kind == "string" and isinstance(raw_val, str) and raw_val:
                if literal_supplied(raw_val, texts):
                    continue
                allowed_old = (old_val,) if isinstance(old_val, str) and old_val else ()
                if _existing_append_supplied(raw_val, texts, allowed_old):
                    continue
                if raw_val in field_cands and re.search(r"同上|一样|相同|沿用|复制|照旧", joined_words):
                    continue
                raise BusinessToolRequestError(
                    f"字段「{label}」的值「{raw_val[:60]}」不是用户原话提供的内容（若为追加请保留原值前缀），不能自行编造或改写。"
                )

            # 9. Numbers / integers (unit_scale == 1)
            if f_spec.kind in {"integer", "number"} and raw_val is not None and not isinstance(raw_val, bool):
                entry = Entry(label, scalar_text(raw_val), kind="text")
                verdict = check_entries(
                    SourceLedger(entries=[entry], candidates=scoped_cands),
                    texts,
                )[0]
                if not verdict.ok:
                    raise BusinessToolRequestError(
                        f"字段「{label}」的数值「{raw_val}」未在用户原话或本轮计算中找到依据，请向用户确认。"
                    )

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        actor_id = self.port.verify_actor(context)
        if actor_id != args.get("actor"):
            raise BusinessToolIdentityError("prepared convention update belongs to a different actor")
        entity = args["entity"]
        spec = self.port.update_specs().get(entity)
        if spec is None:
            raise BusinessToolRequestError("convention update spec is no longer available")
        record_id = args["record_id"]
        self.port.recheck_bindings(args.get("bindings") or [], actor_id=actor_id)

        # Pre-dispatch reread: refuse if any non-volatile field drifted since preparation
        current = self.port.read_record(entity, record_id)
        if not isinstance(current, dict):
            raise BusinessMutationRefusedError(
                "target record is no longer readable before dispatch",
                reason="precondition_changed",
            )
        current_snap = _non_volatile_snapshot(current, spec.volatile_fields)
        if (_snapshot_preserved(args["snapshot"], current_snap, ignore=spec.volatile_fields)
                or _restricted_snapshot_digests(current, spec.volatile_fields) != args.get("restricted_field_digests", {})):
            raise BusinessMutationRefusedError(
                "current record changed after approval; prepare again",
                reason="precondition_changed",
            )

        uncertain_dispatch = False
        try:
            self.port.dispatch_update(spec, record_id, args["payload"])
        except BusinessMutationIndeterminate:
            uncertain_dispatch = True

        # Post-dispatch same-actor full-field readback
        try:
            after = self.port.read_record(entity, record_id)
        except Exception as exc:
            raise BusinessMutationIndeterminate(
                "convention update acknowledged; same-actor readback failed",
                phase="postcondition",
                reconciliation={"entity": entity, "id": record_id},
            ) from exc
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
        for key, expected_val in changed.items():
            matched = (
                match_fn(spec, spec_fields[key], after.get(key), expected_val)
                if callable(match_fn) and key in spec_fields
                else _values_equal(after.get(key), expected_val, kind=field_kinds.get(key, ""))
            )
            if not matched:
                raise BusinessMutationIndeterminate(
                    f"convention update readback did not confirm field {key}",
                    phase="postcondition",
                    reconciliation={"entity": entity, "id": record_id, "field": key},
                )
        ignore = frozenset(spec.volatile_fields) | frozenset(changed.keys())
        drifted = _snapshot_preserved(current_snap, _non_volatile_snapshot(after, spec.volatile_fields), ignore=ignore)
        if not drifted and _restricted_snapshot_digests(after, spec.volatile_fields) != args.get("restricted_field_digests", {}):
            drifted = ["restricted_field"]
        if drifted:
            raise BusinessMutationIndeterminate(
                "convention update altered unmentioned fields: " + ", ".join(drifted[:6]),
                phase="postcondition",
                reconciliation={"entity": entity, "id": record_id, "unverified_effect": "unrelated_field_preservation"},
            )
        self.port.invalidate_reads(frozenset({entity}))
        if uncertain_dispatch:
            verified_after_readback()
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


def _verify_scaled_number_provenance(f_spec: ConventionFieldSpec, raw_val, texts: tuple[str, ...]) -> None:
    joined = "\n".join(texts)
    label = f_spec.label or f_spec.name
    s_raw = str(raw_val).strip()
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
                if (re.search(r"(?<![\d.])" + re.escape(fen_str) + r"\s*分", joined)
                        or re.search(r"(?<![\d.])" + re.escape(num_str) + r"\s*(?:元|块)", joined)
                        or re.search(r"(?<![\d.])" + re.escape(yuan_clean) + r"\s*(?:元|块)", joined)):
                    return
                if (check_entries(SourceLedger(entries=[Entry(label, yuan_clean, kind="text")]), texts)[0].ok
                        and not re.search(r"(?<![\d.])" + re.escape(yuan_clean) + r"\s*分", joined)):
                    return
                raise BusinessToolRequestError(f"字段「{label}」的金额「{s_raw}」未在用户原话中找到同单位依据。")
            # unit == "分"
            fen_clean = scalar_text(int(dec))
            yuan_clean = scalar_text(dec / Decimal(100))
            if (re.search(r"(?<![\d.])" + re.escape(fen_clean) + r"\s*分", joined)
                    or re.search(r"(?<![\d.])" + re.escape(yuan_clean) + r"\s*(?:元|块)", joined)):
                return
            raise BusinessToolRequestError(f"字段「{label}」的金额「{s_raw}」未在用户原话中找到依据。")
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
        if spec is None:
            dedicated_fn = getattr(self.port, "dedicated_tool_for", None)
            dedicated = dedicated_fn(entity, "status") if callable(dedicated_fn) else None
            if dedicated:
                raise BusinessToolRequestError(
                    f"实体「{entity}」已有已安装的专用状态操作「{dedicated}」，请直接用 propose_operation(\"{dedicated}\", ...) 办理。"
                )
            raise BusinessToolRequestError(
                f"实体「{entity}」不属于通用两态互逆状态开关范围（多态业务流转请使用已安装的专用状态操作，或走页面行内按钮 request_submit）。"
            )
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
        details = (
            ApprovalDetail("preserved", spec.label, disp_name),
            ApprovalDetail("change", "状态", f"{before_label} → {after_label}"),
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
            (target_label,),
            details=details,
            reconcile_key=key,
            reconciliation={"entity": entity, "id": record_id, "operation": "toggle_record_status", "body_digest": key},
            effect_projection=projection,
        )

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        actor_id = self.port.verify_actor(context)
        if actor_id != args.get("actor"):
            raise BusinessToolIdentityError("prepared status toggle belongs to a different actor")
        entity = args["entity"]
        spec = self.port.status_specs().get(entity)
        if spec is None:
            raise BusinessToolRequestError("two-state status spec is no longer available")
        record_id = args["record_id"]
        current = self.port.read_record(entity, record_id)
        if not isinstance(current, dict):
            raise BusinessMutationRefusedError(
                "target record is no longer readable before dispatch",
                reason="precondition_changed",
            )
        current_snap = _non_volatile_snapshot(current, spec.volatile_fields)
        if (_snapshot_preserved(args["snapshot"], current_snap, ignore=spec.volatile_fields)
                or _restricted_snapshot_digests(current, spec.volatile_fields) != args.get("restricted_field_digests", {})):
            raise BusinessMutationRefusedError(
                "current record changed after approval; prepare again",
                reason="precondition_changed",
            )

        uncertain_dispatch = False
        try:
            self.port.dispatch_status(spec, record_id, args["target_code"])
        except BusinessMutationIndeterminate:
            uncertain_dispatch = True

        try:
            after = self.port.read_record(entity, record_id)
        except Exception as exc:
            raise BusinessMutationIndeterminate(
                "status toggle acknowledged; same-actor readback failed",
                phase="postcondition",
                reconciliation={"entity": entity, "id": record_id},
            ) from exc
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
        drifted = _snapshot_preserved(current_snap, _non_volatile_snapshot(after, spec.volatile_fields), ignore=ignore)
        if not drifted and _restricted_snapshot_digests(after, spec.volatile_fields) != args.get("restricted_field_digests", {}):
            drifted = ["restricted_field"]
        if drifted:
            raise BusinessMutationIndeterminate(
                "status toggle altered unmentioned fields: " + ", ".join(drifted[:6]),
                phase="postcondition",
                reconciliation={"entity": entity, "id": record_id, "unverified_effect": "unrelated_field_preservation"},
            )
        self.port.invalidate_reads(frozenset({entity}))
        if uncertain_dispatch:
            verified_after_readback()
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
    "ConventionFieldSpec",
    "ConventionStatusHandler",
    "ConventionUpdateHandler",
    "ConventionUpdateSpec",
    "ConventionWriteEffects",
    "ConventionWritePort",
    "TwoStateStatusSpec",
    "convention_tool_definitions",
    "normalize_changes_argument",
]
