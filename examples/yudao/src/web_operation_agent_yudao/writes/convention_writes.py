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

from web_operation_agent.business.convention_writes import (
    ConventionFieldSpec,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
    _values_equal,
    convention_tool_definitions,
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
from ..rules.source import _CALL, _mask, _methods, _signature_params
from .write_definitions import RECORD_CONTRACTS

_CACHE_LOCK = RLock()
_COMPILED_SPECS_CACHE: OrderedDict[tuple[int, int, int], tuple] = OrderedDict()

_SECRET = DEFAULT_SECRET_FIELD_PATTERN
_VOLATILE = frozenset({"updateTime", "updater", "updaterName", "modifyTime", "version", "createTime"})
_NAME_CANDIDATES = (
    "name", "title", "no", "postName", "companyName", "nickname",
    "username", "code", "jobNumber", "signature", "label",
)
_QUERY_CANDIDATES = (
    "name", "title", "no", "postName", "companyName", "nickname",
    "username", "code", "jobNumber",
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
_DEDICATED_UPDATES = {
    contract.entity: f"{contract.prefix}_update"
    for contract in RECORD_CONTRACTS
    if "update" in contract.operations
}
_DEDICATED_STATUSES = {
    "promotion/coupon-template": "promotion_coupon_template_status",
    "promotion/seckill-activity": "promotion_seckill_activity_close",
}
_TWO_STATE_COMMON_BODY = frozenset({
    "system/user",
    "hrm/recruit/channel",
    "pay/app",
    "promotion/seckill-config",
    "ai/knowledge/document",
    "ai/knowledge/segment",
})
_TWO_STATE_COMMON_QUERY = frozenset({
    "mes/md/item",
    "mes/pro/route",
})
_CANDIDATE_SUFFIX = re.compile(r"^(.+?)\s*[（(](?:ID\s*=|编号\s*|#)\s*([0-9]+)\s*[）)]$")
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
    r"|(?:formData(?:\.value)?|data)\.(\w+)\s*=\s*(?:convertToInteger|yuanToFen)\()"
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


def _infer_reference(entity: str, prop: str, kind: str, desc: str) -> str:
    if kind == "integer":
        if prop in {"deptId", "keeperDeptId", "receiveDeptId"} or (entity == "system/dept" and prop == "parentId"):
            return "system/dept"
        if prop in {
            "leaderUserId", "ownerUserId", "managerUserId", "moderatorUserId",
            "keeperUserId", "handoverUserId", "handlerUserId",
        }:
            return "system/user"
        if prop in {"leaderEmployeeId", "employeeId", "interviewEmployeeId"}:
            return "hrm/employee"
        if prop == "areaId" and ("地区" in desc or "省" in desc or "区域" in desc):
            return "system/area"
    if kind == "id_array":
        if prop.endswith("UserIds"):
            return "system/user"
        if prop.endswith("DeptIds"):
            return "system/dept"
        if prop.endswith("EmployeeIds"):
            return "hrm/employee"
        if prop == "postIds":
            return "system/post"
    return ""


def _compile_java_update_semantics(
    source,
    ops: dict,
    refusals: dict,
) -> tuple[set[str], dict[str, frozenset[str]], dict[str, tuple[tuple[str, str], ...]]]:
    """Extract general update exclusions, immutable fields, and record-state refusal guards from masked Java ServiceImpl."""
    conditional_cleanup_entities: set[str] = set()
    immutable_fields: dict[str, frozenset[str]] = {}
    nonnull_refusals: dict[str, tuple[tuple[str, str], ...]] = {}
    if source is None or not getattr(source, "root", None):
        return conditional_cleanup_entities, immutable_fields, nonnull_refusals

    needed_ifaces = {
        info[0]
        for url, info in ops.items()
        if len(info) >= 5 and info[4] == "Put" and url.endswith("/update")
    }
    svc_data: dict[str, tuple[str, dict[str, list[tuple[int, int, int]]]]] = {}
    for path in source._java_files("ServiceImpl.java"):
        iface = path.stem[:-len("Impl")]
        if iface not in needed_ifaces:
            continue
        masked = _mask(source._read(source._relative(path)))
        sigs: dict[str, list[tuple[int, int, int]]] = {}
        for name, start, bs, be in _methods(masked):
            sigs.setdefault(name, []).append((start, bs, be))
        svc_data[iface] = (masked, sigs)

    nonnull_pat = re.compile(
        r"^(?:Objects\.nonNull\(\s*\w+\.get([A-Z]\w*)\(\)\s*\)|\w+\.get([A-Z]\w*)\(\)\s*!=\s*null)$"
    )
    set_null_pat = re.compile(r"\.set([A-Z]\w*)\(\s*null\s*\)")

    for url, info in ops.items():
        if len(info) < 5 or info[4] != "Put" or not url.endswith("/update"):
            continue
        entity = url[:-len("/update")]
        svc_iface, method_name = info[0], info[1]

        guards: list[tuple[str, str]] = []
        for ref in refusals.get((svc_iface, method_name), ()):
            cond = (getattr(ref, "condition", "") or "").strip()
            m_nn = nonnull_pat.fullmatch(cond)
            if m_nn:
                g_prop = m_nn.group(1) or m_nn.group(2)
                prop_camel = g_prop[0].lower() + g_prop[1:]
                guards.append((prop_camel, getattr(ref, "message", "") or cond))
        if guards:
            nonnull_refusals[entity] = tuple(guards)

        if svc_iface not in svc_data:
            continue
        masked, sigs = svc_data[svc_iface]
        spans = sigs.get(method_name)
        if not spans:
            conditional_cleanup_entities.add(entity)
            continue

        # Walk method_name and same-class helper methods it calls
        visited: set[tuple[str, int]] = set()
        work: list[tuple[str, int, int]] = [(method_name, i, 0) for i in range(len(spans))]
        bodies: list[tuple[str, int, int, int]] = []
        while work:
            mname, idx, call_depth = work.pop()
            if (mname, idx) in visited:
                continue
            visited.add((mname, idx))
            s, bs, be = sigs[mname][idx]
            bodies.append((mname, idx, bs, be))
            for call_m in _CALL.finditer(masked[bs:be]):
                callee = call_m.group(1)
                for ci in range(len(sigs.get(callee, ()))):
                    if (callee, ci) not in visited:
                        work.append((callee, ci, call_depth + 1))

        # Any custom own-mapper update* call (other than updateById) across entry+helpers -> exclude
        custom_mapper_update = False
        for _mname, _idx, bs, be in bodies:
            own_mapper_calls = re.findall(r"\b\w+Mapper\.(update\w+)\s*\(", masked[bs:be])
            if any(mc != "updateById" for mc in own_mapper_calls):
                custom_mapper_update = True
                break
        if custom_mapper_update:
            conditional_cleanup_entities.add(entity)
            continue

        # Inspect top-level entry parameter for unconditional vs conditional setXxx(null)
        s0, bs0, be0 = spans[0]
        params = _signature_params(masked, s0, bs0)
        if not params:
            continue
        req_var = params[0]

        uncond: set[str] = set()
        cond_null = False
        brace_depth = 0
        pending_branch = False
        req_var_pat = re.compile(r"\b" + re.escape(req_var) + r"\s*\.set[A-Z]\w*\(")

        for raw_ln in masked[bs0:be0].splitlines():
            ln = raw_ln.strip()
            if not ln:
                continue
            is_branch_head = bool(re.match(r"^(?:if|else)\b", ln))
            ends_stmt = ln.endswith((";", "{", "}"))
            if req_var_pat.search(ln):
                null_props = [p[0].lower() + p[1:] for p in set_null_pat.findall(ln)]
                if "?" in ln and "null" in ln:
                    cond_null = True
                elif null_props:
                    if brace_depth == 0 and not pending_branch and not is_branch_head and ln.startswith(req_var + "."):
                        uncond.update(p for p in null_props if p != "id")
                    else:
                        cond_null = True
            brace_depth += ln.count("{") - ln.count("}")
            if is_branch_head and not ln.endswith("{") and ";" not in ln:
                pending_branch = True
            elif ends_stmt:
                pending_branch = False

        # Any setXxx(null) inside same-class helpers counts as conditional/helper mutation -> exclude
        for mname, _idx, bs, be in bodies[1:]:
            if mname == method_name and _idx == 0:
                continue
            if set_null_pat.search(masked[bs:be]):
                cond_null = True
                break

        if cond_null:
            conditional_cleanup_entities.add(entity)
        elif uncond:
            immutable_fields[entity] = frozenset(uncond)

    return conditional_cleanup_entities, immutable_fields, nonnull_refusals


class YudaoConventionWritePort:
    """Adapter runtime and lazy compiler for ``update_record`` and ``toggle_record_status``."""

    def __init__(self, client, goal_data):
        self.client = client
        self.goal_data = goal_data
        self._lock = RLock()
        self._updates: dict[str, ConventionUpdateSpec] | None = None
        self._statuses: dict[str, TwoStateStatusSpec] | None = None
        self._immutable_fields: dict[str, frozenset[str]] = {}
        self._nonnull_refusals: dict[str, tuple[tuple[str, str], ...]] = {}

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
        if kind == "update":
            return _DEDICATED_UPDATES.get(clean)
        if kind == "status":
            return _DEDICATED_STATUSES.get(clean)
        return None

    def _compile_all(self) -> None:
        docs = self._docs()
        paths = docs.get("paths") or {}
        schemas = (docs.get("components") or {}).get("schemas") or {}
        source = getattr(self.goal_data, "_source", None)
        ui = getattr(self.goal_data, "_ui", None)
        cache_key = (id(docs), id(source), id(ui))
        with _CACHE_LOCK:
            cached = _COMPILED_SPECS_CACHE.get(cache_key)
            if cached is not None:
                _COMPILED_SPECS_CACHE.move_to_end(cache_key)
        if cached is not None:
            updates, statuses, immutable_fields, nonnull_refusals, _, _, _ = cached
            self._immutable_fields = immutable_fields
            self._nonnull_refusals = nonnull_refusals
            self._updates = updates
            self._statuses = statuses
            return
        ui_labels, ui_dicts, ui_scales = _compile_ui_metadata(source, ui)

        updates: dict[str, ConventionUpdateSpec] = {}
        statuses: dict[str, TwoStateStatusSpec] = {}

        # 1. Two-state reversible status specs (excluding entities owned by dedicated reviewed status ops)
        common_synonyms = (
            ("启用", "开启"),
            ("正常", "开启"),
            ("停用", "关闭"),
            ("禁用", "关闭"),
        )
        for p, item in sorted(paths.items()):
            if not p.startswith("/admin-api/") or not p.endswith("/update-status") or not isinstance(item, dict):
                continue
            put = item.get("put")
            if not isinstance(put, dict):
                continue
            entity = p[len("/admin-api/"):-len("/update-status")]
            if entity in _DEDICATED_STATUSES:
                continue
            try:
                resp_ref = next(iter(put["responses"]["200"]["content"].values()))["schema"].get("$ref", "")
            except (KeyError, StopIteration, TypeError, AttributeError):
                resp_ref = ""
            if not resp_ref.endswith("/CommonResultBoolean"):
                continue
            tag = ((put.get("tags") or [""])[0]).replace("管理后台 - ", "").strip() or entity
            if entity == "product/spu" and f"/admin-api/{entity}/get-detail" in paths:
                statuses[entity] = TwoStateStatusSpec(
                    entity=entity,
                    label=tag,
                    states=(("上架", 1), ("下架", 0)),
                    name_fields=("name",),
                    volatile_fields=_VOLATILE,
                    synonyms=(("开启", "上架"), ("启用", "上架"), ("关闭", "下架"), ("停用", "下架")),
                )
            elif (entity in _TWO_STATE_COMMON_BODY or entity in _TWO_STATE_COMMON_QUERY) and f"/admin-api/{entity}/get" in paths:
                get_op = (paths.get(f"/admin-api/{entity}/get") or {}).get("get") or {}
                resp_props = self._resp_props(get_op, schemas)
                name_fields = tuple(k for k in _NAME_CANDIDATES if k in resp_props) or ("name",)
                statuses[entity] = TwoStateStatusSpec(
                    entity=entity,
                    label=tag,
                    states=(("开启", 0), ("关闭", 1)),
                    name_fields=name_fields,
                    volatile_fields=_VOLATILE,
                    synonyms=common_synonyms,
                )

        # 2. Source rules & Java update semantics (fail closed on unexpected analysis error)
        unsafe_updates: set[str] = set()
        immutable_fields: dict[str, frozenset[str]] = {}
        nonnull_refusals: dict[str, tuple[tuple[str, str], ...]] = {}
        if source is not None and hasattr(source, "_server_rules"):
            try:
                ops, refusals, cascades = source._server_rules()
                for url, info in ops.items():
                    if len(info) >= 5 and info[4] == "Put" and url.endswith("/update"):
                        casc = cascades.get((info[0], info[1]))
                        if not casc:
                            continue
                        if casc.get("untraceable") or any(
                            str(call[0]).endswith("Service") for call in casc.get("calls", ())
                        ):
                            unsafe_updates.add(url[:-len("/update")])
                cond_cleanup, immutable_fields, nonnull_refusals = _compile_java_update_semantics(
                    source, ops, refusals,
                )
                unsafe_updates.update(cond_cleanup)
            except Exception:
                self._immutable_fields = {}
                self._nonnull_refusals = {}
                self._updates = {}
                self._statuses = statuses
                return
        self._immutable_fields = immutable_fields
        self._nonnull_refusals = nonnull_refusals

        # 3. Flat 1:1 symmetric update specs
        for p, item in sorted(paths.items()):
            if not p.startswith("/admin-api/") or not p.endswith("/update") or not isinstance(item, dict):
                continue
            put = item.get("put")
            if not isinstance(put, dict) or "requestBody" not in put:
                continue
            entity = p[len("/admin-api/"):-len("/update")]
            if entity in _DEDICATED_UPDATES or entity in unsafe_updates:
                continue
            get_op = (paths.get(f"/admin-api/{entity}/get") or {}).get("get")
            if not isinstance(get_op, dict):
                continue
            try:
                put_resp_ref = next(iter(put["responses"]["200"]["content"].values()))["schema"].get("$ref", "")
                req_raw = next(iter(put["requestBody"]["content"].values()))["schema"]
            except (KeyError, StopIteration, TypeError, AttributeError):
                continue
            if not put_resp_ref.endswith("/CommonResultBoolean"):
                continue
            req_schema = self._deref(req_raw, schemas)
            req_props = req_schema.get("properties") or {}
            resp_props = self._resp_props(get_op, schemas)
            req_required = set(req_schema.get("required") or ())
            if "id" not in req_props or "id" not in resp_props:
                continue

            ent_labels = ui_labels.get(entity, {})
            ent_dicts = ui_dicts.get(entity, {})
            ent_scales = ui_scales.get(entity, {})

            field_specs: list[ConventionFieldSpec] = []
            disqualified = False
            for fname, raw_prop in req_props.items():
                if fname == "id":
                    continue
                prop = self._deref(raw_prop, schemas)
                desc = str(prop.get("description") or raw_prop.get("description") or "")
                lbl = ent_labels.get(fname) or _clean_desc_label(desc)
                if _SECRET.search(fname) or _SECRET.search(desc) or _SECRET.search(lbl):
                    if fname in req_required:
                        disqualified = True
                        break
                    continue
                ptype = prop.get("type")
                pformat = prop.get("format") or ""
                if "$ref" in raw_prop or ptype == "object":
                    disqualified = True
                    break
                if fname not in resp_props:
                    disqualified = True
                    break

                date_kind = ""
                if ptype == "array":
                    items_raw = prop.get("items") or {}
                    items = self._deref(items_raw, schemas)
                    if "$ref" in items_raw or items.get("type") not in {"integer", "string", "number"}:
                        disqualified = True
                        break
                    kind = "id_array"
                elif ptype == "string" and pformat == "date-time":
                    kind = "integer"
                    date_kind = "epoch_ms"
                elif ptype == "string" and pformat == "date":
                    kind = "string"
                    date_kind = "date"
                elif ptype in {"string", "integer", "number", "boolean"}:
                    kind = ptype
                else:
                    disqualified = True
                    break

                unit_scale, display_unit = ent_scales.get(fname, (1, ""))
                if unit_scale == 1 and kind == "integer" and re.search(r"单位\s*[：:（(]?\s*分\b", desc):
                    unit_scale, display_unit = 100, "元"

                enum_type = ent_dicts.get(fname, "")
                if (not enum_type and fname == "status" and kind == "integer"
                        and entity not in statuses
                        and ("CommonStatusEnum" in desc or "开启状态" in desc or "启用状态" in desc)):
                    enum_type = "common_status"

                ref_entity = _infer_reference(entity, fname, kind, desc)
                max_len = prop.get("maxLength") if type(prop.get("maxLength")) is int else None
                min_val = prop.get("minimum") if type(prop.get("minimum")) in (int, float) else None
                max_val = prop.get("maximum") if type(prop.get("maximum")) in (int, float) else None
                min_len = prop.get("minLength") if type(prop.get("minLength")) is int and prop.get("minLength") > 0 else None
                try:
                    field_specs.append(ConventionFieldSpec(
                        name=fname,
                        kind=kind,
                        label=lbl,
                        required=fname in req_required,
                        max_length=max_len,
                        minimum=min_val,
                        maximum=max_val,
                        min_length=min_len,
                        enum_type=enum_type,
                        reference_entity=ref_entity,
                        unit_scale=unit_scale,
                        display_unit=display_unit,
                        date_kind=date_kind,
                    ))
                except ValueError:
                    if fname in req_required:
                        disqualified = True
                        break
                    continue

            if disqualified or not field_specs:
                continue
            tag = ((put.get("tags") or [""])[0]).replace("管理后台 - ", "").strip() or entity
            name_fields = tuple(k for k in _NAME_CANDIDATES if k in resp_props) or ("name",)
            updates[entity] = ConventionUpdateSpec(
                entity=entity,
                label=tag,
                fields=tuple(field_specs),
                id_field="id",
                name_fields=name_fields,
                volatile_fields=_VOLATILE,
            )

        self._updates = updates
        self._statuses = statuses
        with _CACHE_LOCK:
            _COMPILED_SPECS_CACHE[cache_key] = (
                updates,
                statuses,
                immutable_fields,
                nonnull_refusals,
                docs,
                source,
                ui,
            )
            _COMPILED_SPECS_CACHE.move_to_end(cache_key)
            while len(_COMPILED_SPECS_CACHE) > 8:
                _COMPILED_SPECS_CACHE.popitem(last=False)

    def _resp_props(self, get_op: dict, schemas: dict) -> dict:
        try:
            outer_raw = next(iter(get_op["responses"]["200"]["content"].values()))["schema"]
        except (KeyError, StopIteration, TypeError, AttributeError):
            return {}
        outer = self._deref(outer_raw, schemas)
        data_raw = (outer.get("properties") or {}).get("data") or {}
        data_schema = self._deref(data_raw, schemas)
        return data_schema.get("properties") or {}

    def update_specs(self) -> dict[str, ConventionUpdateSpec]:
        with self._lock:
            if self._updates is None:
                self._compile_all()
            return self._updates or {}

    def status_specs(self) -> dict[str, TwoStateStatusSpec]:
        with self._lock:
            if self._statuses is None:
                self._compile_all()
            return self._statuses or {}

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

    def read_record(self, entity: str, record_id) -> dict | None:
        path = "product/spu/get-detail" if entity == "product/spu" else f"{entity}/get"
        try:
            data = self.client.get(path, id=record_id)
        except YudaoReadRefused as exc:
            if exc.permission_denied or exc.code == 401:
                raise BusinessToolPermissionError("当前账号无权读取目标记录详情。") from exc
            if exc.read_failed:
                raise BusinessToolUnavailableError("目标记录详情接口当前不可用。") from exc
            raise BusinessToolRequestError(f"目标记录详情不可读（code={exc.code}）。") from exc
        return data if isinstance(data, dict) else None

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
        # 1. Try published /page or /list endpoint directly for fast exact resolution
        docs = self._docs()
        paths = docs.get("paths") or {}
        page_path = f"/admin-api/{entity}/page"
        list_path = f"/admin-api/{entity}/list"
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
        elif list_path in paths:
            res = self.client.get(f"{entity}/list")
            if isinstance(res, list):
                rows = [r for r in res if isinstance(r, dict)]

        folded = clean_name.casefold()
        exact_rows = []
        for row in rows:
            rid = row.get(spec.id_field)
            if rid is None:
                continue
            if hint_id is not None and str(rid) != hint_id:
                continue
            row_names = [
                str(row.get(k)).strip()
                for k in (*spec.name_fields, *_NAME_CANDIDATES)
                if isinstance(row.get(k), str) and str(row.get(k)).strip()
            ]
            if any(n.casefold() == folded for n in row_names):
                exact_rows.append(row)

        if len(exact_rows) == 1:
            matched_id = exact_rows[0][spec.id_field]
        elif len(exact_rows) > 1:
            choices = tuple(
                f"{exact_rows[i].get(spec.name_fields[0]) or clean_name}（ID={exact_rows[i][spec.id_field]}）"
                for i in range(min(6, len(exact_rows)))
            )
            raise BusinessToolReferenceError(
                f"找到多条名为「{clean_name}」的{spec.label}，请向用户确认具体哪一条",
                entity=entity,
                choices=choices,
            )
        else:
            # Fallback to goal_data.find when entity is indexed there
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
                near = tuple(dict.fromkeys(
                    *(r.display for r in found),
                    *(
                        f"{row.get(spec.name_fields[0]) or row.get('name') or row.get('title') or row.get('no')}（ID={row.get(spec.id_field)}）"
                        for row in rows[:6]
                        if row.get(spec.id_field) is not None
                    ),
                ))[:6]
                raise BusinessToolReferenceError(
                    f"未找到唯一匹配的{spec.label}「{target}」，请核对确切业务名称或单号",
                    entity=entity,
                    choices=near,
                )

        record = self.read_record(entity, matched_id)
        if not isinstance(record, dict) or str(record.get(spec.id_field)) != str(matched_id):
            raise BusinessToolRequestError(f"目标{spec.label}详情当前不可读。")
        self._require_crm_write_if_needed(entity, record[spec.id_field])
        if isinstance(spec, ConventionUpdateSpec):
            self.check_record_updatable(entity, record)
        return record

    def check_record_updatable(self, entity: str, record: dict) -> None:
        self.update_specs()
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

    def _resolve_dict_choice(self, dict_type: str, raw_value, kind: str) -> tuple[int | str, str, dict]:
        s_val = str(raw_value).strip()
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
            code: int | str = int(code_raw)
        else:
            code = code_raw
        label = str(exact[0]["label"]).strip()
        return code, label, {"kind": "dict", "dict_type": dict_type, "label": label, "code": code}

    def _dict_label_for(self, dict_type: str, code) -> str:
        if code is None:
            return "（空）"
        rows = self.client.get("system/dict-data/simple-list") or []
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("dictType") == dict_type and str(r.get("value")) == str(code):
                    return str(r.get("label"))
        return str(code)

    def _resolve_single_ref(self, ref_entity: str, token: str, *, allow_root: bool = False) -> tuple[int, str, dict]:
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

        endpoint_map = {
            "system/dept": ("system/dept/simple-list", "name"),
            "system/user": ("system/user/simple-list", "nickname"),
            "hrm/employee": ("hrm/employee/simple-list", "name"),
            "system/post": ("system/post/simple-list", "name"),
        }
        if ref_entity not in endpoint_map:
            raise BusinessToolRequestError(f"不支持解析关联类型：{ref_entity}")
        list_path, name_key = endpoint_map[ref_entity]
        raw_rows = self.client.get(list_path)
        items = raw_rows if isinstance(raw_rows, list) else (raw_rows or {}).get("list") if isinstance(raw_rows, dict) else None
        if not isinstance(items, list):
            raise BusinessToolRequestError(f"关联列表 {list_path} 当前不可读。")
        rows = [r for r in items if isinstance(r, dict) and type(r.get("id")) is int]

        if ref_entity == "system/dept" and "/" in name:
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
                if str(r.get(name_key) or "").strip() == name
                and (hint_id is None or str(r["id"]) == hint_id)
            ]
        if len(matches) != 1:
            sample = "、".join(str(r.get(name_key)) for r in rows[:15] if r.get(name_key))
            raise BusinessToolRequestError(
                f"在 {ref_entity} 中未找到唯一匹配的「{token}」（可选示例：{sample}）。"
            )
        chosen = matches[0]
        label = str(chosen.get(name_key) or name).strip()
        return chosen["id"], label, {
            "kind": "relation",
            "list_path": list_path,
            "name_key": name_key,
            "id": chosen["id"],
            "name": str(chosen.get(name_key) or "").strip(),
        }

    def encode_field(
        self,
        spec: ConventionUpdateSpec,
        field: ConventionFieldSpec,
        raw_value,
        current_record: dict,
    ) -> tuple[object, str, str, dict | None]:
        if field.name == "status" and spec.entity in self.status_specs():
            raise BusinessToolRequestError(
                f"实体「{spec.entity}」的启用/上下架状态开关独立于详情修改，请改用 toggle_record_status 操作。"
            )
        shown_label = field.label or field.name
        if field.name in self._immutable_fields.get(spec.entity, ()):
            raise BusinessToolRequestError(
                f"字段「{shown_label}（{field.name}）」在 {spec.entity}/update 接口中不可修改"
                "（创建后锁定或需走专用转移/变更入口），请改走对应专用操作或页面。"
            )
        old_raw = current_record.get(field.name)

        if raw_value is None:
            if field.required:
                raise BusinessToolRequestError(f"字段「{shown_label}」为必填项，不能清空为 null。")
            return None, "（空）" if old_raw in (None, "") else str(old_raw), "（清空）", None

        # 1. Reference fields (single FK or ID array)
        if field.reference_entity:
            if field.kind == "id_array":
                if not isinstance(raw_value, list):
                    raise BusinessToolRequestError(f"字段「{shown_label}」须传名称数组。")
                ids: list[int] = []
                labels: list[str] = []
                sub_bindings: list[dict] = []
                for item in raw_value:
                    rid, rlabel, b = self._resolve_single_ref(field.reference_entity, str(item))
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
                allow_root=(spec.entity == "system/dept" and field.name == "parentId"),
            )
            old_disp = "（空）" if old_raw is None else str(old_raw)
            return rid, old_disp, f"{rlabel}（ID={rid}）", b

        # 2. Dictionary-bound enum fields
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

        # 4. Epoch-ms timestamps (LocalDateTime in Yudao Jackson PUT deserialization always expects epoch ms)
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
                raw_rows = self.client.get(b["list_path"])
                items = raw_rows if isinstance(raw_rows, list) else (raw_rows or {}).get("list") if isinstance(raw_rows, dict) else None
                if not isinstance(items, list) or not any(
                    isinstance(r, dict) and r.get("id") == b["id"] and str(r.get(b["name_key"]) or "").strip() == b["name"]
                    for r in items
                ):
                    raise BusinessMutationRefusedError("related record changed after approval", reason="precondition_changed")

    def encode_preserved_field(self, spec: ConventionUpdateSpec, field: ConventionFieldSpec, raw_value):
        if field.date_kind == "epoch_ms" and isinstance(raw_value, str) and raw_value.strip():
            ms, _ = self._parse_epoch_ms(raw_value, shown_label=field.label or field.name)
            return ms
        return raw_value

    def matches_readback_value(self, spec: ConventionUpdateSpec, field: ConventionFieldSpec, readback_value, expected_api_value) -> bool:
        if field.date_kind == "epoch_ms" and type(expected_api_value) is int and isinstance(readback_value, str):
            return readback_value.strip() == self._format_epoch_ms(expected_api_value)
        return _values_equal(readback_value, expected_api_value, kind=field.kind)

    def dispatch_update(self, spec: ConventionUpdateSpec, record_id, payload: dict) -> None:
        self._require_crm_write_if_needed(spec.entity, record_id)
        self.client.write_once("PUT", f"{spec.entity}/update", body=payload, operation="update_record")

    def dispatch_status(self, spec: TwoStateStatusSpec, record_id, status_code) -> None:
        if spec.entity in _TWO_STATE_COMMON_QUERY:
            self.client.write_once(
                "PUT",
                f"{spec.entity}/update-status",
                body={},
                params={"id": record_id, "status": status_code},
                operation="toggle_record_status",
            )
        else:
            self.client.write_once(
                "PUT",
                f"{spec.entity}/update-status",
                body={"id": record_id, "status": status_code},
                operation="toggle_record_status",
            )

    def invalidate_reads(self, entities: frozenset[str]) -> None:
        self.goal_data.invalidate_reads(entities)


def yudao_convention_tools(client, goal_data):
    port = YudaoConventionWritePort(client, goal_data)
    return convention_tool_definitions(
        port,
        scope="yudao.write",
        entity_example="promotion/coupon-template、hrm/employee、oa/schedule、product/spu",
    )


__all__ = ["YudaoConventionWritePort", "yudao_convention_tools"]
