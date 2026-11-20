"""Yudao convention write port for flat 1:1 symmetric updates and two-state toggles.

Specs are compiled lazily from the deployment's published OpenAPI schema and optional
bound release source tree (`YudaoSource`), never hardcoded to benchmark prompts or IDs:

- ``update_record`` covers ``PUT /admin-api/<entity>/update`` endpoints whose 200 response
  is ``CommonResultBoolean``, that pair with ``GET /admin-api/<entity>/get``, carry ``id``
  on both, require no secret fields, contain only flat scalar or primitive-ID-array fields
  all present at top-level of ``RespVO``, and have zero untraceable or cross-Service
  cascades in ``YudaoSource``.
- ``toggle_record_status`` covers reversible two-state ``PUT /admin-api/<entity>/update-status``
  switches verified to use ``CommonStatusEnum`` (开启/关闭) or ``ProductSpuStatusEnum``
  (上架/下架, refusing -1 回收站).
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from threading import RLock
from zoneinfo import ZoneInfo

from web_operation_agent.business.compiled_writes import (
    CompiledAggregateUpdateSpec,
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
    _values_equal,
)

YUDAO_WRITE_ROUTE_CAPABILITY = WriteRouteCapability(
    compiled_api_writes=True,
    inline_row_status_submit=True,
    page_form_mode="prefill_handoff",
)
from web_operation_agent.business.errors import (
    BusinessToolIdentityError,
    BusinessToolPermissionError,
    BusinessToolReferenceError,
    BusinessToolRequestError,
    BusinessToolUnavailableError,
)
from web_operation_agent.business.write_pipeline import DEFAULT_SECRET_FIELD_PATTERN
from web_operation_agent.execution.submit_contract import scalar_text

from ..reads.client import YudaoReadRefused
from ..rules.compiled_write_source import compile_yudao_write_catalog
from .write_definitions import RECORD_CONTRACTS

_CACHE_LOCK = RLock()
_COMPILED_SPECS_CACHE: OrderedDict[tuple[int, int, int, str], dict] = OrderedDict()

_SECRET = DEFAULT_SECRET_FIELD_PATTERN
_NAME_CANDIDATES = (
    "name", "title", "no", "postName", "companyName", "nickname",
    "username", "employeeName", "voucherNumber", "digest", "subject",
    "code", "jobNumber", "signature", "label",
    "itemName", "spuName", "userNickname", "content",
)
_QUERY_CANDIDATES = (
    "name", "title", "no", "postName", "companyName", "nickname",
    "username", "search", "voucherNumber", "digest", "subject",
    "code", "jobNumber",
)
_CRM_BIZ_TYPES = {
    "crm/clue": 1,
    "crm/customer": 2,
    "crm/contact": 3,
    "crm/business": 4,
    "crm/contract": 5,
    "crm/product": 6,
    "crm/receivable": 7,
    "crm/receivable-plan": 8,
}
_DEDICATED_CREATES = {
    contract.entity: f"{contract.prefix}_create"
    for contract in RECORD_CONTRACTS
    if "create" in contract.operations
}
_DEDICATED_CREATES.update({
    "crm/receivable-plan": "crm_receivable_plan_create",
    "oa/announcement": "oa_announcement_create",
    "crm/follow-up-record": "crm_follow_up_create",
})
_DEDICATED_UPDATES = {
    contract.entity: f"{contract.prefix}_update"
    for contract in RECORD_CONTRACTS
    if "update" in contract.operations
}
_DEDICATED_STATUSES = {
    "promotion/coupon-template": "promotion_coupon_template_status",
    "promotion/seckill-activity": "promotion_seckill_activity_close",
    **{
        ent: "erp_document_status"
        for ent in (
            "erp/purchase-order",
            "erp/purchase-in",
            "erp/purchase-return",
            "erp/sale-order",
            "erp/sale-out",
            "erp/sale-return",
            "erp/stock-in",
            "erp/stock-out",
            "erp/stock-move",
            "erp/stock-check",
            "erp/finance-payment",
            "erp/finance-receipt",
        )
    },
}
_CANDIDATE_SUFFIX = re.compile(r"^(.+?)\s*[（(](?:ID\s*=|编号\s*|#)\s*([A-Za-z0-9_-]+)\s*[）)]$")
_API_IMPORT_RE = re.compile(r"import\s+(?:\*\s+as\s+(\w+)|\{([^}]*)\})\s+from\s+['\"]@/api/([^'\"]+)['\"]")
_PUT_URL_ARROW_RE = re.compile(
    r"\b(\w+)\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*(?::[^=>{]+)?=>\s*\{?\s*"
    r"(?:return\s+)?(?:await\s+)?request\.put\(\{\s*url:\s*[`'\"]([^`'\"?$]+)"
)
_PUT_URL_FN_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{\s*"
    r"(?:return\s+)?(?:await\s+)?request\.put\(\{\s*url:\s*[`'\"]([^`'\"?$]+)"
)
_FORM_ITEM_RE = re.compile(r"<el-form-item\b([^>]*)>(.*?)</el-form-item>", re.S)
_DICT_CALL_RE = re.compile(r"get(?:Int|Str|Bool)?DictOptions\(\s*DICT_TYPE\.([A-Z][A-Z0-9_]*)\s*\)")
_FEN_ASSIGN_RE = re.compile(
    r"(?:(\w+)\s*:\s*(?:convertToInteger|yuanToFen)\(\s*(?:formData(?:\.value)?|data)\.(\w+)"
    r"|(?:formData(?:\.value)?|data|item|row)\.(\w+)\s*=\s*(?:convertToInteger|yuanToFen)\()"
)
_ZHE_ASSIGN_RE = re.compile(r"(\w+)\s*:\s*[^,\n;{}]*formData(?:\.value)?\.(\w+)\s*\*\s*10(?:\.0)?\b")

_UI_META_CACHE: OrderedDict[tuple[str, str], tuple[dict, dict, dict]] = OrderedDict()
_UI_META_LOCK = RLock()


def _clean_desc_label(description: str) -> str:
    if not description:
        return ""
    head = re.split(r"[，,；;（(。!！\n]", description.strip(), maxsplit=1)[0].strip()
    return head[:40] if 0 < len(head) <= 40 else ""


def _compile_ui_metadata(source, ui=None) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, dict[str, tuple[int, str]]]]:
    """Extract (labels, dict_types, unit_scales) per entity from release Vue/TS source."""
    ui_src: Path | None = None
    release_key = ""
    if source is not None and getattr(source, "root", None):
        cand = Path(source.root) / "ui" / "src"
        if (cand / "views").is_dir():
            ui_src = cand
            release_key = str(getattr(source, "release", ""))
    if ui_src is None and ui is not None and getattr(ui, "root", None):
        cand = Path(ui.root) / "src"
        if (cand / "views").is_dir():
            ui_src = cand
            release_key = "ui"
    if ui_src is None:
        return {}, {}, {}
    ui_views = ui_src / "views"
    ui_api = ui_src / "api"
    key = (str(ui_src), release_key)
    with _UI_META_LOCK:
        cached = _UI_META_CACHE.get(key)
        if cached is not None:
            _UI_META_CACHE.move_to_end(key)
            return cached

    dict_map: dict[str, str] = {}
    dict_ts = ui_src / "utils" / "dict.ts"
    if dict_ts.is_file():
        raw_dict = dict_ts.read_text(encoding="utf-8", errors="ignore")
        enum_blocks = re.findall(r"export enum DICT_TYPE\s*\{([^}]+)\}", raw_dict)
        if enum_blocks:
            for const, code in re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*['\"]([a-z0-9_]+)['\"]", enum_blocks[0], re.M):
                dict_map[const] = code

    api_puts: dict[str, dict[str, str]] = {}
    if ui_api.is_dir():
        for ts_path in sorted(ui_api.rglob("*.ts")):
            rel = ts_path.relative_to(ui_api).as_posix()
            mod = rel[:-len("/index.ts")] if rel.endswith("/index.ts") else rel[:-len(".ts")]
            text = ts_path.read_text(encoding="utf-8", errors="ignore")
            fn_urls: dict[str, str] = {}
            for pattern in (_PUT_URL_ARROW_RE, _PUT_URL_FN_RE):
                for fn_name, url in pattern.findall(text):
                    cleaned = url.strip().removeprefix("/admin-api").strip("/")
                    if cleaned.endswith("/update"):
                        fn_urls[fn_name] = cleaned[:-len("/update")]
            if fn_urls:
                api_puts[mod] = fn_urls

    labels: dict[str, dict[str, str]] = {}
    dicts: dict[str, dict[str, str]] = {}
    scales: dict[str, dict[str, tuple[int, str]]] = {}

    for vue_path in sorted(ui_views.rglob("*.vue")):
        text = vue_path.read_text(encoding="utf-8", errors="ignore")
        if "update" not in text and "Update" not in text:
            continue
        imports: dict[str, str] = {}
        direct_fns: dict[str, str] = {}
        for alias, named, mod in _API_IMPORT_RE.findall(text):
            if alias:
                imports[alias] = mod
            elif named:
                for token in named.split(","):
                    parts = token.strip().split(" as ")
                    orig = parts[0].strip()
                    local = parts[-1].strip()
                    if orig and local:
                        direct_fns[local] = f"{mod}:{orig}"

        entities: set[str] = set()
        for obj, fn in re.findall(r"\b(\w+)\.(\w+)\(", text):
            mod = imports.get(obj)
            if mod and mod in api_puts and fn in api_puts[mod]:
                entities.add(api_puts[mod][fn])
        for local, target in direct_fns.items():
            if re.search(r"\b" + re.escape(local) + r"\s*\(", text):
                mod, orig = target.split(":", 1)
                if mod in api_puts and orig in api_puts[mod]:
                    entities.add(api_puts[mod][orig])
        if len(entities) != 1:
            continue
        entity = next(iter(entities))
        ent_labels = labels.setdefault(entity, {})
        ent_dicts = dicts.setdefault(entity, {})
        ent_scales = scales.setdefault(entity, {})

        for head, body in _FORM_ITEM_RE.findall(text):
            m_prop = re.search(r'\bprop="([A-Za-z][A-Za-z0-9_]*)"', head)
            if not m_prop:
                continue
            prop = m_prop.group(1)
            m_label = re.search(r'\blabel="([^"{]+)"', head)
            if m_label:
                lbl = " ".join(m_label.group(1).split()).rstrip(":：").strip()
                if lbl:
                    ent_labels.setdefault(prop, lbl)
            m_dict = _DICT_CALL_RE.search(body)
            if m_dict and m_dict.group(1) in dict_map:
                ent_dicts.setdefault(prop, dict_map[m_dict.group(1)])

        for p1, p2, p3 in _FEN_ASSIGN_RE.findall(text):
            field_name = p1 or p2 or p3
            if field_name:
                ent_scales[field_name] = (100, "元")
        for p1, p2 in _ZHE_ASSIGN_RE.findall(text):
            if p1 and p1 == p2 and re.search(r"data\." + re.escape(p1) + r"\s*/\s*10", text):
                ent_scales[p1] = (10, "折")

    result = (labels, dicts, scales)
    with _UI_META_LOCK:
        _UI_META_CACHE[key] = result
        while len(_UI_META_CACHE) > 4:
            _UI_META_CACHE.popitem(last=False)
    return result


class YudaoConventionWritePort:
    """Adapter runtime and lazy compiler for 4-category compiled API writes."""

    def __init__(self, client, goal_data):
        self.client = client
        self.goal_data = goal_data
        self._lock = RLock()
        self._creates: dict[str, CompiledCreateSpec] | None = None
        self._excluded_create_reasons: dict[str, str] = {}
        self._updates: dict[str, ConventionUpdateSpec] | None = None
        self._aggregates: dict[str, CompiledAggregateUpdateSpec] | None = None
        self._statuses: dict[str, TwoStateStatusSpec] | None = None
        self._relations: dict[str, tuple[CompiledRelationSpec, ...]] | None = None
        self._immutable_fields: dict[str, frozenset[str]] = {}
        self._nonnull_refusals: dict[str, tuple[tuple[str, str], ...]] = {}
        self._composite_hints: dict[tuple[str, str], dict] = {}

    def _docs(self) -> dict:
        raw = getattr(self.goal_data, "_documents", None)
        docs = raw() if callable(raw) else raw
        return docs if isinstance(docs, dict) else {}

    @staticmethod
    def _deref(schema, schemas: dict) -> dict:
        if not isinstance(schema, dict):
            return {}
        ref = schema.get("$ref")
        return schemas.get(ref.rsplit("/", 1)[-1], {}) if isinstance(ref, str) else schema

    @staticmethod
    def dedicated_tool_for(entity: str, kind: str) -> str | None:
        clean = entity.strip().strip("/")
        if kind == "create":
            return _DEDICATED_CREATES.get(clean)
        if kind == "update":
            return _DEDICATED_UPDATES.get(clean)
        if kind == "status":
            return _DEDICATED_STATUSES.get(clean)
        return None

    def excluded_create_reason(self, entity: str) -> str:
        with self._lock:
            if self._creates is None:
                self._compile_all()
            return self._excluded_create_reasons.get(entity.strip().strip("/"), "")

    def _compile_all(self) -> None:
        docs = self._docs()
        source = getattr(self.goal_data, "_source", None)
        ui = getattr(self.goal_data, "_ui", None)
        rel = str(getattr(source, "release", "")) if source is not None else ""
        cache_key = (id(docs), id(source), id(ui), rel)
        with _CACHE_LOCK:
            cached = _COMPILED_SPECS_CACHE.get(cache_key)
            if cached is not None:
                _COMPILED_SPECS_CACHE.move_to_end(cache_key)
        if cached is not None:
            catalog = cached
        else:
            catalog = compile_yudao_write_catalog(
                docs=docs,
                source=source,
                ui=ui,
                dedicated_creates=frozenset(_DEDICATED_CREATES),
                dedicated_updates=frozenset(_DEDICATED_UPDATES),
                dedicated_statuses=frozenset(_DEDICATED_STATUSES),
                ui_metadata_fn=_compile_ui_metadata,
            )
            with _CACHE_LOCK:
                _COMPILED_SPECS_CACHE[cache_key] = catalog
                _COMPILED_SPECS_CACHE.move_to_end(cache_key)
                while len(_COMPILED_SPECS_CACHE) > 8:
                    _COMPILED_SPECS_CACHE.popitem(last=False)

        self._creates = catalog["creates"]
        self._excluded_create_reasons = catalog["excluded_create_reasons"]
        self._updates = catalog["updates"]
        self._aggregates = catalog["aggregates"]
        self._statuses = catalog["statuses"]
        self._relations = catalog["relations"]
        self._immutable_fields = catalog["immutable_fields"]
        self._nonnull_refusals = catalog["nonnull_refusals"]

    def create_specs(self) -> dict[str, CompiledCreateSpec]:
        with self._lock:
            if self._creates is None:
                self._compile_all()
            return self._creates or {}

    def update_specs(self) -> dict[str, ConventionUpdateSpec]:
        with self._lock:
            if self._updates is None:
                self._compile_all()
            return self._updates or {}

    def aggregate_update_specs(self) -> dict[str, CompiledAggregateUpdateSpec]:
        with self._lock:
            if self._aggregates is None:
                self._compile_all()
            return self._aggregates or {}

    def status_specs(self) -> dict[str, TwoStateStatusSpec]:
        with self._lock:
            if self._statuses is None:
                self._compile_all()
            return self._statuses or {}

    def relation_specs(self) -> dict[str, tuple[CompiledRelationSpec, ...]]:
        with self._lock:
            if self._relations is None:
                self._compile_all()
            return self._relations or {}

    def verify_actor(self, context) -> str:
        if context is None:
            raise BusinessToolIdentityError("convention writes require a bound actor")
        context.require_live(application_id="yudao")
        info = self.client.get("system/auth/get-permission-info") or {}
        user = info.get("user") or {}
        if str(user.get("id")) != context.principal.actor_id or self.client.tenant != context.principal.tenant_id:
            raise BusinessToolIdentityError("native client no longer represents the bound actor")
        return str(user["id"])

    def _require_crm_write_if_needed(self, entity: str, record_id) -> None:
        biz_type = _CRM_BIZ_TYPES.get(entity)
        if biz_type is None:
            return
        info = self.client.get("system/auth/get-permission-info") or {}
        roles = info.get("roles")
        if isinstance(roles, list) and "crm_admin" in roles:
            return
        actor_id = str((info.get("user") or {}).get("id") or "")
        grants = self.client.get("crm/permission/list", bizType=biz_type, bizId=record_id)
        if not isinstance(grants, list) or not any(
            isinstance(g, dict) and str(g.get("userId")) == actor_id and str(g.get("level")) in {"1", "3"}
            for g in grants
        ):
            raise BusinessToolPermissionError("当前账号没有该 CRM 记录的修改权限（需负责人或读写团队成员权限）。")

    @staticmethod
    def _spec_id_param(entity: str, spec=None) -> str:
        if entity == "hrm/salary/employee-info":
            return "employeeId"
        if isinstance(spec, TwoStateStatusSpec) and spec.id_param:
            return spec.id_param
        if isinstance(spec, (CompiledAggregateUpdateSpec, CompiledRelationSpec)) and spec.id_field:
            return spec.id_field
        return getattr(spec, "id_field", "id") or "id"

    def _enrich_special_read(self, entity: str, data: dict) -> dict:
        if entity == "hrm/salary/employee-info":
            enriched = dict(data)
            is_adj = enriched.get("id") is not None or enriched.get("regularSalary") not in (None, 0)
            enriched.setdefault("recordType", 2 if is_adj else 1)
            if enriched.get("changeReason") is None:
                enriched["changeReason"] = 8 if is_adj else 0
            return enriched
        if entity == "product/spu" and isinstance(data.get("skus"), list) and data.get("name"):
            enriched = dict(data)
            spu_name = str(data["name"])
            new_skus = []
            changed = False
            for sku in data["skus"]:
                if isinstance(sku, dict) and sku.get("name") in (None, ""):
                    new_skus.append({**sku, "name": spu_name})
                    changed = True
                else:
                    new_skus.append(sku)
            if changed:
                enriched["skus"] = new_skus
                return enriched
        return data

    def read_record(self, entity: str, record_id, *, spec=None, composite_id: dict | None = None) -> dict | None:
        if spec is None and f"/admin-api/{entity}/get" not in (self._docs().get("paths") or {}):
            cand_spec = self.status_specs().get(entity)
            if isinstance(cand_spec, TwoStateStatusSpec) and getattr(cand_spec, "read_list_path", ""):
                spec = cand_spec
        id_field = self._spec_id_param(entity, spec)
        if isinstance(spec, TwoStateStatusSpec) and getattr(spec, "read_list_path", ""):
            try:
                raw_rows = self.client.get(spec.read_list_path)
            except YudaoReadRefused as exc:
                if exc.permission_denied or exc.code == 401:
                    raise BusinessToolPermissionError("当前账号无权读取目标记录列表。") from exc
                if exc.read_failed:
                    raise BusinessToolUnavailableError("目标记录列表接口当前不可用。") from exc
                raise BusinessToolRequestError(f"目标记录列表不可读（code={exc.code}）。") from exc
            rows = raw_rows if isinstance(raw_rows, list) else (raw_rows or {}).get("list") if isinstance(raw_rows, dict) else []
            for row in rows or ():
                if isinstance(row, dict) and str(row.get(id_field)) == str(record_id):
                    return self._enrich_special_read(entity, row)
            return None

        if entity == "product/spu":
            path = "product/spu/get-detail"
        elif isinstance(spec, TwoStateStatusSpec) and getattr(spec, "read_path", ""):
            path = spec.read_path
        else:
            path = f"{entity}/get"

        qparams = dict(composite_id or self._composite_hints.get((entity, str(record_id))) or {})
        qparams[id_field] = record_id
        if entity.startswith("fms/") and "accountSetId" not in qparams:
            get_op = ((self._docs().get("paths") or {}).get(f"/admin-api/{path}") or {}).get("get") or {}
            req_q = {
                p.get("name") for p in (get_op.get("parameters") or ())
                if isinstance(p, dict) and p.get("in") == "query" and p.get("required") is True
            }
            if "accountSetId" in req_q:
                for aset_id in self._account_set_ids():
                    try:
                        trial = self.client.get(path, **{**qparams, "accountSetId": aset_id})
                    except YudaoReadRefused:
                        continue
                    if isinstance(trial, dict):
                        self._composite_hints[(entity, str(record_id))] = {"accountSetId": aset_id}
                        return self._enrich_special_read(entity, trial)
                return None
        try:
            data = self.client.get(path, **qparams)
        except YudaoReadRefused as exc:
            if exc.permission_denied or exc.code == 401:
                raise BusinessToolPermissionError("当前账号无权读取目标记录详情。") from exc
            if exc.read_failed:
                raise BusinessToolUnavailableError("目标记录详情接口当前不可用。") from exc
            raise BusinessToolRequestError(f"目标记录详情不可读（code={exc.code}）。") from exc
        if not isinstance(data, dict):
            return None
        if entity.startswith("fms/") and data.get("accountSetId") is not None:
            self._composite_hints[(entity, str(record_id))] = {"accountSetId": data["accountSetId"]}
        return self._enrich_special_read(entity, data)

    def _account_set_ids(self) -> list[int]:
        docs = self._docs()
        paths = docs.get("paths") or {}
        raw = None
        if "/admin-api/fms/config/account-set/simple-list" in paths:
            raw = self.client.get("fms/config/account-set/simple-list")
        elif "/admin-api/fms/config/account-set/page" in paths:
            res = self.client.get("fms/config/account-set/page", pageNo=1, pageSize=50)
            raw = res.get("list") if isinstance(res, dict) else None
        elif "/admin-api/fms/config/account-set/list" in paths:
            raw = self.client.get("fms/config/account-set/list")
        if isinstance(raw, dict):
            raw = raw.get("list")
        return [r["id"] for r in (raw or ()) if isinstance(r, dict) and type(r.get("id")) is int]

    def resolve_target(self, entity: str, target: str, *, spec) -> dict:
        raw = target.strip()
        if not raw:
            raise BusinessToolRequestError("请提供目标记录的确切业务名称或单号。")
        m_cand = _CANDIDATE_SUFFIX.fullmatch(raw)
        clean_name = m_cand.group(1).strip() if m_cand else raw
        hint_id = m_cand.group(2) if m_cand else None
        if not m_cand and clean_name.isdigit():
            raise BusinessToolRequestError("target 请传目标记录的确切业务名称、单号或 '名称（ID=编号）'，不能只传裸数字 ID。")

        matched_id = None
        matched_comp: dict[str, object] = {}
        id_field = getattr(spec, "id_field", "id") or "id"
        docs = self._docs()
        paths = docs.get("paths") or {}
        page_path = f"/admin-api/{entity}/page"
        list_path = f"/admin-api/{entity}/list"
        simple_path = f"/admin-api/{entity}/simple-list"
        rows: list[dict] = []

        if page_path in paths:
            get_op = (paths[page_path] or {}).get("get") or {}
            q_names = {
                p["name"] for p in (get_op.get("parameters") or ())
                if isinstance(p, dict) and p.get("in") == "query" and isinstance(p.get("name"), str)
            }
            req_missing = [
                p["name"] for p in (get_op.get("parameters") or ())
                if isinstance(p, dict) and p.get("in") == "query" and p.get("required") is True
                and p.get("name") not in {"pageNo", "pageSize"}
            ]
            if not req_missing:
                narrow = {}
                for qk in (*spec.name_fields, *_QUERY_CANDIDATES):
                    if qk in q_names:
                        narrow[qk] = clean_name
                        break
                res = self.client.get(f"{entity}/page", pageNo=1, pageSize=100, **narrow)
                if isinstance(res, dict) and isinstance(res.get("list"), list):
                    rows = [r for r in res["list"] if isinstance(r, dict)]
                if not rows and narrow:
                    res_all = self.client.get(f"{entity}/page", pageNo=1, pageSize=100)
                    if isinstance(res_all, dict) and isinstance(res_all.get("list"), list):
                        rows = [r for r in res_all["list"] if isinstance(r, dict)]
            elif req_missing == ["accountSetId"]:
                for aset_id in self._account_set_ids():
                    res = self.client.get(f"{entity}/page", accountSetId=aset_id, pageNo=1, pageSize=100)
                    if isinstance(res, dict) and isinstance(res.get("list"), list):
                        for r in res["list"]:
                            if isinstance(r, dict):
                                row_copy = dict(r)
                                row_copy.setdefault("accountSetId", aset_id)
                                rows.append(row_copy)
            elif set(req_missing) == {"accountSetId", "auxiliaryTypeId"} and entity == "fms/config/auxiliary-item":
                type_path = "/admin-api/fms/config/auxiliary-type/simple-list"
                type_ep = "fms/config/auxiliary-type/simple-list" if type_path in paths else "fms/config/auxiliary-type/list"
                for aset_id in self._account_set_ids():
                    raw_types = self.client.get(type_ep, accountSetId=aset_id)
                    type_rows = raw_types.get("list") if isinstance(raw_types, dict) else raw_types
                    for trow in type_rows or ():
                        tid = trow.get("id") if isinstance(trow, dict) else None
                        if tid is None:
                            continue
                        res = self.client.get(
                            f"{entity}/page",
                            accountSetId=aset_id,
                            auxiliaryTypeId=tid,
                            pageNo=1,
                            pageSize=100,
                        )
                        if isinstance(res, dict) and isinstance(res.get("list"), list):
                            for r in res["list"]:
                                if isinstance(r, dict):
                                    row_copy = dict(r)
                                    row_copy.setdefault("accountSetId", aset_id)
                                    rows.append(row_copy)
            elif req_missing == ["scope"] and entity == "oa/file-node":
                for scope_val in ("my", "shared"):
                    res = self.client.get(f"{entity}/page", scope=scope_val, pageNo=1, pageSize=100)
                    if isinstance(res, dict) and isinstance(res.get("list"), list) and res["list"]:
                        rows = [r for r in res["list"] if isinstance(r, dict)]
                        break
            elif req_missing == ["tabType"] and entity == "product/spu":
                res = self.client.get("product/spu/page", tabType=0, pageNo=1, pageSize=100)
                if isinstance(res, dict) and isinstance(res.get("list"), list):
                    rows = [r for r in res["list"] if isinstance(r, dict)]
            elif req_missing == ["projectId"] and "/admin-api/pms/pm/project/page" in paths:
                proj_res = self.client.get("pms/pm/project/page", pageNo=1, pageSize=20)
                for proj in (proj_res.get("list") or ()) if isinstance(proj_res, dict) else ():
                    pid = proj.get("id")
                    if pid is None:
                        continue
                    res = self.client.get(f"{entity}/page", projectId=pid, pageNo=1, pageSize=100)
                    if isinstance(res, dict) and isinstance(res.get("list"), list):
                        rows.extend(r for r in res["list"] if isinstance(r, dict))
        if not rows:
            for ep in (list_path, simple_path, f"/admin-api/{entity}/list-all-simple", f"/admin-api/{entity}/my-page"):
                if ep not in paths:
                    continue
                get_op = (paths[ep] or {}).get("get") or {}
                ep_req = [
                    p["name"] for p in (get_op.get("parameters") or ())
                    if isinstance(p, dict) and p.get("in") == "query" and p.get("required") is True
                    and p.get("name") not in {"pageNo", "pageSize"}
                ]
                if ep_req:
                    continue
                qargs = {"pageNo": 1, "pageSize": 100} if ep.endswith("-page") else {}
                res = self.client.get(ep.removeprefix("/admin-api/"), **qargs)
                items = res if isinstance(res, list) else (res.get("list") if isinstance(res, dict) else None)
                if isinstance(items, list) and items:
                    rows = [r for r in items if isinstance(r, dict)]
                    break
        if not rows and entity == "ai/knowledge/segment" and "/admin-api/ai/knowledge/document/page" in paths:
            doc_res = self.client.get("ai/knowledge/document/page", pageNo=1, pageSize=20)
            for doc in (doc_res.get("list") or ()) if isinstance(doc_res, dict) else ():
                did = doc.get("id")
                if did is None:
                    continue
                seg_res = self.client.get("ai/knowledge/segment/page", documentId=did, pageNo=1, pageSize=100)
                if isinstance(seg_res, dict) and isinstance(seg_res.get("list"), list):
                    rows.extend(r for r in seg_res["list"] if isinstance(r, dict))
        if not rows and id_field == "employeeId" and "/admin-api/hrm/employee/page" in paths:
            res = self.client.get("hrm/employee/page", name=clean_name, pageNo=1, pageSize=100)
            items = res.get("list") if isinstance(res, dict) else None
            if not items:
                res2 = self.client.get("hrm/employee/page", pageNo=1, pageSize=100)
                items = res2.get("list") if isinstance(res2, dict) else None
            rows = [
                {**r, "employeeId": r["id"], "employeeName": r.get("name")}
                for r in (items or ()) if isinstance(r, dict) and r.get("id") is not None
            ]
        if not rows and id_field == "documentId" and "/admin-api/pms/kb/document/search-page" in paths:
            res = self.client.get("pms/kb/document/search-page", keyword=clean_name, pageNo=1, pageSize=100)
            items = res.get("list") if isinstance(res, dict) else None
            if not items:
                res2 = self.client.get("pms/kb/document/search-page", pageNo=1, pageSize=100)
                items = res2.get("list") if isinstance(res2, dict) else None
            rows = [
                {**r, "documentId": r["id"], "title": r.get("title"), "name": r.get("title")}
                for r in (items or ()) if isinstance(r, dict) and r.get("id") is not None
            ]

        comp_fields = tuple(getattr(spec, "composite_id_fields", ()) or ())
        folded = clean_name.casefold()
        exact_rows = []
        for row in rows:
            rid = row.get(id_field)
            if rid is None:
                continue
            if hint_id is not None and str(rid) != hint_id:
                continue
            row_names = [
                str(row.get(k)).strip()
                for k in (*spec.name_fields, *_NAME_CANDIDATES)
                if isinstance(row.get(k), (str, int)) and not isinstance(row.get(k), bool) and str(row.get(k)).strip()
            ]
            if row.get("voucherWordName") and row.get("voucherNumber") is not None:
                vw = str(row["voucherWordName"]).strip()
                vn = str(row["voucherNumber"]).strip()
                row_names.extend((f"{vw}-{vn}", f"{vw}{vn}", f"{vw}字第{vn}号"))
            if any(n.casefold() == folded for n in row_names):
                exact_rows.append(row)

        if len(exact_rows) == 1:
            chosen_row = exact_rows[0]
            matched_id = chosen_row[id_field]
            matched_comp = {cf: chosen_row[cf] for cf in comp_fields if cf != id_field and chosen_row.get(cf) is not None}
            if entity.startswith("fms/") and chosen_row.get("accountSetId") is not None:
                matched_comp.setdefault("accountSetId", chosen_row["accountSetId"])
            if matched_comp:
                self._composite_hints[(entity, str(matched_id))] = dict(matched_comp)
        elif len(exact_rows) > 1:
            choices = tuple(
                f"{exact_rows[i].get(spec.name_fields[0]) or clean_name}（ID={exact_rows[i][id_field]}）"
                for i in range(min(6, len(exact_rows)))
            )
            raise BusinessToolReferenceError(
                f"找到多条名为「{clean_name}」的{spec.label}，请向用户确认具体哪一条",
                entity=entity,
                choices=choices,
            )
        else:
            try:
                found = self.goal_data.find(entity, clean_name, limit=8)
            except Exception:
                found = ()
            exact_refs = [
                r for r in found
                if (hint_id is None or str(r.id) == hint_id)
                and (r.name.strip().casefold() == folded or r.display.strip().casefold() == folded)
            ]
            if len(exact_refs) == 1:
                matched_id = exact_refs[0].id
            else:
                near = tuple(dict.fromkeys((
                    *(r.display for r in found),
                    *(
                        f"{row.get(spec.name_fields[0]) or row.get('name') or row.get('title') or row.get('no')}（ID={row.get(id_field)}）"
                        for row in rows[:6]
                        if row.get(id_field) is not None
                    ),
                )))[:6]
                raise BusinessToolReferenceError(
                    f"未找到唯一匹配的{spec.label}「{target}」，请核对确切业务名称或单号",
                    entity=entity,
                    choices=near,
                )

        record = self.read_record(entity, matched_id, spec=spec, composite_id=matched_comp)
        if not isinstance(record, dict) or str(record.get(id_field)) != str(matched_id):
            raise BusinessToolRequestError(f"目标{spec.label}详情当前不可读。")
        self._require_crm_write_if_needed(entity, matched_id)
        if isinstance(spec, (ConventionUpdateSpec, CompiledAggregateUpdateSpec)):
            self.check_record_updatable(entity, record)
        return record

    def check_record_updatable(self, entity: str, record: dict) -> None:
        self.update_specs()
        self.aggregate_update_specs()
        if entity == "system/role" and record.get("type") == 1:
            raise BusinessToolRequestError("内置系统角色禁止通过通用修改接口变更。")
        if entity == "pms/kb/group" and record.get("type") in (1, 2):
            raise BusinessToolRequestError("内置系统知识库分组禁止通过通用修改接口变更。")
        if entity.startswith("erp/") and record.get("status") == 20:
            raise BusinessToolRequestError("当前 ERP 单据已审核通过，不可直接修改。")
        if entity == "mes/dv/check-plan" and record.get("status") not in (None, 0):
            raise BusinessToolRequestError("当前点检计划已启用，仅草稿状态可直接修改。")
        if entity == "mes/wm/stocktaking-plan" and record.get("status") not in (None, 1):
            raise BusinessToolRequestError("当前盘点方案已启用，仅停用状态可直接修改。")
        if entity.startswith("wms/") and entity != "wms/item" and "status" in record and record.get("status") not in (None, 0):
            raise BusinessToolRequestError("当前 WMS 单据已非草稿状态，不可直接修改。")
        if entity.startswith("crm/") and "auditStatus" in record and record.get("auditStatus") not in (None, 0, 10):
            raise BusinessToolRequestError("当前 CRM 记录已审批完成，不可直接修改。")
        if entity.startswith("oa/") and entity.endswith("-apply"):
            if record.get("processInstanceId") or record.get("status") not in (None, -1, 0):
                raise BusinessToolRequestError("当前 OA 申请已进入审批流程，不可直接修改。")
        for prop, msg in self._nonnull_refusals.get(entity, ()):
            if record.get(prop) is not None:
                raise BusinessToolRequestError(f"目标记录当前不可直接修改：{msg}")

    def _zone(self) -> ZoneInfo:
        tz = getattr(self.goal_data, "time_zone", "Asia/Shanghai")
        tz_name = tz() if callable(tz) else tz
        return ZoneInfo(tz_name)

    def _format_epoch_ms(self, val) -> str:
        if type(val) is int:
            return datetime.fromtimestamp(val / 1000.0, tz=self._zone()).strftime("%Y-%m-%d %H:%M:%S")
        return "（空）" if val in (None, "") else str(val)

    def _parse_epoch_ms(self, raw_text: str, *, shown_label: str) -> tuple[int, str]:
        s_val = raw_text.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s_val):
            s_val = s_val + " 00:00:00"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", s_val):
            s_val = re.sub(r"\s+", " ", s_val) + ":00"
        else:
            s_val = re.sub(r"\s+", " ", s_val)
        try:
            dt = datetime.strptime(s_val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._zone())
        except ValueError as exc:
            raise BusinessToolRequestError(
                f"字段「{shown_label}」时间格式须为 YYYY-MM-DD HH:MM:SS（或 YYYY-MM-DD）。"
            ) from exc
        return int(dt.timestamp() * 1000), dt.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_scaled(val, scale: int, unit: str) -> str:
        if val is None or val == "":
            return "（空）"
        try:
            human = scalar_text(Decimal(str(val)) / Decimal(scale))
            return f"{human}{unit}（接口值 {val}）" if unit else human
        except (InvalidOperation, ValueError):
            return str(val)

    def _enum_labels_for(self, enum_cls: str) -> dict[str, str]:
        source = getattr(self.goal_data, "_source", None)
        if source is not None and hasattr(source, "enum_value_labels"):
            return (source.enum_value_labels() or {}).get(enum_cls) or {}
        return {}

    def _resolve_dict_choice(self, dict_type: str, raw_value, kind: str) -> tuple[int | str, str, dict]:
        s_val = str(raw_value).strip()
        if dict_type.startswith("__java_enum__:"):
            enum_cls = dict_type.split(":", 1)[1]
            labels = self._enum_labels_for(enum_cls)
            matches = [(k, lbl) for k, lbl in labels.items() if str(lbl).strip() == s_val]
            if len(matches) != 1:
                allowed = "、".join(str(v) for v in list(labels.values())[:20])
                raise BusinessToolRequestError(
                    f"枚举「{enum_cls}」中没有唯一匹配的选项「{raw_value}」（可选标签：{allowed or '无'}；请传中文标签而非数字编码）。"
                )
            code_raw, label = matches[0]
            if kind == "integer":
                if not re.fullmatch(r"-?\d+", str(code_raw).strip()):
                    raise BusinessToolRequestError(f"枚举「{enum_cls}」编码不是整数：{code_raw!r}")
                code: int | str = int(code_raw)
            else:
                code = str(code_raw)
            return code, label, {"kind": "java_enum", "enum_cls": enum_cls, "label": label, "code": code}

        if dict_type == "common_status":
            s_val = {"启用": "开启", "正常": "开启", "停用": "关闭", "禁用": "关闭"}.get(s_val, s_val)
        rows = self.client.get("system/dict-data/simple-list") or []
        if not isinstance(rows, list):
            raise BusinessToolRequestError("当前系统字典不可读，无法核验枚举选项。")
        choices = [r for r in rows if isinstance(r, dict) and r.get("dictType") == dict_type]
        exact = [r for r in choices if str(r.get("label") or "").strip() == s_val]
        if len(exact) != 1:
            allowed = "、".join(str(r.get("label")) for r in choices[:20] if r.get("label"))
            raise BusinessToolRequestError(
                f"字典「{dict_type}」中没有唯一匹配的选项「{raw_value}」（可选标签：{allowed or '无'}；请传中文标签而非数字编码）。"
            )
        code_raw = str(exact[0].get("value", "")).strip()
        if kind == "integer":
            if not re.fullmatch(r"-?\d+", code_raw):
                raise BusinessToolRequestError(f"字典「{dict_type}」编码不是整数：{code_raw!r}")
            code = int(code_raw)
        else:
            code = code_raw
        label = str(exact[0]["label"]).strip()
        return code, label, {"kind": "dict", "dict_type": dict_type, "label": label, "code": code}

    def _dict_label_for(self, dict_type: str, code) -> str:
        if code is None:
            return "（空）"
        if dict_type.startswith("__java_enum__:"):
            labels = self._enum_labels_for(dict_type.split(":", 1)[1])
            return labels.get(str(code), str(code))
        rows = self.client.get("system/dict-data/simple-list") or []
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("dictType") == dict_type and str(r.get("value")) == str(code):
                    return str(r.get("label"))
        return str(code)

    def _ref_endpoint_for(self, ref_entity: str) -> tuple[str, tuple[str, ...]]:
        known = {
            "system/dept": ("system/dept/simple-list", ("name",)),
            "system/user": ("system/user/simple-list", ("nickname", "username")),
            "hrm/employee": ("hrm/employee/simple-list", ("name", "jobNumber")),
            "system/post": ("system/post/simple-list", ("name",)),
            "system/role": ("system/role/simple-list", ("name",)),
            "system/menu": ("system/menu/list-all-simple", ("name",)),
            "pms/pm/work-item-status": ("pms/pm/work-item-status/list", ("name",)),
            "oa/file-node": ("oa/file-node/directory-list", ("name",)),
        }
        if ref_entity in known:
            return known[ref_entity]
        docs = self._docs()
        paths = docs.get("paths") or {}
        for tail in ("simple-list", "simple-all-list", "list-all-simple", "list", "page"):
            cand = f"/admin-api/{ref_entity}/{tail}"
            if cand in paths and isinstance((paths[cand] or {}).get("get"), dict):
                return f"{ref_entity}/{tail}", (
                    "name", "postName", "companyName", "employeeName", "nickname",
                    "title", "username", "subject", "label", "code", "no", "digest",
                )
        raise BusinessToolRequestError(f"不支持解析关联类型：{ref_entity}")

    def _list_ref_rows(self, ref_entity: str, *, query_ctx: dict | None = None) -> tuple[str, tuple[str, ...], list[dict], dict]:
        list_path, name_keys = self._ref_endpoint_for(ref_entity)
        docs = self._docs()
        paths = docs.get("paths") or {}
        get_op = ((paths.get(f"/admin-api/{list_path}") or {}).get("get") or {})
        req_params = [
            p["name"] for p in (get_op.get("parameters") or ())
            if isinstance(p, dict) and p.get("in") == "query" and p.get("required") is True
        ]
        call_params: dict[str, object] = {}
        if list_path.endswith("/page"):
            call_params["pageNo"] = 1
            call_params["pageSize"] = 100
        q_ctx = dict(query_ctx or {})
        missing_acct = False
        for rp in req_params:
            if rp in call_params:
                continue
            if rp in q_ctx and q_ctx[rp] is not None:
                call_params[rp] = q_ctx[rp]
            elif rp == "accountSetId":
                missing_acct = True
            else:
                raise BusinessToolRequestError(f"关联列表 {list_path} 缺少必需查询参数 {rp}。")

        items_acc: list[dict] = []
        if missing_acct:
            for aset_id in self._account_set_ids():
                p_try = {**call_params, "accountSetId": aset_id}
                raw_rows = self.client.get(list_path, **p_try)
                sub = raw_rows if isinstance(raw_rows, list) else (raw_rows or {}).get("list") if isinstance(raw_rows, dict) else []
                items_acc.extend(r for r in (sub or ()) if isinstance(r, dict) and type(r.get("id")) is int)
            return list_path, name_keys, items_acc, call_params

        raw_rows = self.client.get(list_path, **call_params) if call_params else self.client.get(list_path)
        items = raw_rows if isinstance(raw_rows, list) else (raw_rows or {}).get("list") if isinstance(raw_rows, dict) else None
        if not isinstance(items, list):
            raise BusinessToolRequestError(f"关联列表 {list_path} 当前不可读。")
        rows = [r for r in items if isinstance(r, dict) and type(r.get("id")) is int]
        return list_path, name_keys, rows, call_params

    def _resolve_single_ref(
        self,
        ref_entity: str,
        token: str,
        *,
        allow_root: bool = False,
        query_ctx: dict | None = None,
    ) -> tuple[int, str, dict]:
        cleaned = token.strip()
        m_cand = _CANDIDATE_SUFFIX.fullmatch(cleaned)
        name = m_cand.group(1).strip() if m_cand else cleaned
        hint_id = m_cand.group(2) if m_cand else None
        if allow_root and name == "根节点":
            return 0, "根节点", {"kind": "root", "entity": ref_entity, "id": 0, "name": "根节点"}
        if not m_cand and name.isdigit():
            raise BusinessToolRequestError(f"关联对象（{ref_entity}）请传确切业务名称或层级路径，不能传裸数字 ID。")

        if ref_entity == "system/area":
            from .write_inputs import _area
            area_id = _area(self.client, name)
            return area_id, name, {"kind": "area", "label": name, "code": area_id}

        list_path, name_keys, rows, call_params = self._list_ref_rows(ref_entity, query_ctx=query_ctx)
        primary_key = next((k for k in name_keys if any(r.get(k) for r in rows)), name_keys[0])

        if ref_entity in {"system/dept", "oa/file-node"} and "/" in name:
            by_id = {r["id"]: r for r in rows}
            def full_path(row: dict) -> str:
                parts = [str(row.get("name") or "").strip()]
                pid = row.get("parentId")
                guard = 0
                while pid and pid in by_id and guard < 16:
                    parts.append(str(by_id[pid].get("name") or "").strip())
                    pid = by_id[pid].get("parentId")
                    guard += 1
                return "/".join(reversed(parts))
            matches = [
                r for r in rows
                if full_path(r) == name and (hint_id is None or str(r["id"]) == hint_id)
            ]
        else:
            matches = [
                r for r in rows
                if (hint_id is None or str(r["id"]) == hint_id)
                and any(str(r.get(k) or "").strip() == name for k in name_keys)
            ]
        if len(matches) != 1:
            sample = "、".join(str(r.get(primary_key)) for r in rows[:15] if r.get(primary_key))
            raise BusinessToolRequestError(
                f"在 {ref_entity} 中未找到唯一匹配的「{token}」（可选示例：{sample}）。"
            )
        chosen = matches[0]
        label = next((str(chosen.get(k)).strip() for k in name_keys if chosen.get(k)), name)
        return chosen["id"], label, {
            "kind": "relation",
            "list_path": list_path,
            "query_params": call_params,
            "name_keys": list(name_keys),
            "name_key": primary_key,
            "id": chosen["id"],
            "name": label,
        }

    def encode_field(
        self,
        spec,
        field: ConventionFieldSpec,
        raw_value,
        current_record: dict,
        *,
        allow_status_field: bool = False,
    ) -> tuple[object, str, str, dict | None]:
        if (
            not allow_status_field
            and field.name == "status"
            and isinstance(spec, (ConventionUpdateSpec, CompiledAggregateUpdateSpec))
            and spec.entity in self.status_specs()
        ):
            raise BusinessToolRequestError(
                f"实体「{spec.entity}」的启用/上下架状态开关独立于详情修改，请改用 toggle_record_status 操作。"
            )
        shown_label = field.label or field.name
        if isinstance(spec, (ConventionUpdateSpec, CompiledAggregateUpdateSpec)) and field.name in self._immutable_fields.get(spec.entity, ()):
            raise BusinessToolRequestError(
                f"字段「{shown_label}（{field.name}）」在 {spec.entity}/update 接口中不可修改"
                "（创建后锁定或需走专用转移/变更入口），请改走对应专用操作或页面。"
            )
        old_raw = current_record.get(field.name) if isinstance(current_record, dict) else None
        ref_ctx = {"accountSetId": current_record["accountSetId"]} if isinstance(current_record, dict) and current_record.get("accountSetId") is not None else None

        if raw_value is None:
            if field.required:
                raise BusinessToolRequestError(f"字段「{shown_label}」为必填项，不能清空为 null。")
            return None, "（空）" if old_raw in (None, "") else str(old_raw), "（清空）", None

        # 1. Reference fields (single FK or ID array)
        if field.reference_entity:
            allow_root = getattr(spec, "entity", "") in {"system/dept", "oa/file-node", "product/category"} and field.name == "parentId"
            if field.kind == "id_array":
                if not isinstance(raw_value, list):
                    raise BusinessToolRequestError(f"字段「{shown_label}」须传名称数组。")
                ids: list[int] = []
                labels: list[str] = []
                sub_bindings: list[dict] = []
                for item in raw_value:
                    rid, rlabel, b = self._resolve_single_ref(field.reference_entity, str(item), query_ctx=ref_ctx)
                    if rid in ids:
                        raise BusinessToolRequestError(f"字段「{shown_label}」包含重复关联项：{rlabel}")
                    ids.append(rid)
                    labels.append(rlabel)
                    sub_bindings.append(b)
                old_disp = str(old_raw) if old_raw else "[]"
                new_disp = "、".join(labels) if labels else "[]"
                return ids, old_disp, new_disp, {"kind": "batch", "items": sub_bindings}
            rid, rlabel, b = self._resolve_single_ref(
                field.reference_entity,
                str(raw_value),
                allow_root=allow_root,
                query_ctx=ref_ctx,
            )
            old_disp = "（空）" if old_raw is None else str(old_raw)
            return rid, old_disp, f"{rlabel}（ID={rid}）", b

        # 2. Dictionary-bound & Java @InEnum fields
        if field.enum_type:
            code, dlabel, b = self._resolve_dict_choice(field.enum_type, raw_value, field.kind)
            old_disp = self._dict_label_for(field.enum_type, old_raw)
            return code, old_disp, dlabel, b

        # 3. Scaled integer fields (e.g. unit_scale=100 fen, unit_scale=10 zhe)
        if field.unit_scale == 100:
            old_disp = self._format_scaled(old_raw, 100, field.display_unit or "元")
            s_val = str(raw_value).strip()
            if s_val in {"0", "0.0", "0.00"}:
                return 0, old_disp, "0元（接口值 0）", None
            m_yuan = re.fullmatch(r"(-?\d+(?:\.\d{1,2})?)\s*(?:元|块)", s_val)
            if m_yuan:
                fen = int((Decimal(m_yuan.group(1)) * Decimal(100)).to_integral_value())
                return fen, old_disp, self._format_scaled(fen, 100, "元"), None
            m_fen = re.fullmatch(r"(-?\d+)\s*分", s_val)
            if m_fen:
                fen = int(m_fen.group(1))
                return fen, old_disp, self._format_scaled(fen, 100, "元"), None
            raise BusinessToolRequestError(
                f"字段「{shown_label}（{field.name}）」在接口中按「分」存储（当前值：{old_disp}）。"
                "为避免元/分百倍歧义，请传带明确单位的字符串，例如 '25元' 或 '2500分'。"
            )

        if field.unit_scale == 10:
            old_disp = self._format_scaled(old_raw, 10, field.display_unit or "折")
            s_val = str(raw_value).strip()
            m_zhe = re.fullmatch(r"(\d+(?:\.\d)?)\s*折", s_val)
            if m_zhe:
                scaled = int((Decimal(m_zhe.group(1)) * Decimal(10)).to_integral_value())
                return scaled, old_disp, self._format_scaled(scaled, 10, "折"), None
            m_pct = re.fullmatch(r"(\d+)\s*%", s_val)
            if m_pct:
                scaled = int(m_pct.group(1))
                return scaled, old_disp, self._format_scaled(scaled, 10, "折"), None
            raise BusinessToolRequestError(
                f"字段「{shown_label}（{field.name}）」在接口中按十分比整数存储（如 8.5折=85，当前值：{old_disp}）；"
                "请传带明确单位的字符串，例如 '8.5折' 或 '85%'。"
            )

        # 4. Epoch-ms timestamps (LocalDateTime in Yudao Jackson PUT/POST deserialization always expects epoch ms)
        if field.date_kind == "epoch_ms":
            old_disp = old_raw.strip() if isinstance(old_raw, str) and old_raw.strip() else self._format_epoch_ms(old_raw)
            if type(raw_value) is int and raw_value > 100_000_000_000:
                return raw_value, old_disp, self._format_epoch_ms(raw_value), None
            ms, formatted = self._parse_epoch_ms(str(raw_value), shown_label=shown_label)
            return ms, old_disp, formatted, None

        if field.date_kind == "date":
            s_val = str(raw_value).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s_val):
                raise BusinessToolRequestError(f"字段「{shown_label}」日期格式须为 YYYY-MM-DD。")
            try:
                datetime.strptime(s_val, "%Y-%m-%d")
            except ValueError as exc:
                raise BusinessToolRequestError(f"字段「{shown_label}」不是有效公历日期：{s_val}") from exc
            return s_val, "（空）" if old_raw in (None, "") else str(old_raw), s_val, None

        # 5. Ordinary scalar / primitive array types
        if field.kind == "boolean":
            if type(raw_value) is bool:
                b_val = raw_value
            elif str(raw_value).strip().lower() in {"true", "是", "开启", "启用"}:
                b_val = True
            elif str(raw_value).strip().lower() in {"false", "否", "关闭", "停用"}:
                b_val = False
            else:
                raise BusinessToolRequestError(f"字段「{shown_label}」需要布尔值（true/false）。")
            return b_val, "（空）" if old_raw is None else str(old_raw), str(b_val), None

        if field.kind == "integer":
            if isinstance(raw_value, bool) or not re.fullmatch(r"-?\d+", str(raw_value).strip()):
                raise BusinessToolRequestError(f"字段「{shown_label}」需要整数。")
            iv = int(str(raw_value).strip())
            return iv, "（空）" if old_raw is None else str(old_raw), str(iv), None

        if field.kind == "number":
            if isinstance(raw_value, bool):
                raise BusinessToolRequestError(f"字段「{shown_label}」需要数值。")
            try:
                dec = Decimal(str(raw_value).strip())
            except (InvalidOperation, ValueError) as exc:
                raise BusinessToolRequestError(f"字段「{shown_label}」数值格式无效：{raw_value!r}") from exc
            fv = int(dec) if dec == dec.to_integral_value() else float(dec)
            return fv, "（空）" if old_raw is None else scalar_text(old_raw), scalar_text(dec), None

        if field.kind == "id_array":
            if not isinstance(raw_value, list):
                raise BusinessToolRequestError(f"字段「{shown_label}」需要数组。")
            sample = old_raw[0] if isinstance(old_raw, list) and old_raw else None
            if type(sample) is int or (raw_value and all(not isinstance(x, bool) and re.fullmatch(r"-?\d+", str(x).strip()) for x in raw_value)):
                try:
                    arr = [int(str(x).strip()) for x in raw_value if not isinstance(x, bool) and re.fullmatch(r"-?\d+", str(x).strip())]
                except (TypeError, ValueError) as exc:
                    raise BusinessToolRequestError(f"字段「{shown_label}」包含无效整数元素。") from exc
                if len(arr) != len(raw_value):
                    raise BusinessToolRequestError(f"字段「{shown_label}」要求全部为整数 ID：{raw_value!r}")
            else:
                arr = [str(x) for x in raw_value]
            return arr, str(old_raw or []), str(arr), None

        if not isinstance(raw_value, str):
            raise BusinessToolRequestError(f"字段「{shown_label}」需要字符串文本。")
        if field.max_length is not None and len(raw_value) > field.max_length:
            raise BusinessToolRequestError(f"字段「{shown_label}」长度超过上限 {field.max_length}。")
        old_disp = "（空）" if old_raw in (None, "") else str(old_raw)
        return raw_value, old_disp, raw_value if raw_value else "（空字符串）", None

    def encode_create_field(
        self,
        spec: CompiledCreateSpec,
        field: ConventionFieldSpec,
        raw_value,
        *,
        actor_id: str,
    ) -> tuple[object, str, dict | None]:
        api_val, _old, disp, binding = self.encode_field(
            spec, field, raw_value, {}, allow_status_field=True
        )
        return api_val, disp, binding

    def encode_create_default(
        self,
        spec: CompiledCreateSpec,
        field: ConventionFieldSpec,
        default_spec: CompiledFieldDefault,
        *,
        actor_id: str,
    ) -> tuple[object, str, dict | None]:
        if default_spec.kind == "actor":
            actor_int = int(actor_id) if str(actor_id).isdigit() else actor_id
            info = self.client.get("system/auth/get-permission-info") or {}
            nickname = str((info.get("user") or {}).get("nickname") or f"当前账号({actor_id})")
            return actor_int, f"{nickname}（ID={actor_id}）", None
        val = default_spec.value
        if field.enum_type:
            if field.enum_type.startswith("__java_enum__:"):
                enum_cls = field.enum_type.split(":", 1)[1]
                lbl = self._enum_labels_for(enum_cls).get(str(val), "")
                code = int(val) if field.kind == "integer" and re.fullmatch(r"-?\d+", str(val)) else val
                return code, lbl or str(val), (
                    {"kind": "java_enum", "enum_cls": enum_cls, "label": lbl, "code": code} if lbl else None
                )
            rows = self.client.get("system/dict-data/simple-list") or []
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r, dict) and r.get("dictType") == field.enum_type and str(r.get("value")) == str(val):
                        lbl = str(r.get("label") or val).strip()
                        code = int(val) if field.kind == "integer" and re.fullmatch(r"-?\d+", str(val)) else val
                        return code, lbl, {"kind": "dict", "dict_type": field.enum_type, "label": lbl, "code": code}
            lbl = self._dict_label_for(field.enum_type, val)
            return val, lbl if lbl != str(val) else str(val), None
        api_val, _old, disp, binding = self.encode_field(spec, field, val, {}, allow_status_field=True)
        return api_val, disp, binding

    def recheck_bindings(self, bindings: list[dict], *, actor_id: str) -> None:
        from web_operation_agent.business.errors import BusinessMutationRefusedError
        from .write_inputs import _area

        flat: list[dict] = []
        for b in bindings:
            if b.get("kind") == "batch":
                flat.extend(b.get("items") or ())
            else:
                flat.append(b)
        if not flat:
            return
        dict_rows = None
        for b in flat:
            kind = b.get("kind")
            if kind == "root":
                continue
            if kind == "java_enum":
                labels = self._enum_labels_for(b["enum_cls"])
                if labels and str(labels.get(str(b["code"])) or "") != str(b["label"]):
                    raise BusinessMutationRefusedError("enum changed after approval", reason="precondition_changed")
                continue
            if kind == "dict":
                if dict_rows is None:
                    dict_rows = self.client.get("system/dict-data/simple-list")
                    if not isinstance(dict_rows, list):
                        raise BusinessMutationRefusedError("dictionary unavailable on dispatch", reason="precondition_changed")
                matches = [
                    r for r in dict_rows
                    if isinstance(r, dict) and r.get("dictType") == b["dict_type"] and r.get("label") == b["label"]
                ]
                if len(matches) != 1 or str(matches[0].get("value")) != str(b["code"]):
                    raise BusinessMutationRefusedError("dictionary changed after approval", reason="precondition_changed")
            elif kind == "area":
                if _area(self.client, b["label"]) != b["code"]:
                    raise BusinessMutationRefusedError("region changed after approval", reason="precondition_changed")
            elif kind == "relation":
                qp = b.get("query_params") or {}
                raw_rows = self.client.get(b["list_path"], **qp) if qp else self.client.get(b["list_path"])
                items = raw_rows if isinstance(raw_rows, list) else (raw_rows or {}).get("list") if isinstance(raw_rows, dict) else None
                name_keys = b.get("name_keys") or [b["name_key"]]
                if not isinstance(items, list) or not any(
                    isinstance(r, dict) and r.get("id") == b["id"] and any(str(r.get(nk) or "").strip() == b["name"] for nk in name_keys)
                    for r in items
                ):
                    raise BusinessMutationRefusedError("related record changed after approval", reason="precondition_changed")

    def encode_preserved_field(self, spec, field: ConventionFieldSpec, raw_value):
        if field.date_kind == "epoch_ms" and isinstance(raw_value, str) and raw_value.strip():
            ms, _ = self._parse_epoch_ms(raw_value, shown_label=field.label or field.name)
            return ms
        return raw_value

    def matches_readback_value(self, spec, field: ConventionFieldSpec, readback_value, expected_api_value) -> bool:
        if field.date_kind == "epoch_ms" and type(expected_api_value) is int and isinstance(readback_value, str):
            return readback_value.strip() == self._format_epoch_ms(expected_api_value)
        return _values_equal(readback_value, expected_api_value, kind=field.kind)

    def dispatch_create(self, spec: CompiledCreateSpec, payload: dict):
        res = self.client.write_once("POST", f"{spec.entity}/create", body=payload, operation="create_record")
        if isinstance(res, dict):
            return res.get("id", res.get("data"))
        return res

    def read_created_record(self, spec: CompiledCreateSpec, created_id, payload: dict) -> dict | None:
        comp = {"accountSetId": payload["accountSetId"]} if payload.get("accountSetId") is not None else None
        if comp and created_id is not None:
            self._composite_hints[(spec.entity, str(created_id))] = dict(comp)
        return self.read_record(spec.entity, created_id, spec=spec, composite_id=comp)

    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None:
        self._require_crm_write_if_needed(spec.entity, record_id)
        self.client.write_once("PUT", f"{spec.entity}/update", body=payload, operation="update_record")

    def read_aggregate_record(self, spec: CompiledAggregateUpdateSpec, record_id, *, composite_id: dict | None = None) -> dict | None:
        return self.read_record(spec.entity, record_id, spec=spec, composite_id=composite_id)

    def dispatch_aggregate_update(self, spec: CompiledAggregateUpdateSpec, record_id, payload: dict, *, current_record: dict):
        self._require_crm_write_if_needed(spec.entity, record_id)
        body = dict(payload)
        if spec.readback_strategy == "change_record_id":
            if body.get("recordType") == 2:
                if body.get("changeReason") is None:
                    body["changeReason"] = 8
                # Guard stale historical salary-set effectTime when user didn't pass an explicit future effectTime
                today_ms = int(datetime.now(self._zone()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
                cur_eff = body.get("effectTime")
                cur_ms = cur_eff
                if isinstance(cur_eff, str) and cur_eff.strip():
                    cur_ms, _ = self._parse_epoch_ms(cur_eff, shown_label="生效时间")
                orig_eff = current_record.get("effectTime")
                if cur_ms is None or (cur_eff == orig_eff and type(cur_ms) is int and cur_ms < today_ms):
                    body["effectTime"] = today_ms
            return self.client.write_once(
                "PUT", f"{spec.entity}/update", body=body, operation="update_record", expect_result="id",
            )
        return self.client.write_once("PUT", f"{spec.entity}/update", body=body, operation="update_record")

    def read_aggregate_after_update(
        self,
        spec: CompiledAggregateUpdateSpec,
        record_id,
        dispatch_result,
        payload: dict,
        *,
        composite_id: dict | None = None,
    ) -> dict | None:
        if spec.readback_strategy == "change_record_id" and type(dispatch_result) is int:
            info = self.read_record(spec.entity, record_id, spec=spec)
            change_rec = self.client.get("hrm/salary/change-record/get", id=dispatch_result)
            if isinstance(info, dict) and isinstance(change_rec, dict):
                merged = dict(info)
                for key in ("salaryOptions", "probationSalaryOptions", "changeReason", "effectTime", "remark", "recordType"):
                    if key in change_rec and change_rec[key] is not None:
                        merged[key] = change_rec[key]
                return merged
            return info if isinstance(info, dict) else (change_rec if isinstance(change_rec, dict) else None)
        return self.read_record(spec.entity, record_id, spec=spec, composite_id=composite_id)

    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None:
        action_path = spec.action_path or f"{spec.entity}/update-status"
        id_param = spec.id_param or "id"
        status_param = spec.status_param or "status"
        if spec.second_action_path and status_code == spec.states[1][1]:
            action_path = spec.second_action_path
            if spec.param_mode == "query":
                self.client.write_once("PUT", action_path, body={}, params={id_param: record_id}, operation="toggle_record_status")
            else:
                self.client.write_once("PUT", action_path, body={id_param: record_id}, operation="toggle_record_status")
            return
        if spec.param_mode == "query":
            self.client.write_once(
                "PUT", action_path, body={}, params={id_param: record_id, status_param: status_code}, operation="toggle_record_status",
            )
            return
        body: dict[str, object] = {id_param: record_id, status_param: status_code}
        if spec.entity.startswith("fms/"):
            cur = self.read_record(spec.entity, record_id, spec=spec)
            if isinstance(cur, dict) and cur.get("accountSetId") is not None:
                body["accountSetId"] = cur["accountSetId"]
        self.client.write_once("PUT", action_path, body=body, operation="toggle_record_status")

    def resolve_relation_items(self, spec: CompiledRelationSpec, items: list[str]) -> tuple[list, list[str], list[dict]]:
        ids: list = []
        labels: list[str] = []
        bindings: list[dict] = []
        allow_root = spec.entity == "oa/file-node" and spec.items_param == "parentId"
        for tok in items:
            rid, rlabel, b = self._resolve_single_ref(spec.target_entity, tok, allow_root=allow_root)
            if rid in ids:
                raise BusinessToolRequestError(f"关联「{spec.relation_label}」包含重复项：{rlabel}")
            ids.append(rid)
            labels.append(rlabel)
            bindings.append(b)
        return ids, labels, bindings

    def read_relation_ids(self, spec: CompiledRelationSpec, target_id) -> list:
        qkey = spec.target_id_param or spec.target_param or "id"
        res = self.client.get(spec.read_path, **{qkey: target_id})
        if isinstance(res, list):
            return [x for x in res if x is not None]
        if isinstance(res, dict) and spec.read_field:
            val = res.get(spec.read_field)
            if spec.single_value:
                return [val] if val is not None else []
            return [x for x in (val or ()) if x is not None] if isinstance(val, (list, tuple, set)) else []
        return []

    def relation_labels_for_ids(self, spec: CompiledRelationSpec, ids: list) -> list[str]:
        if not ids:
            return []
        try:
            _lp, name_keys, rows, _qp = self._list_ref_rows(spec.target_entity)
        except Exception:
            return [str(x) for x in ids]
        by_id = {str(r["id"]): next((str(r[k]).strip() for k in name_keys if r.get(k)), str(r["id"])) for r in rows}
        return [by_id.get(str(x), str(x)) for x in ids]

    def dispatch_relation(self, spec: CompiledRelationSpec, target_id, item_ids: list) -> None:
        val = (item_ids[0] if item_ids else None) if spec.single_value else list(item_ids)
        payload = {spec.target_param: target_id, spec.items_param: val}
        kwargs = {"expect_result": "bool"} if spec.http_method == "POST" else {}
        if spec.param_mode == "query":
            self.client.write_once(spec.http_method, spec.action_path, body={}, params=payload, operation="assign_record_relations", **kwargs)
        else:
            self.client.write_once(spec.http_method, spec.action_path, body=payload, operation="assign_record_relations", **kwargs)

    def invalidate_reads(self, entities: frozenset[str]) -> None:
        self.goal_data.invalidate_reads(entities)

    def write_route_capability(self) -> WriteRouteCapability:
        return YUDAO_WRITE_ROUTE_CAPABILITY


def yudao_convention_tools(client, goal_data):
    port = YudaoConventionWritePort(client, goal_data)
    return compiled_write_tool_definitions(
        port,
        scope="yudao.write",
        entity_example="crm/receivable、hrm/employee、promotion/coupon-template、erp/purchase-order、fms/voucher、product/spu",
    )


__all__ = ["YUDAO_WRITE_ROUTE_CAPABILITY", "YudaoConventionWritePort", "yudao_convention_tools"]
