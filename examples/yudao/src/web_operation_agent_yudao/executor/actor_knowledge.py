"""Actor-scoped menu and OpenAPI read-only knowledge for Yudao."""
from __future__ import annotations

from datetime import datetime
import inspect
import json
import math
import re
import threading
from zoneinfo import ZoneInfo

from web_operation_agent.execution.read_summary import SummaryRefused, parse_summary_spec
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS

from ..reads.api_content import REDACTED, composed, fenced, redacted
from ..reads.turn_tables import TurnReadError
from .page_prompts import _ALL_ROWS, _API_CHARS, _GROUPS

_TIME_KEY = re.compile(r"(?:Time|Date)$", re.I)
#: Numbers that name or classify a row; a sum or a highest value of them means nothing.
_NOT_A_QUANTITY = re.compile(r"(?:^|[a-z])(?:Id|No|Status|State|Type|Sort|Level|Sex|Code|Enable|Flag)$"
                              r"|^(?:id|no|status|state|type|sort|level|sex|code|enable|flag)$")
#: Identities a read returns without their names, named through another read the same
#: actor may make: {read path: {column: (lookup path, name field, column title)}}.
_RELATION_NAMES = {"system/dept/list": {"leaderUserId": ("system/user/simple-list", "nickname", "负责人")}}
_RECORD_KEY = re.compile(r"(?i)(^|_)(id|no|name|title|subject)$|name$")
_SENSITIVE_COL_KEY = re.compile(r"(?:password|secret|token|apikey|privatekey)", re.I)

#: Narrowed 16 pure default-config / financial-report-snapshot writing GETs allowed
#: ONLY on active user Q&A `call_api` (2026-10-29 & 2026-11-20 owner decisions).
#: Business side-effect GETs (mail, pay notify, visit counters, Redis) and binary
#: exports remain refused; background warm/previews/probes refuse all.
USER_READ_WRITING_GET_ALLOWLIST: frozenset[str] = frozenset({
    "/admin-api/fms/closing/period/overview",
    "/admin-api/fms/closing/scheme/list",
    "/admin-api/fms/home/get",
    "/admin-api/fms/home/metric-detail",
    "/admin-api/fms/report/balance-sheet/check",
    "/admin-api/fms/report/balance-sheet/get",
    "/admin-api/fms/report/cash-flow-statement/adjustment/list",
    "/admin-api/fms/report/cash-flow-statement/check",
    "/admin-api/fms/report/cash-flow-statement/get",
    "/admin-api/fms/report/income-statement/check",
    "/admin-api/fms/report/income-statement/get",
    "/admin-api/mes/wm/material-stock/page",
    "/admin-api/pay/wallet-transaction/page",
    "/admin-api/pay/wallet/get",
    "/admin-api/pms/kb/group/list",
    "/admin-api/pms/pm/project-group/list",
})


def _grams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} | set(text)


def _score(query: str, *fields) -> float:
    terms = [t for t in re.split(r"[\s,，、/]+", query.lower()) if t]
    score = 0.0
    for weight, field in fields:
        if not field:
            continue
        low = field.lower()
        for term in terms:
            if term in low:
                score += 6 * weight * (1 + len(term) / 8)
        want, have = _grams("".join(terms)), _grams(low)
        if want:
            score += weight * 3 * len(want & have) / len(want)
    return score


def _slim(value, deep: bool = False):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            if isinstance(item, (dict, list)):
                if deep:
                    out[key] = _slim(item) if isinstance(item, dict) else [_slim(x) for x in item[:20]]
                elif isinstance(item, list) and item and not isinstance(item[0], (dict, list)):
                    out[key] = item[:10]
                continue
            out[key] = item[:80] if isinstance(item, str) else item
        return out
    return value


def _amount(value) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


class QueryParametersError(ValueError):
    """Safe structural correction, generated only from installed parameter names."""


_ENTITY_READ_SUFFIXES = (
    "/list-all-simple",
    "/simple-all-list",
    "/directory-list",
    "/export-excel",
    "/simple-list",
    "/get-detail",
    "/page",
    "/list",
    "/get",
)
_REF_CELL_KEYS = frozenset({"id", "name", "title", "no", "code", "period", "stage", "installment", "nickname", "username"})
_NON_METRIC_COL_RE = re.compile(
    r"^(?:id|period|stage|sort|status|auditStatus|type|creator|updater)$|Id$|Time$|Date$",
    re.I,
)


def _entity_slug_for_path(path: str) -> str:
    rel = str(path or "").removeprefix("/admin-api/").strip("/")
    for suffix in _ENTITY_READ_SUFFIXES:
        if rel.endswith(suffix):
            rel = rel[: -len(suffix)]
            break
    return rel.strip("/")


def _inline_small_table_ties_note(rows: list, col_labels: dict[str, str] | None = None) -> str:
    """Non-imperative extremum tie statistics for inline complete small tables (2..30 rows)."""
    dict_rows = [r for r in rows or () if isinstance(r, dict)]
    if len(dict_rows) < 2 or len(dict_rows) > 30:
        return ""
    labels = col_labels or {}
    keys = list(dict.fromkeys(k for r in dict_rows[:10] for k in r))
    parts: list[str] = []
    for key in keys:
        if _NON_METRIC_COL_RE.search(key) or _SENSITIVE_COL_KEY.search(key):
            continue
        vals = []
        ok = True
        for row in dict_rows:
            v = row.get(key)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                ok = False
                break
            vals.append(v)
        if not ok or len(vals) < 2:
            continue
        distinct = set(vals)
        if len(distinct) < 2:
            continue
        hi = max(vals)
        lo = min(vals)
        hi_cnt = sum(1 for v in vals if v == hi)
        lo_cnt = sum(1 for v in vals if v == lo)
        if hi_cnt < 2 and lo_cnt < 2:
            continue
        lbl = labels.get(key)
        col_disp = f"{key}（{lbl}）" if lbl and lbl != key else key
        sub = []
        if hi_cnt >= 2:
            sub.append(f"最大值 {hi} 共 {hi_cnt} 行同值")
        if lo_cnt >= 2 and lo != hi:
            sub.append(f"最小值 {lo} 共 {lo_cnt} 行同值")
        if sub:
            parts.append(f"{col_disp}：" + "、".join(sub))
        if len(parts) >= 3:
            break
    if not parts:
        return ""
    return f"内联全表同值统计（共 {len(dict_rows)} 行）：" + "；".join(parts) + "。"


def _cell_values(rows, column_names=None, cap=400, *, entity_slug: str = ""):
    """(binding, value) candidates of rows the actor read this turn: every scalar cell
    bound to its column's label (and raw field key when different), plus the record's
    own name/id cells unbound for selection fields (and ``entity_slug`` when known)."""
    bound, raw_keys, records, ref_cands = [], [], [], []
    names = column_names or {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        for key, cell in row.items():
            if isinstance(cell, (dict, list, tuple)) or cell is None or isinstance(cell, bool):
                continue
            raw_key = str(key)[:80]
            label = str(names.get(key, key))[:80]
            if len(bound) < cap:
                bound.append((label, cell))
                if raw_key != label:
                    raw_keys.append((raw_key, cell))
            if (_RECORD_KEY.search(str(key)) or _RECORD_KEY.search(label)) and len(records) < 4000:
                records.append(("", cell))
            if entity_slug and (raw_key in _REF_CELL_KEYS or _RECORD_KEY.search(raw_key)) and len(ref_cands) < 4000:
                ref_cands.append((entity_slug, cell))
            if len(bound) >= cap and len(records) >= 4000 and (not entity_slug or len(ref_cands) >= 4000):
                return bound + raw_keys + records + ref_cands, bound
    return bound + raw_keys + records + ref_cands, bound


class ActorKnowledge:
    """The signed-in actor's menu pages and the application's read-only API, searchable."""

    def __init__(self, client, api_docs, writing_gets=frozenset(), time_zone: str = "Asia/Shanghai",
                 codes=None, money_review=None, filter_semantics=None, columns=None, query_choices=None,
                 derived_columns=None):
        self.client, self._docs, self.writing_gets = client, api_docs, frozenset(writing_gets)
        #: `codes(list_path)`: the names of a list's coded values by field, when the
        #: application names them ({"bizType": {"50": "销售出库"}}).
        self._codes = codes
        self._columns = columns
        self._derived_columns = derived_columns
        self._query_choices = query_choices
        self.money_review = money_review
        #: What each published query parameter's condition means, compiled from the
        #: deployed release's source. Direct identity bindings may also be reapplied
        #: in summaries; comparison labels alone are not executable bindings.
        self.filter_semantics = filter_semantics
        self._zone = ZoneInfo(time_zone)
        self._menu = self._ops = self._by_path = self._schemas = self._s3_index = self._loaded_docs = None
        #: The host may load this ahead of the first turn, or concurrent read threads may race _load().
        self._loading = threading.RLock()
        self.user_name = ""
        #: The session parameters of the application this actor is signed in to; the turn
        #: sets its own, so a read is redacted of them like a page is.
        self.handoffs = BASELINE_SESSION_HANDOFFS
        #: What the last `call_api` read, for whoever is watching the turn: how many rows
        #: (one for a single record) and which of the application's lists; None when the
        #: read was refused or named no interface.
        self.last_read: dict | None = None
        self.last_keys: tuple[str, ...] = ()
        self.turn_targets: dict[str, set[str]] = {}
        self.probed_s3_edges: set[str] = set()
        self.last_writing_get_allowed: bool = False

    def _load(self):
        with self._loading:
            if self._ops is None:
                self._learn()

    def _learn(self):
        docs = self._docs() if callable(self._docs) else self._docs
        self._loaded_docs = docs if isinstance(docs, dict) else None
        self._schemas = (docs.get("components") or {}).get("schemas") or {}
        ops = []
        for path, item in (docs.get("paths") or {}).items():
            op = item.get("get") if isinstance(item, dict) else None
            if not path.startswith("/admin-api/") or not isinstance(op, dict):
                continue
            if any(mark in path for mark in ("/export", "/import", "/download")):
                continue
            ops.append({"path": path, "summary": op.get("summary") or "",
                        "tag": (op.get("tags") or [""])[0].replace("管理后台 - ", ""), "op": op})
        self._by_path = {op["path"]: op for op in ops}
        info = self.client.get("system/auth/get-permission-info") or {}
        self.user_name = str((info.get("user") or {}).get("nickname") or "")
        self._menu = []
        self._walk(info.get("menus") or [], [], "")
        self._ops = ops

    def _s3_bridge(self):
        self._load()
        with self._loading:
            if self._s3_index is None:
                from ..reads.evidence_sheet import S3ErpStockRecordIndex

                def safe_codes(rel: str) -> dict:
                    if self._codes is None:
                        return {}
                    try:
                        return self._codes(rel) or {}
                    except (DataRefused, LookupError, ValueError):
                        return {}

                self._s3_index = S3ErpStockRecordIndex.build(
                    self._by_path, self.writing_gets, codes_fn=safe_codes
                )
            return self._s3_index

    def fms_dual_month_context(self, today: str) -> tuple[str, tuple[str, ...]]:
        """Read non-writing FMS account-sets & closing period for dual-month alignment."""
        self._load()
        acct_path = "/admin-api/fms/config/account-set/list"
        curr_path = "/admin-api/fms/closing/period/current-month"
        if (acct_path not in self._by_path or curr_path not in self._by_path
                or acct_path in self.writing_gets or curr_path in self.writing_gets):
            return "", ()
        try:
            acct_sets = self._read("fms/config/account-set/list")
        except DataRefused:
            return "", ()
        if not isinstance(acct_sets, list) or not acct_sets:
            return "", ()
        ordered = sorted(
            (a for a in acct_sets if isinstance(a, dict) and a.get("id") is not None),
            key=lambda a: (0 if a.get("defaultStatus") else 1, str(a.get("id"))),
        )[:3]
        if not ordered:
            return "", ()
        cal_month = str(today or "")[:7]
        parts: list[str] = []
        vals: list[str] = []
        if re.fullmatch(r"\d{4}-\d{2}", cal_month):
            parts.append(f"系统当前日历月={cal_month}")
            vals.extend((cal_month, cal_month[:4], str(int(cal_month[5:7]))))
        for acct in ordered:
            aid = acct["id"]
            cname = str(acct.get("companyName") or f"账套#{aid}").strip()
            try:
                raw_month = self._read("fms/closing/period/current-month", accountSetId=aid)
            except DataRefused:
                continue
            period_str = str(raw_month or "").strip()
            m = re.fullmatch(r"(\d{4})-?(\d{2})", period_str)
            if m:
                y, mo = m.group(1), int(m.group(2))
                norm = f"{y}-{mo:02d}"
                parts.append(f"账套「{cname}」(accountSetId={aid}) 当前会计期={norm}（year={y}，period={mo}）")
                vals.extend((str(aid), norm, y, str(mo), f"{y}{mo:02d}"))
            elif period_str:
                parts.append(f"账套「{cname}」(accountSetId={aid}) 当前会计期={period_str}")
                vals.extend((str(aid), period_str))
        if len(parts) <= 1 and not any("当前会计期=" in p for p in parts):
            return "", ()
        note = (
            "财务域双重月份上下文（同账号只读）："
            + "；".join(parts)
            + "。查账套总账/明细账/报表或结账期间时，区分系统当前日历月与账套当前会计期（year/period）。"
        )
        return note, tuple(dict.fromkeys(vals))

    def _walk(self, nodes, names, base):
        for node in nodes or []:
            path = node.get("path") or ""
            full = path if path.startswith("/") else f"{base.rstrip('/')}/{path}"
            trail = names + [node.get("name") or ""]
            children = node.get("children") or []
            if children:
                self._walk(children, trail, full)
            elif node.get("visible", True) and node.get("component"):
                self._menu.append({"name": " > ".join(trail), "path": full, "leaf": trail[-1]})

    def has_page(self, path: str) -> bool:
        self._load()
        return any(m["path"] == path for m in self._menu)

    def menu_matches(self, keyword: str) -> list[dict]:
        self._load()
        ranked = sorted(((_score(keyword, (2, m["leaf"]), (1, m["name"])), m) for m in self._menu),
                        key=lambda pair: -pair[0])
        return [m for score, m in ranked[:8] if score > 2]

    def search_menu(self, keyword: str) -> str:
        hits = self.menu_matches(keyword)
        if not hits:
            return "没有匹配的菜单页面。换一个更短的业务词再试。"
        return "\n".join(f"{m['path']}  {m['name']}" for m in hits)

    def search_api(self, keyword: str, *, include_writes: bool = False) -> str:
        self._load()
        ranked = sorted(((_score(keyword, (2, o["summary"]), (2, o["tag"]), (1, o["path"])), o)
                         for o in self._ops), key=lambda pair: -pair[0])
        lines = [self._api_line(op) for score, op in ranked[:8] if score > 2]
        if include_writes:
            from .domain_toolpacks import format_write_entry_line, search_write_directory

            docs = self._loaded_docs if isinstance(self._loaded_docs, dict) else (
                self._docs if isinstance(self._docs, dict) else None
            )
            write_hits = search_write_directory(keyword, docs=docs, limit=6)
            lines.extend(format_write_entry_line(entry) for entry in write_hits)
        return "\n".join(lines) or "没有匹配的只读接口。换一个业务词再试。"

    def _api_line(self, op) -> str:
        semantics = self.filter_semantics.params_for(op["path"]) if self.filter_semantics else {}
        params = []
        published = sorted(op["op"].get("parameters") or [], key=lambda p: p.get("required") is not True)
        for p in published:
            if p.get("in") != "query":
                continue
            desc = (p.get("description") or "").split("，")[0][:24]
            if p.get("name") == "id":
                desc = "原生记录ID" + (f"；接口说明：{desc}" if desc else "")
            kind = (p.get("schema") or {}).get("type")
            params.append(f"{p['name']}{'*' if p.get('required') else ''}({desc}"
                          + (f"; {kind}" if kind in {"string", "integer", "number", "boolean", "array"} else "")
                          + (f"; {semantics[p['name']]}" if p["name"] in semantics else "") + ")")
        line = (f"GET {op['path']}  [{op['tag']}] {op['summary']}\n    参数: "
                + (", ".join(params[:14]) or "无"))
        if self._query_choices is not None:
            try:
                offered = self._query_choices(op["path"].removeprefix("/admin-api/"))
                names = {p["name"] for p in published if p.get("in") == "query"}
                choices = {key: values for key, values in offered.items() if key in names
                    and isinstance(values, dict) and 0 < len(values) <= 50
                    and all(isinstance(k, str) and 0 < len(k) <= 120 and isinstance(v, str)
                            and 0 < len(v) <= 160 for k, v in values.items())}
            except (DataRefused, ValueError, TypeError, AttributeError):
                choices = {}
            if choices:
                line += "\n原生查询选项（代码=显示名；不是页面就绪或权限证明）：\n" + fenced(
                    redacted(json.dumps(choices, ensure_ascii=False), self.handoffs))
        return line

    def page_path(self, breadcrumb: str) -> str:
        """The path of the menu page the application's navigation shows under these names."""
        self._load()
        wanted = [part.strip() for part in re.split(r"[/>]", breadcrumb) if part.strip()]
        return next((m["path"] for m in self._menu
                     if [part.strip() for part in m["name"].split(">")] == wanted), "")

    def list_path(self, entity: str) -> str:
        """The path of the paged list an entity key names, when the application publishes one."""
        self._load()
        path = f"/admin-api/{entity.strip('/')}/page"
        return path if path in self._by_path else ""

    def _prepare_list_preview(self, entity: str):
        """Prepare optional name context without mutating turn-scoped knowledge state."""
        path = self.list_path(entity)
        if not path:
            return "", None
        published = {p["name"]: p for p in self._by_path[path]["op"].get("parameters", ())
                     if isinstance(p, dict) and p.get("in") == "query" and isinstance(p.get("name"), str)}
        params = {key: value for key, value in (("pageNo", 1), ("pageSize", 20)) if key in published}
        if any(p.get("required") is True and key not in params for key, p in published.items()):
            return "", None
        prepared = self._prepare_call_api(path, params)
        if "error" in prepared:
            return "", prepared
        return prepared["text"], prepared

    def list_preview(self, entity: str) -> str:
        """Optional name context uses only the endpoint's own query contract."""
        _text, prepared = self._prepare_list_preview(entity)
        if prepared is None:
            return ""
        return self._commit_call_api(prepared)

    def list_api(self, entity: str) -> str:
        """The paged list an entity key names, when the application publishes one."""
        self._load()
        op = self._by_path.get(f"/admin-api/{entity.strip('/')}/page")
        return self._api_line(op) if op else ""

    def outside_actor_scope(self, entity: str, name: str) -> bool:
        """Distinguish a scoped miss from a record this actor cannot open."""
        return bool(self.outside_actor_scope_records(entity, name))

    def outside_actor_scope_records(self, entity: str, name: str) -> tuple[tuple[str, str], ...]:
        """Ordinary-list candidates, not a permission verdict about their details."""
        if entity != "crm/customer":
            return ()
        path = self.list_path(entity)
        if not path or path in self.writing_gets:
            return ()
        published = {p["name"]: p for p in self._by_path[path]["op"].get("parameters", ())
                     if isinstance(p, dict) and p.get("in") == "query" and isinstance(p.get("name"), str)}
        params = {"name": name, "pageNo": 1, "pageSize": 5}
        if entity == "crm/customer" and "pool" in published:
            params["pool"] = False
        if (not {"sceneType", *params} <= published.keys()
                or any(p.get("required") is True and key not in params for key, p in published.items())):
            return ()
        self._validate_params(path, params)
        data = self._read(path[len("/admin-api/"):], **params)
        wanted = name.strip().casefold()
        if not wanted or not isinstance(data, dict):
            return ()
        return tuple((str(row["id"]), row["name"]) for row in data.get("list") or ()
                     if isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0
                     and isinstance(row.get("name"), str)
                     and wanted in row["name"].strip().casefold())[:5]

    @staticmethod
    def _stem(label: str) -> str:
        core = re.sub(r"^(?:ERP|CRM|HRM|OA|BPM|FMS|WMS|PMS|商城|系统|会员|交易|财务|人事)\s*", "", label).strip()
        core = re.sub(r"(?:管理|列表|记录|信息|模板|档案|设置|配置|台账)$", "", core).strip()
        if len(core) >= 4 and core.startswith("库存") and core.endswith("单"):
            return core[2:]
        return core

    def suggest(self, sentence: str) -> str:
        """Pages and lists whose own names the person's words contain, found without a
        model request. A first step usually asks for exactly these."""
        self._load()
        said = re.sub(r"\s+", "", sentence)
        exact_pages = [m for m in self._menu if len(m["leaf"]) >= 2 and re.sub(r"\s+", "", m["leaf"]) in said]
        stem_pages = [m for m in self._menu if m not in exact_pages
                      and len(self._stem(m["leaf"])) >= 2 and self._stem(m["leaf"]) in said]
        pages = (sorted(exact_pages, key=lambda m: -len(m["leaf"]))
                 + sorted(stem_pages, key=lambda m: -len(self._stem(m["leaf"]))))[:4]
        lists = []
        for op in self._ops:
            if not op["path"].endswith(("/page", "/list")):
                continue
            noun = op["tag"].split()[-1] if op["tag"].split() else ""
            stem = self._stem(noun)
            token = noun if (len(noun) >= 2 and noun in said) else (stem if (len(stem) >= 2 and stem in said) else "")
            if token:
                lists.append((len(noun in said and noun or ""), len(token), op["path"].endswith("/page"), op, token))
        lists.sort(key=lambda item: (-item[0], -item[1], -int(item[2])))
        nouns = [item[4] for item in lists]
        picked, seen_paths = [], set()
        for _exact_len, _tok_len, _is_page, op, noun in lists:
            if any(noun != other and noun in other for other in nouns):
                continue
            if op["path"] not in seen_paths:
                seen_paths.add(op["path"])
                picked.append(op)
            if len(picked) >= 5:
                break
        if not pages and not picked:
            return ""
        out = ["程序按字面先找到的（可能不全、也可能不是用户指的那个；不合适就用 search_menu / search_api 自己找）:"]
        out += [f"页面 {m['path']}  {m['name']}" for m in pages]
        out += [self._api_line(op) for op in picked]
        return "\n".join(out)

    def _deref(self, schema):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        return self._schemas.get(ref.rsplit("/", 1)[-1], {}) if ref else (schema or {})

    def _row_schema(self, path):
        op = self._by_path.get(path)
        try:
            schema = next(iter(op["op"]["responses"]["200"]["content"].values()))["schema"]
        except (KeyError, StopIteration, TypeError):
            return {}
        for _ in range(6):
            schema = self._deref(schema)
            props = schema.get("properties") or {}
            if "data" in props and "code" in props:
                schema = props["data"]
            elif "list" in props and "total" in props:
                schema = props["list"]
            elif schema.get("type") == "array":
                schema = schema.get("items") or {}
            else:
                return props
        return {}

    def _cell(self, key: str, value):
        """An instant under a time-named key as the application's clock shows it."""
        if (isinstance(value, int) and not isinstance(value, bool) and _TIME_KEY.search(key)
                and 10 ** 11 < value < 10 ** 13):
            return datetime.fromtimestamp(value / 1000, self._zone).strftime("%Y-%m-%d %H:%M:%S")
        return value

    def _row(self, row, fields=()):
        if not isinstance(row, dict):
            return row
        if fields:
            row = {k: row[k] for k in dict.fromkeys(("id", *fields)) if k in row}
        return {k: self._cell(k, v) for k, v in _slim(row).items()}

    def _read(self, path: str, **params):
        """One answer of the application's API as it may be told to a model."""
        return redacted(self.client.get(path, **params), self.handoffs)

    def _pages(self, path: str, params: dict, follow: bool, first=None, *, on_page=None):
        """One read of a list, and with `follow` its later pages."""
        data = self._read(path, **params) if first is None else first
        if isinstance(data, list):
            return data, list(data), len(data), True, 1
        if not (isinstance(data, dict) and isinstance(data.get("list"), list)):
            return data, None, None, True, 1
        rows, total, page = list(data["list"]), data.get("total"), params.get("pageNo")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return data, rows, total, False, 1
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return data, rows, total, False, 1
        if len(rows) > total:
            return data, rows, total, False, 1

        def ids(batch):
            values = [row.get("id") for row in batch if isinstance(row, dict)]
            if (len(values) != len(batch) or any(isinstance(v, bool) or not isinstance(v, (int, str))
                                                 or v == "" for v in values)):
                return None
            return tuple(str(v) for v in values)

        first_page = page
        seen = ids(rows)
        if seen is not None and len(set(seen)) != len(seen):
            return data, rows, total, False, 1
        requests = 1
        if follow and rows and len(rows) < min(total, _ALL_ROWS):
            if seen is None:
                return data, rows, total, False, requests
            visited = set(seen)
            while len(rows) < min(total, _ALL_ROWS):
                page += 1
                requests += 1
                if callable(on_page):
                    on_page(page)
                more = self._read(path, **{**params, "pageNo": page})
                if (not isinstance(more, dict) or type(more.get("total")) is not int
                        or more["total"] != total
                        or not isinstance(more.get("list"), list)):
                    break
                chunk = more["list"]
                next_ids = ids(chunk)
                if (not chunk or next_ids is None or len(set(next_ids)) != len(next_ids)
                        or visited.intersection(next_ids) or len(rows) + len(chunk) > total):
                    break
                rows += chunk
                visited.update(next_ids)
        return data, rows, total, first_page == 1 and len(rows) == total, requests

    def _prepare_call_api(
        self,
        path: str,
        params: dict,
        fields=(),
        *,
        row_offset: int = 0,
        summary=None,
        actor_digest: str = "",
        use_row_sink: bool = False,
        on_page=None,
        allow_user_writing_gets: bool = False,
        allow_cluster_probe: bool = False,
        current_request: str = "",
    ) -> dict:
        """Pure-read preparation for `call_api`; never mutates `self` or `TurnTables`."""
        pages_read: list[int] = []

        def track_page(page_no: int) -> None:
            pages_read.append(page_no)
            if callable(on_page):
                on_page(page_no)

        prepared: dict = {
            "reset_query_answer": True,
            "reset_read": False,
            "turn_targets_update": None,
            "has_last_read": False,
            "last_read": None,
            "last_keys": (),
            "last_query_answer": None,
            "pages_read": pages_read,
        }
        try:
            if use_row_sink and summary is not None:
                raise QueryParametersError("行表读取不接受汇总交付参数")
            if summary is not None:
                if (not isinstance(fields, (tuple, list)) or len(fields) > 12
                        or any(not isinstance(key, str) or not key for key in fields)
                        or len(set(fields)) != len(fields) or row_offset):
                    raise QueryParametersError("summary不能与row_offset同时使用；fields若提供须为最多12个不同列名")
                summary, rewritten = parse_summary_spec(summary)
                used = (*summary.group_by, *summary.columns, *(condition.column for condition in summary.where),
                        *(measure.column for measure in summary.measures if measure.column), *(key.column for key in summary.order_by))
                fields = tuple(dict.fromkeys((*fields, *used))) or ("id",)
                if len(fields) > 12:
                    raise QueryParametersError("summary引用列超过12个，请减少fields或汇总声明中的列")
            else:
                rewritten = ()
            fields = tuple(str(f) for f in fields or () if isinstance(f, str) and f)[:12]
            if type(row_offset) is not int or not 0 <= row_offset <= _ALL_ROWS or row_offset and not fields:
                raise QueryParametersError("row_offset must be an integer from 0 to 5000; nonzero requires fields")
            self._load()
            prepared["reset_read"] = True
            if not path.startswith("/admin-api/"):
                path = "/admin-api/" + path.lstrip("/")
            if path not in self._by_path:
                near = self.search_api(path.replace("/admin-api/", "").replace("/", " "))
                prepared["text"] = f"没有这个只读接口：{path}。相近的接口：\n{near}"
                return prepared
            if path in self.writing_gets:
                if not allow_user_writing_gets or path not in USER_READ_WRITING_GET_ALLOWLIST:
                    prepared["text"] = "这个接口读取时会写入数据，助手不允许调用。"
                    return prepared
                prepared["writing_get_allowed"] = path
            if isinstance(params, dict):
                params = dict(params)
                published_q = {
                    q.get("name"): q for q in self._by_path[path]["op"].get("parameters", ())
                    if isinstance(q, dict) and q.get("in") == "query"
                }
                for k, v in list(params.items()):
                    if (isinstance(v, list) and len(v) == 2
                            and all(isinstance(item, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item) for item in v)):
                        q = published_q.get(k) or {}
                        schema = self._deref(q.get("schema") or {})
                        items = self._deref(schema.get("items") or {})
                        hint = " ".join(str(x or "") for x in (q.get("description"), q.get("example"), schema.get("description")))
                        if items.get("format") == "date-time" or "00:00:00" in hint or "HH:mm:ss" in hint:
                            params[k] = [f"{v[0]} 00:00:00", f"{v[1]} 23:59:59"]
            self._validate_params(path, params)
            filters = {key: value for key, value in params.items() if key not in ("pageNo", "pageSize")}
            relative, props, out = path[len("/admin-api/"):], self._row_schema(path), []
            if summary is not None:
                from ..reads.read_summary import bind_native_conditions
                equalities = self.filter_semantics.equalities_for(path) if self.filter_semantics is not None else None
                summary = bind_native_conditions(summary, filters, props, equalities)
                fields = tuple(dict.fromkeys((*fields, *(c.column for c in summary.where))))
                if len(fields) > 12:
                    raise QueryParametersError("汇总与接口范围条件合计引用列超过12个，未执行读取")
            declared_total = None
            data_lines: set[int] = set()
            try:
                pages_kw = (
                    {"on_page": track_page}
                    if "on_page" in inspect.signature(self._pages).parameters
                    else {}
                )
                data, rows, total, whole, requests = self._pages(
                    relative, params, bool(fields), **pages_kw
                )
                declared_total = total if type(total) is int and total >= 0 else None
                observed = rows if rows is not None else [data]
                if (whole or rows is None) and all(isinstance(row, dict) for row in observed):
                    ids = {str(row["id"]) for row in observed if row.get("id") is not None}
                    if ids:
                        prepared["turn_targets_update"] = (path.rpartition("/")[0], ids)
                if isinstance(data, list):
                    out.append(f"返回 {len(rows)} 行")
                elif rows is not None:
                    count = (f"total={total}（符合条件的总数）" if type(total) is int and total >= 0
                             else "total=未知")
                    out.append(f"{count}，本次读了 {len(rows)} 行"
                               + ("（已读完所有页）" if whole and fields else ""))
                    if requests > 1:
                        out.append("本次经多次分页请求读取，应用没有提供同一时点快照；"
                                   "若读取期间有变更，合计、极值和完整名单可能混合不同时点的记录。")
            except DataRefused as exc:
                if exc.permission_denied or exc.read_failed:
                    raise
                prepared["text"] = f"接口拒绝：{redacted(str(exc)[:200], self.handoffs)}"
                return prepared
            metadata_keys = tuple(dict.fromkeys((*fields, *filters,
                *(rows[0] if rows and isinstance(rows[0], dict)
                  else data if rows is None and isinstance(data, dict) else props))))
            native_names = self._names(relative)
            metadata = self._read_vocabulary(relative, metadata_keys, props, native_names)
            try:
                column_names = dict((self._columns(relative) or {}) if self._columns is not None else {})
            except (DataRefused, LookupError, ValueError):
                column_names = {}
            named, named_lines = self._relation_names(relative, rows if rows is not None else [data], column_names)
            if summary is not None:
                from ..reads.read_summary import summarize
                if rows is None and isinstance(data, dict) and "id" in data:
                    if "id" in filters and str(filters["id"]) != str(data["id"]):
                        raise SummaryRefused("record_identity_mismatch", "detail response does not identify the requested native record")
                    rows, total, whole = [data], 1, True
                    declared_total = 1
                prepared["has_last_read"] = True
                prepared["last_read"] = {"count": 1 if rows is None else len(rows), "scope": self._by_path[path]["tag"],
                                         "complete": whole if rows is not None else None, "declared_total": declared_total,
                                         "values": _cell_values(rows if rows is not None else [data], column_names, entity_slug=_entity_slug_for_path(path))[0]}
                prepared["last_keys"] = tuple(dict.fromkeys(key for row in rows or () if isinstance(row, dict) for key in row))
                result = summarize(path=relative, filters=filters, fields=fields, props=props,
                    deref=self._deref, rows=rows, total=total, whole=whole, requests=requests,
                    actor_digest=actor_digest, zone=self._zone, money_review=self.money_review, spec=summary,
                    columns=column_names, names=native_names, relation_names=named)
                prepared["last_query_answer"] = result
                unverified = [column.name for column in result.columns if column.unverified_unit]
                told, data_idx = ["已按当前账号的真实读取准备程序汇总；数值和名单保存在本轮服务端。"], set()
                if rewritten:
                    told.append("程序对汇总声明的理解：" + "；".join(rewritten) + "。")
                for line, is_data in (("实际接口条件：" + json.dumps(filters, ensure_ascii=False), True),
                                      ("实际汇总声明：" + json.dumps(result.spec.to_json(), ensure_ascii=False), True),
                                      (f"读取 {result.source_count} 行，命中 {result.matched_count} 行，完整={result.complete}。", False),
                                      ("输出列与已核验单位：" + json.dumps([(column.name, column.unit) for column in result.columns],
                                                                  ensure_ascii=False), True)):
                    if is_data:
                        data_idx.add(len(told))
                    told.append(line)
                if unverified:
                    told.append("这些金额列的显示单位未经核验，按接口原值给出并标注：" + "、".join(unverified))
                for line, is_data in named_lines:
                    if is_data:
                        data_idx.add(len(told))
                    told.append(line)
                told.append(result.incomplete_reason or "这份结果只覆盖上述条件；用户的其它义务仍须完成。")
                prepared["text"] = self._told(told, data_idx) + metadata
                return prepared
            candidates = self.money_review.fields_for(relative) if self.money_review is not None else {}
            read_rows = rows if rows is not None else [data]
            certified = {key: spec for key, spec in candidates.items()
                         if any(isinstance(row, dict) and spec.convertible(row.get(key))
                                for row in read_rows)
                         and all(not isinstance(row, dict) or row.get(key) is None
                                 or spec.convertible(row.get(key)) for row in read_rows)}

            def present(row):
                if not isinstance(row, dict):
                    return row
                return {key: certified[key].display_value(value) if key in certified else value
                        for key, value in row.items()}

            if rows is None:
                data = present(data)
            else:
                rows = [present(row) for row in rows]
                unbound_line = ""
                if filters and self.filter_semantics is not None:
                    unbound = self.filter_semantics.unbound_for(path, filters, all_paths=self._by_path)
                    if unbound:
                        hints = [f"{key}（可改用已绑定接口：{'、'.join(alts)}）" for key, alts in unbound]
                        unbound_line = (
                            "提示：以下参数在该接口后端查询中未绑定，筛选可能未生效："
                            + "；".join(hints)
                            + "；请核对返回记录或改用已绑定接口。"
                        )
                if fields:
                    scope_lines = (["本次接口查询范围：业务筛选参数如下（本次调用所提交的条件）：",
                                    json.dumps(filters, ensure_ascii=False)] if filters else
                                   ["本次接口查询范围：未带业务筛选条件；以下 total、合计和分组覆盖当前账号可见的此接口全表，"
                                    "不能当作用户点名的子集。"])
                    if filters and self.filter_semantics is not None:
                        conditions = self.filter_semantics.condition_line(path, filters)
                        if conditions:
                            scope_lines.append("各条件的语义（按部署版本源码编译）：" + conditions)
                    if unbound_line:
                        scope_lines.append(unbound_line)
                    data_lines = {index + len(scope_lines) for index in data_lines}
                    if filters:
                        data_lines.add(1)
                    out[:0] = scope_lines
                elif unbound_line:
                    data_lines = {index + 1 for index in data_lines}
                    out.insert(0, unbound_line)
            column_names = {}
            try:
                column_names = dict((self._columns(relative) or {}) if self._columns is not None else {})
            except (DataRefused, LookupError, ValueError):
                column_names = {}
            entity_slug = _entity_slug_for_path(path)
            prepared["has_last_read"] = True
            prepared["last_read"] = {"count": 1 if rows is None else len(rows), "scope": self._by_path[path]["tag"],
                                     "complete": whole if rows is not None else None,
                                     "declared_total": declared_total,
                                     "values": _cell_values(rows if rows is not None else [data], column_names, entity_slug=entity_slug)[0]}
            prepared["has_unbound_filter"] = bool(rows is not None and unbound_line)
            if rows is None:
                keys = list(data.keys()) if isinstance(data, dict) else []
            else:
                keys = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
            prepared["last_keys"] = tuple(str(key) for key in keys)
            blanked = {k for row in (rows if rows is not None else [data]) if isinstance(row, dict)
                       for k, v in row.items() if v == REDACTED}
            if fields:
                missing = [f for f in fields if f != "id" and f not in keys and f not in props]
                if missing:
                    out.append("提示：本次 fields 中请求的以下列名在返回记录中不存在："
                               + "、".join(missing)
                               + "；请改用下方实际存在的列名（本轮行表已保留接口返回的全部真实列，可直接用真实列名在 sql 中查询，无需重复 call_api）。")
                other = [key for key in dict.fromkeys(k for row in (rows if rows is not None else [data])
                         if isinstance(row, dict) for k in row) if key in props
                         and key not in ("id", *fields) and key not in blanked]
                if other:
                    out.append("本次只展示 fields 指定的列；下面是已返回但未展示的列，需要时在 sql 中直接按列名查询（无需重复 call_api），不能据此说接口没有这些字段：")
                    data_lines.add(len(out))
                    out.append("; ".join(key + ("（" + str(self._deref(props[key]).get("description") or "")[:40] + "）"
                        if key in props and self._deref(props[key]).get("description") else "") for key in other[:32]))
            verified_keys = [key for key in (fields or keys) if key in certified and key in keys]
            if verified_keys:
                out.append("这些字段已按部署前端源码和同记录非零页面样本逐字段核验；"
                           "以下数值是页面显示值，可直接用于本次纯查询，无需再次开页面："
                           + "；".join(f"{key}（{certified[key].display_unit}）"
                                      for key in dict.fromkeys(verified_keys)))
            if metadata:
                out.append(metadata)
            notes = [f"{k}={(self._deref(props[k]).get('description') or '')[:40]}"
                     for k in (fields or keys) if k in props and self._deref(props[k]).get("description")
                     and k not in blanked and k not in certified]
            if notes:
                out.append("字段说明: " + "; ".join(notes[:30]))
            names, read = native_names, (rows if rows is not None else [data])
            coded = {}
            for key in (fields or keys):
                seen = dict.fromkeys(str(row.get(key)) for row in read
                                     if isinstance(row, dict) and row.get(key) not in (None, ""))
                pairs = [(code, names[key][code]) for code in seen if code in names.get(key, {})]
                if pairs:
                    coded[key] = pairs
            if coded:
                out.append("代码列的取值和名称（应用的字典）:")
                data_lines.add(len(out))
                out.append("；".join(f"{key}: " + "、".join(f"{code}={label}" for code, label in pairs[:20])
                                    for key, pairs in coded.items()))
            for line, is_data in named_lines:
                if is_data:
                    data_lines.add(len(out))
                out.append(line)
            merged_col_labels = self._merge_column_labels(column_names, keys, props, blanked)
            code_maps = self._extract_code_maps(native_names, keys, props, blanked)
            sib_prepared = None
            if allow_cluster_probe and rows is not None and summary is None:
                sib_prepared = self._prepare_s3_probe(
                    path=path,
                    filters=filters,
                    rows=rows,
                    fields=fields,
                    col_labels=merged_col_labels,
                    current_request=current_request,
                )
            if use_row_sink:
                if (rows is None and isinstance(data, dict) and "id" in filters
                        and "id" in data and str(filters["id"]) != str(data["id"])):
                    raise TurnReadError("record_identity_mismatch", "详情接口返回的记录编号与请求不符。")
                observed = rows if rows is not None else ([] if data is None else [data])
                shown_rows = [self._row(r, fields) if isinstance(r, dict) else r for r in observed]
                clock_notes: list = []
                if rows is not None:
                    row_keys = tuple(dict.fromkeys(
                        k for r in rows[:5] if isinstance(r, dict) for k in r
                    )) or tuple(props)
                    self._append_completeness_and_clock_notes(
                        clock_notes, props=props, fields=fields, rows=rows, whole=whole,
                        keys=row_keys, require_fields_for_partial=False, always_check_clocks=True)
                prepared["row_sink_payload"] = {
                    "path": relative,
                    "params": filters,
                    "rows": observed,
                    "total": declared_total if rows is not None else len(observed),
                    "complete": whole if rows is not None else True,
                    "published": tuple(dict.fromkeys((*props, *fields))) if not observed else (),
                    "column_labels": merged_col_labels,
                    "code_maps": code_maps,
                }
                prepared["out"] = out
                prepared["data_lines"] = data_lines
                prepared["shown_rows"] = shown_rows
                prepared["clock_notes"] = clock_notes
                prepared["sib_prepared"] = sib_prepared
                return prepared
            if sib_prepared and sib_prepared.get("note"):
                data_lines.add(len(out))
                out.append(sib_prepared["note"])
            if rows is None:
                shown = self._row(data) if isinstance(data, dict) else data
                if isinstance(data, dict):
                    shown.update({k: v for k, v in _slim(data, deep=True).items() if k not in shown})
                data_lines.add(len(out))
                out.append(json.dumps(shown, ensure_ascii=False)[:3500])
                prepared["text"] = self._told(out, data_lines)
                return prepared
            self._append_completeness_and_clock_notes(
                out, props=props, fields=fields, rows=rows, whole=whole,
                keys=keys, require_fields_for_partial=True, always_check_clocks=False)
            prepared["text"] = self._finish_plain_rows(
                out=out, data_lines=data_lines, rows=rows, fields=fields,
                whole=whole, coded=coded, row_offset=row_offset,
            )
            return prepared
        except (QueryParametersError, TurnReadError, SummaryRefused, DataRefused) as exc:
            prepared["error"] = exc
            return prepared

    def _merge_column_labels(self, column_names: dict, keys, props: dict, blanked: set) -> dict[str, str]:
        merged: dict[str, str] = {}
        for k in dict.fromkeys((*column_names, *keys, *props)):
            if k in blanked or _SENSITIVE_COL_KEY.search(k):
                continue
            lbl = str(column_names.get(k) or "").strip()
            if not lbl and k in props:
                desc = str(self._deref(props[k]).get("description") or "").strip()
                lbl = desc.split("，")[0].split(",")[0].split("(")[0].split("（")[0].strip()[:24]
            if lbl:
                merged[k] = lbl
        return merged

    def _extract_code_maps(self, native_names: dict, keys, props: dict, blanked: set) -> dict[str, dict[str, str]]:
        return {
            k: {str(c): str(lbl) for c, lbl in native_names[k].items()}
            for k in dict.fromkeys((*keys, *props))
            if k in native_names and native_names[k] and k not in blanked and not _SENSITIVE_COL_KEY.search(k)
        }

    def _prepare_s3_probe(
        self,
        *,
        path: str,
        filters: dict,
        rows: list,
        fields: tuple,
        col_labels: dict[str, str],
        current_request: str,
    ) -> dict | None:
        from ..reads.evidence_sheet import map_sibling_params, metric_columns, should_trigger_s3_probe

        reason = should_trigger_s3_probe(
            rows, fields=fields, current_request=current_request, col_labels=col_labels
        )
        if not reason:
            return None
        edges = self._s3_bridge().edges_for(path)
        if not edges:
            return None
        for edge in edges[:2]:
            if edge.edge_id in self.probed_s3_edges:
                continue
            sib_path = edge.target_path
            if sib_path in self.writing_gets or sib_path not in self._by_path:
                continue
            sib_op = self._by_path[sib_path]
            sib_params = map_sibling_params(filters, edge, sib_op["op"].get("parameters") or [])
            if sib_params is None:
                continue
            sib_rel = sib_path.removeprefix("/admin-api/")
            try:
                self._validate_params(sib_path, sib_params)
                _d, sib_rows, sib_total, sib_whole, _rq = self._pages(sib_rel, sib_params, False)
            except (QueryParametersError, DataRefused, TurnReadError):
                continue
            if sib_rows is None or len(sib_rows) == 0:
                continue
            dict_sib = [r for r in sib_rows if isinstance(r, dict)]
            sib_metrics = metric_columns(dict_sib)
            if reason == "all_zero_metrics" and sib_metrics and all(
                sum(float(r.get(k) or 0) for r in dict_sib) == 0.0 for k in sib_metrics
            ):
                continue
            sib_props = self._row_schema(sib_path)
            sib_native = self._names(sib_rel)
            try:
                sib_col_names = dict((self._columns(sib_rel) or {}) if self._columns is not None else {})
            except (DataRefused, LookupError, ValueError):
                sib_col_names = {}
            sib_keys = tuple(dict_sib[0].keys()) if dict_sib else tuple(sib_props)
            sib_blanked = {k for r in dict_sib for k, v in r.items() if v == REDACTED}
            sib_labels = self._merge_column_labels(sib_col_names, sib_keys, sib_props, sib_blanked)
            sib_codes = self._extract_code_maps(sib_native, sib_keys, sib_props, sib_blanked)
            sib_shown = [self._row(r) if isinstance(r, dict) else r for r in sib_rows]
            sib_filters = {k: v for k, v in sib_params.items() if k not in {"pageNo", "pageSize"}}
            sib_cands, _ = _cell_values(sib_rows, sib_col_names, entity_slug=_entity_slug_for_path(sib_path))
            eff_total = sib_total if type(sib_total) is int and sib_total >= 0 else len(sib_rows)
            note = (
                f"库存流水旁路对照（{reason} 触发同账号只读补查）：GET {sib_path} "
                f"条件 {json.dumps(sib_filters, ensure_ascii=False)} 返回 {eff_total} 条。"
            )
            return {
                "edge_id": edge.edge_id,
                "reason": reason,
                "sib_path": sib_path,
                "row_sink_payload": {
                    "path": sib_rel,
                    "params": sib_filters,
                    "rows": sib_rows,
                    "total": eff_total,
                    "complete": bool(sib_whole),
                    "published": tuple(dict.fromkeys((*sib_props,))),
                    "column_labels": sib_labels,
                    "code_maps": sib_codes,
                },
                "shown_rows": sib_shown,
                "cell_values": sib_cands,
                "note": note,
            }
        return None

    def _commit_call_api(self, prepared: dict, *, row_sink=None) -> str:
        """Apply turn-scoped `ActorKnowledge` and `TurnTables` mutations on the main thread."""
        if prepared.get("reset_query_answer"):
            self.last_query_answer = None
        if prepared.get("reset_read"):
            self.last_read = None
            self.last_keys = ()
        if prepared.get("turn_targets_update") is not None:
            prefix, ids = prepared["turn_targets_update"]
            self.turn_targets.setdefault(prefix, set()).update(ids)
        if prepared.get("has_last_read"):
            self.last_read = prepared["last_read"]
            self.last_keys = prepared["last_keys"]
        if prepared.get("last_query_answer") is not None:
            self.last_query_answer = prepared["last_query_answer"]
        if "error" in prepared:
            raise prepared["error"]
        if "text" in prepared:
            return prepared["text"]
        out = list(prepared["out"])
        data_lines = set(prepared["data_lines"])
        if row_sink is not None:
            info = row_sink(**prepared["row_sink_payload"])
            out.append(f"本轮行表 {info['name']}，读取回执 {info['receipt']}。")
            data_lines.add(len(out))
            out.append(json.dumps(info, ensure_ascii=False))
            observed = prepared["row_sink_payload"]["rows"]
            if observed:
                data_lines.add(len(out))
                shown_rows = prepared["shown_rows"]
                inline = (json.dumps(shown_rows, ensure_ascii=False)
                          if info["complete"] and len(observed) <= 30 else "")
                if inline and len(inline) <= 9000:
                    out.append(f"全部行（共 {len(observed)} 行，已读完所有页；内联时间字段已按本应用时区换算为 YYYY-MM-DD HH:MM:SS，单表原样字段或名单可直接引用上方读取回执交付，"
                               f"无需再调 tables/sql 重复取行；若需分转元÷100.0 等单位换算、子集计数、多行聚合、差额、分组或排序极值，须调 sql 计算，注意 SQL 行表内时间列仍存原始毫秒整数）：" + inline)
                    ties_note = _inline_small_table_ties_note(
                        observed, prepared["row_sink_payload"].get("column_labels") or {}
                    )
                    if ties_note:
                        out.append(ties_note)
                else:
                    out.append("样例（不是全表）：" + json.dumps(shown_rows[:3], ensure_ascii=False)[:2600])
            else:
                filters_used = prepared["row_sink_payload"].get("params") or {}
                if info.get("complete") and filters_used and not prepared.get("has_unbound_filter"):
                    cond_desc = json.dumps(filters_used, ensure_ascii=False)[:300]
                    out.append(
                        f"精确绑定空集核实（complete=True，0 行）：在已绑定筛选参数 {cond_desc} 下已读取全部结果共 0 行；"
                        "若筛选条件已准确对应用户范围，权威空集已核实，无需为凑记录反复放宽或更换无关查询。"
                    )
            out.extend(prepared["clock_notes"])
            sib = prepared.get("sib_prepared")
            if sib is not None:
                self.probed_s3_edges.add(sib["edge_id"])
                sib_name = f"{info['name']}_sib"
                try:
                    sib_info = row_sink(**sib["row_sink_payload"], table_name=sib_name)
                except TurnReadError:
                    sib_info = None
                if sib_info is not None:
                    has_sib_time = any(re.search(r"Time$|Date$", str(c)) for c in (sib_info.get("columns") or ()))
                    time_hint = "（SQL 行表内时间列存原始毫秒整数，请用 date_range 返回的 start_ms / end_exclusive_ms 比较）" if has_sib_time else ""
                    out.append(
                        f"{sib['note']} 已写入独立关联行表 {sib_info['name']}，读取回执 {sib_info['receipt']}。{time_hint}"
                    )
                    data_lines.add(len(out))
                    out.append(json.dumps(sib_info, ensure_ascii=False))
                    sib_shown = sib["shown_rows"]
                    sib_inline = (
                        json.dumps(sib_shown, ensure_ascii=False)
                        if sib_info["complete"] and len(sib_shown) <= 30
                        else ""
                    )
                    data_lines.add(len(out))
                    if sib_inline and len(sib_inline) <= 6000:
                        out.append(f"{sib_info['name']} 全部行：" + sib_inline)
                    else:
                        out.append(f"{sib_info['name']} 样例：" + json.dumps(sib_shown[:5], ensure_ascii=False)[:2200])
                    if self.last_read is not None and sib.get("cell_values"):
                        existing_vals = list(self.last_read.get("values") or ())
                        self.last_read["values"] = existing_vals + list(sib["cell_values"])
                else:
                    out.append(sib["note"])
        return self._told(out, data_lines)

    def call_api(self, path: str, params: dict, fields=(), *, row_offset: int = 0,
                 summary=None, actor_digest: str = "", row_sink=None, on_page=None,
                 allow_user_writing_gets: bool = True,
                 allow_cluster_probe: bool = False,
                 current_request: str = "") -> str:
        prepared = self._prepare_call_api(
            path, params, fields,
            row_offset=row_offset,
            summary=summary,
            actor_digest=actor_digest,
            use_row_sink=row_sink is not None,
            on_page=on_page,
            allow_user_writing_gets=allow_user_writing_gets,
            allow_cluster_probe=allow_cluster_probe,
            current_request=current_request,
        )
        self.last_writing_get_allowed = bool(prepared.get("writing_get_allowed"))
        return self._commit_call_api(prepared, row_sink=row_sink)

    def _finish_plain_rows(self, *, out: list, data_lines: set, rows: list, fields: tuple,
                           whole: bool, coded: dict, row_offset: int) -> str:
        quantities = []
        for key in fields if whole else ():
            values = [row.get(key) for row in rows if isinstance(row, dict)]
            if _TIME_KEY.search(key):
                continue
            if not values or _NOT_A_QUANTITY.search(key) or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                continue
            quantities.append(key)
            top = max(values)
            highest = [r for r in rows if r.get(key) == top]
            plus, minus = [v for v in values if v > 0], [v for v in values if v < 0]
            signs = (f"；其中正数 {len(plus)} 行合计 {_amount(sum(plus))}，负数 {len(minus)} 行合计 {_amount(sum(minus))}"
                     if plus and minus else "")
            out.append(f"{key}（{len(values)} 行）: 合计 {_amount(sum(values))}，最大 {_amount(top)}，"
                       f"最小 {_amount(min(values))}{signs}；最大的有 {len(highest)} 行:")
            data_lines.add(len(out))
            out.append("；".join(json.dumps(self._row(r, fields), ensure_ascii=False) for r in highest[:6]))
            if len(values) > 1:
                out.append(f"{key} 按数值降序覆盖前 3 名（同值并列全部计入名次）:")
                rank = 1
                for value in sorted(set(values), reverse=True):
                    if rank > 3:
                        break
                    members = [r for r in rows if r.get(key) == value]
                    last = rank + len(members) - 1
                    place = f"第 {rank} 名" if last == rank else f"第 {rank}–{last} 名并列"
                    out.append(f"{place}，{key}={_amount(value)}，{len(members)} 行:")
                    data_lines.add(len(out))
                    out.append("；".join(json.dumps(self._row(r, fields), ensure_ascii=False)
                                        for r in members[:6]))
                    if len(members) > 6:
                        out.append(f"该并列档另有 {len(members) - 6} 行未列出；不能从中任取几行称为唯一名次。")
                    rank = last + 1
        grouped = 0
        for key in fields if whole else ():
            if key in quantities or _TIME_KEY.search(key) or grouped >= 2:
                continue
            groups: dict[str, list] = {}
            for row in rows:
                if isinstance(row, dict) and row.get(key) not in (None, "") and not isinstance(row.get(key), (dict, list)):
                    groups.setdefault(str(row[key]), []).append(row)
            identity = key.endswith("Id")
            if not groups or len(groups) == len(rows) and not identity:
                continue
            grouped += 1
            label = dict(coded.get(key, ()))
            missing = len(rows) - sum(len(members) for members in groups.values())
            out.append(f"按 {key} 分组（{len(groups)} 种）:")
            ranked = sorted(groups.items(), key=lambda item: -len(item[1]))
            boundary = len(ranked[min(2, len(ranked) - 1)][1])
            shown = ranked if len(ranked) <= _GROUPS else [g for g in ranked if len(g[1]) >= boundary][:_GROUPS]
            data_lines.add(len(out))
            out.append("；".join(
                f"{value}" + (f"（{label[value]}）" if value in label else "") + f": {len(members)} 行"
                + "".join(f"，{q} 合计 {_amount(sum(r.get(q) for r in members))}" for q in quantities)
                for value, members in shown))
            out.append(f"缺值 {missing} 行，不计入有效分组。"
                       + ("按编号区分对象，同名不能合并。" if identity else ""))
            if len(shown) < len(groups):
                omitted_ties = sum(len(members) >= boundary for _, members in ranked) - len(shown)
                out.append(f"仅展示 {len(shown)} 组；前三名临界行数 {boundary}。"
                           + (f"另有 {omitted_ties} 组同属临界并列但未展示，不能据此给出完整前三名名单。"
                              if omitted_ties > 0 else "其余组均低于临界行数。"))
        compact = bool(fields) and len(rows) > 25
        columns = tuple(dict.fromkeys(("id", *fields)))
        if compact:
            out.append("以下各行用 JSON 数组表示，列顺序:")
            data_lines.add(len(out))
            out.append(json.dumps(columns, ensure_ascii=False))
        if data_lines:
            out.append("各行:")
        used, listed = sum(len(line) for line in out), 0
        for row in rows[row_offset:] if fields else rows[:25]:
            shown_row = self._row(row, fields)
            line = (json.dumps([shown_row.get(key) for key in columns], ensure_ascii=False,
                               separators=(",", ":")) if compact and isinstance(shown_row, dict)
                    else json.dumps(shown_row, ensure_ascii=False))
            if used + len(line) > _API_CHARS * (2 if fields else 1) and listed:
                break
            data_lines.add(len(out))
            out.append(line)
            used, listed = used + len(line), listed + 1
        if fields:
            end = row_offset + listed
            out.append(f"行展示窗口: row_offset={row_offset}，本窗口列出 {listed} 行；本次共读到 {len(rows)} 行。")
            if end < len(rows):
                out.append(f"next_row_offset={end}；其余 {len(rows) - end} 行尚未展示。"
                           "继续完整名单时保持 path、params、fields 不变，另带此 row_offset 续读；"
                           "这是已读结果的展示位置，不是应用查询参数，不要猜名称条件补读。"
                           "每次仍重新读取应用，不保证各窗口间数据不变。")
        elif listed < len(rows):
            out.append(f"……本次已读的行只列出了前 {listed} 行，其余 {len(rows) - listed} 行未列出。"
                       "要统计、比较或找最大最小：用 fields 只取要用的列；只有结果明确显示已读完所有页，统计才代表全量。")
        return self._told(out, data_lines)

    def _append_completeness_and_clock_notes(
        self,
        out: list,
        *,
        props: dict,
        fields: tuple,
        rows: list,
        whole: bool,
        keys,
        require_fields_for_partial: bool,
        always_check_clocks: bool,
    ) -> None:
        """Append partial-read warning, multi-clock disambiguation, and per-month counts."""
        if (not require_fields_for_partial or fields) and not whole:
            out.append(
                "这次没有读完符合条件的全部记录；不能据已读行给出全量合计、最大最小、分组、完整名单或不存在结论。"
                "请缩小筛选范围，或说明此项无法完整核实。"
            )
        clocks = [
            k for k in dict.fromkeys((*fields, *(keys if (fields or always_check_clocks) else ())))
            if _TIME_KEY.search(k)
        ][:5]
        if whole and rows and len(clocks) > 1:
            named = [(k, (self._deref(props.get(k) or {}).get("description") or "")[:20]) for k in clocks]
            out.append(
                "这些行有几个时间列: "
                + "; ".join(f"{k}={d}" if d else k for k, d in named)
                + "。用户说的是哪个时间（创建、下单、入库…）就用哪一列的行数。"
            )
        for key in (clocks if whole and rows else ()):
            months: dict[str, int] = {}
            for row in rows:
                shown = self._cell(key, row.get(key)) if isinstance(row, dict) else None
                if isinstance(shown, str) and re.match(r"\d{4}-\d{2}", shown):
                    months[shown[:7]] = months.get(shown[:7], 0) + 1
            if months:
                out.append(
                    f"{key} 按月的行数: "
                    + "，".join(f"{month} {count} 行" for month, count in sorted(months.items(), reverse=True)[:12])
                )

    def _relation_names(self, path, rows, column_names):
        """Names for identities a read returns bare, from the reviewed lookup read under the same actor."""
        wanted = _RELATION_NAMES.get(path, {})
        named, lines = {}, []
        for key, (lookup, name_key, title) in wanted.items():
            identities = {str(row[key]) for row in rows or () if isinstance(row, dict)
                          and isinstance(row.get(key), (int, str)) and not isinstance(row.get(key), bool)
                          and row.get(key) != ""}
            if not identities:
                continue
            try:
                listed = self._read(lookup)
            except DataRefused:
                continue
            items = listed if isinstance(listed, list) else (listed or {}).get("list") if isinstance(listed, dict) else None
            names = {str(item.get("id")): item.get(name_key) for item in items or ()
                     if isinstance(item, dict) and str(item.get("id")) in identities
                     and isinstance(item.get(name_key), str) and item.get(name_key) and REDACTED not in item[name_key]}
            if not names:
                continue
            named[key] = names
            column_names.setdefault(key, title)
            lines += [(f"{key}（{title}）是用户编号；对应的用户昵称读自 /admin-api/{lookup}：", False),
                      ("；".join(f"{identity}={name}" for identity, name in sorted(names.items())[:40]), True)]
        return named, lines

    def _validate_params(self, path, params):
        """Validate only the query vocabulary the installed API publishes."""
        if not isinstance(params, dict) or len(params) > 32:
            raise QueryParametersError("query parameters must be a bounded object (maximum 32 fields)")
        published = {q.get("name"): q for q in self._by_path[path]["op"].get("parameters", ())
                     if isinstance(q, dict) and q.get("in") == "query"}
        if set(params) - set(published):
            declared = ", ".join(k for k in list(published)[:24] if isinstance(k, str))
            raise QueryParametersError(
                "query parameter is not declared by this endpoint; use search_api for its fields"
                + (f" (declared: {declared})" if declared else "")
            )
        missing = {key for key, q in published.items() if q.get("required") is True} - set(params)
        if missing:
            names = sorted(key for key in missing if isinstance(key, str)
                           and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key))
            raise QueryParametersError("required query parameters are missing: " + ", ".join(names[:32]))

        def check(value, schema):
            kind = self._deref(schema).get("type")
            if isinstance(value, list):
                if kind not in {None, "array"} or not 1 <= len(value) <= 50:
                    raise ValueError("invalid query array")
                for item in value:
                    if isinstance(item, (list, dict)):
                        raise ValueError("nested query values are unavailable")
                    check(item, schema.get("items", {}))
                return
            if (not isinstance(value, (str, int, float, bool))
                    or isinstance(value, str) and (len(value) > 4000 or re.search(r"[\x00-\x1f\x7f]", value))
                    or isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("invalid query scalar")
            if kind == "integer" and not (type(value) is int or isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,20}", value)):
                raise ValueError("query requires an integer")
            if kind == "number":
                try:
                    if isinstance(value, bool) or not math.isfinite(float(value)):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError("query requires a finite number") from None
            if kind == "boolean" and not (type(value) is bool or value in {"true", "false"}):
                raise ValueError("query requires a boolean")
            if kind == "string" and not isinstance(value, str):
                raise ValueError("query requires text")
            if kind in {"object", "array"}:
                raise ValueError("unsupported query value")
            choices = schema.get("enum")
            if choices and value not in choices and str(value) not in {str(v) for v in choices}:
                raise ValueError("query choice is not declared")

        for key, value in params.items():
            try:
                check(value, published[key].get("schema", {}))
            except ValueError as exc:
                name = key if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key) else "parameter"
                raise QueryParametersError(name + ": " + str(exc)) from None
            if key in {"pageNo", "pageSize"}:
                if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,5}", str(value)):
                    raise QueryParametersError(key + ": paging requires a positive bounded integer")
                if key == "pageSize" and int(value) > 1000:
                    raise QueryParametersError("pageSize: maximum 1000")

    def _names(self, path: str) -> dict:
        if self._codes is None:
            return {}
        try:
            return self._codes(path) or {}
        except (DataRefused, LookupError, ValueError):
            return {}

    def _read_vocabulary(self, path, keys, props, names):
        """Bounded deployment metadata, including choices absent from an empty result."""
        try:
            labels = (self._columns(path) or {}) if self._columns is not None else {}
        except (DataRefused, LookupError, ValueError):
            labels = {}
        try:
            derived = (self._derived_columns(path) or {}) if self._derived_columns is not None else {}
        except (DataRefused, LookupError, ValueError):
            derived = {}
        fields = [key for key in keys if key in props][:24]
        shown = {key: str(labels[key])[:80] for key in fields if key in labels}
        choices = {key: {str(code)[:40]: str(label)[:80]
                         for code, label in list(names[key].items())[:12]}
                   for key in fields if key in names and names[key]}
        choices = dict(list(choices.items())[:8])
        derived_shown = {str(lbl)[:80]: str(expr)[:80] for lbl, expr in list(derived.items())[:8]}
        if not shown and not choices and not derived_shown:
            return ""
        payload: dict = {"read_path": path, "columns": shown, "choices": choices}
        if derived_shown:
            payload["derived_columns"] = derived_shown
        return "\n" + self._told([
            "该实体的部署列表列名与应用字典（不是当前页面就绪证明；空结果也不改变这些代码的含义）：",
            json.dumps(payload, ensure_ascii=False)], {1})

    def _told(self, out: list, data_lines: set) -> str:
        """A read as the model is told it."""
        return redacted(composed((index in data_lines, line) for index, line in enumerate(out)), self.handoffs)
