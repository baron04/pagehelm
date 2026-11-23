"""Independent application oracle (direct YudaoClient reads + independent Playwright DOM/network).

Never reads `UiSource` or product `ActorKnowledge` as ground truth. Page DOM inspection
reads rendered Element Plus tables, headers, cells, filter controls, and the Vue app's
own `/admin-api/` GET responses.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import sync_playwright

from evals.yudao.write_contract.stack import login
from web_operation_agent_yudao.reads.client import YudaoClient

from .ledger import FAMILIES

_LIST_ENDPOINT_RE = re.compile(r"/admin-api/.*/(?:page|list|tree)(?:-[a-z0-9-]+)?$")
_TIME_NUM_RE = re.compile(r"Time$|Date$", re.IGNORECASE)
_CODE_HINT_RE = re.compile(r"(?:status|state|type|category|mode|sex|level|source|channel|kind)$", re.IGNORECASE)


def _route_slug(route: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", route.strip("/")) or "root"


def sign_in_init_script(token: dict) -> str:
    record = lambda value: json.dumps({"c": 0, "e": 1.7976931348623157e308, "v": json.dumps(value)})
    return (
        "(() => { try {"
        f"localStorage.setItem('ACCESS_TOKEN', {json.dumps(record(token['access']))});"
        f"localStorage.setItem('REFRESH_TOKEN', {json.dumps(record(token['refresh']))});"
        f"localStorage.setItem('tenantId', {json.dumps(record(token['tenant']))});"
        "} catch (e) {} })()"
    )


def _inspect_single_route(page, origin: str, route: str) -> dict:
    """Open one route in a clean browser tab and inspect rendered table, filters, and APIs."""
    captured_gets: list[dict] = []

    def on_response(resp) -> None:
        if resp.request.method != "GET":
            return
        parsed = urlsplit(resp.url)
        if not parsed.path.startswith("/admin-api/"):
            return
        if parsed.path.startswith("/admin-api/system/auth/") or parsed.path.startswith("/admin-api/system/tenant/"):
            return
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = None
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        data = payload.get("data") if isinstance(payload, dict) else None
        row_ids: list[str] = []
        total = None
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            total = data.get("total")
            row_ids = [str(r.get("id")) for r in data["list"][:10] if isinstance(r, dict) and r.get("id") is not None]
        elif isinstance(data, list):
            total = len(data)
            row_ids = [str(r.get("id")) for r in data[:10] if isinstance(r, dict) and r.get("id") is not None]
        captured_gets.append({
            "path": parsed.path,
            "params": params,
            "status": resp.status,
            "code": payload.get("code") if isinstance(payload, dict) else None,
            "total": total,
            "row_ids": row_ids,
            "is_list": bool(_LIST_ENDPOINT_RE.search(parsed.path)) and isinstance(data, (dict, list)),
            "sample_rows": (data.get("list")[:3] if isinstance(data, dict) and isinstance(data.get("list"), list)
                            else (data[:3] if isinstance(data, list) else [])),
        })

    page.on("response", on_response)
    error = ""
    try:
        page.goto(origin + route, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_selector(".el-table, .el-form, .el-card, main, .app-main", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        # Wait briefly for loading masks and initial list API calls to finish.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            loading = page.evaluate(
                "() => [...document.querySelectorAll('.el-loading-mask')]"
                ".some(e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0)"
            )
            if not loading and (captured_gets or time.time() + 1.5 > deadline):
                break
            page.wait_for_timeout(150)
        page.wait_for_timeout(350)

        dom = page.evaluate("""() => {
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const tables = [...document.querySelectorAll('.el-table')].filter(t => !t.closest('.el-dialog') && visible(t));
            let headers = [];
            let rows = [];
            if (tables.length) {
                const t = tables[0];
                const ths = [...t.querySelectorAll('.el-table__header-wrapper thead tr:last-child th')];
                headers = ths.map(th => (th.querySelector('.cell')?.innerText || th.innerText || '').replace(/\\s+/g, ' ').trim());
                const trs = [...t.querySelectorAll('.el-table__body-wrapper tbody tr')].slice(0, 5);
                rows = trs.map(tr => {
                    const tds = [...tr.querySelectorAll('td')];
                    const rowObj = {};
                    const cells = tds.map((td, idx) => {
                        let txt = (td.querySelector('.cell')?.innerText || td.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!txt) {
                            const sw = td.querySelector('.el-switch');
                            if (sw) txt = sw.classList.contains('is-checked') ? '__SWITCH_TRUE__' : '__SWITCH_FALSE__';
                        }
                        const h = headers[idx] || ('col_' + idx);
                        if (h) rowObj[h] = txt;
                        return txt;
                    });
                    return {cells, by_header: rowObj};
                });
            }
            const forms = [...document.querySelectorAll('form.el-form')].filter(f => !f.closest('.el-dialog') && visible(f));
            const controls = [];
            let searchBtn = '';
            for (const f of forms) {
                for (const b of f.querySelectorAll('button')) {
                    if (!visible(b)) continue;
                    const txt = (b.innerText || '').replace(/\\s+/g, '').trim();
                    if ((txt === '搜索' || txt === '查询') && !searchBtn) searchBtn = (b.innerText || '').trim();
                }
                for (const item of f.querySelectorAll('.el-form-item')) {
                    if (!visible(item)) continue;
                    const rawLbl = (item.querySelector('.el-form-item__label')?.innerText || '').replace(/\\s+/g, ' ').trim();
                    const label = rawLbl.replace(/[:：]\\s*$/, '').trim();
                    if (!label) continue;
                    const hasRange = !!item.querySelector('.el-date-editor--daterange, .el-date-editor--datetimerange, .el-date-editor--monthrange');
                    const hasDate = !!item.querySelector('.el-date-editor');
                    const hasTree = !!item.querySelector('.el-tree-select');
                    const hasSelect = !!item.querySelector('.el-select');
                    const hasRadio = !!item.querySelector('.el-radio-group');
                    const hasNum = !!item.querySelector('.el-input-number');
                    const input = item.querySelector('input:not([type="hidden"]), textarea');
                    let kind = 'text';
                    if (hasRange) kind = 'daterange';
                    else if (hasDate) kind = 'date';
                    else if (hasTree) kind = 'tree_select';
                    else if (hasSelect) kind = 'select';
                    else if (hasRadio) kind = 'radio';
                    else if (hasNum) kind = 'number';
                    else if (!input) continue;
                    const placeholder = (input?.getAttribute('placeholder') || '').trim();
                    const radioOpts = hasRadio
                        ? [...item.querySelectorAll('.el-radio, .el-radio-button')].map(r => (r.innerText || '').trim()).filter(Boolean)
                        : [];
                    controls.push({
                        label,
                        kind,
                        placeholder,
                        options: radioOpts,
                        current_value: input ? (input.value || '') : '',
                    });
                }
            }
            const hasDetailBtn = !!tables[0] && [...tables[0].querySelectorAll('button, a')]
                .some(el => /详情/.test((el.innerText || '').trim()));
            return {
                final_path: location.pathname,
                title: document.title,
                table_count: tables.length,
                headers,
                rows,
                controls,
                search_button: searchBtn,
                has_detail_btn: hasDetailBtn,
                has_error_toast: !![...document.querySelectorAll('.el-message--error')].find(visible),
            };
        }""")

        # Enrich select filter controls with live dropdown option labels (first 8 options).
        for idx, ctrl in enumerate(dom.get("controls") or ()):
            if ctrl["kind"] != "select" or idx >= 6:
                continue
            try:
                opts = page.evaluate("""(targetLabel) => {
                    const items = [...document.querySelectorAll('form.el-form .el-form-item')];
                    const item = items.find(it => {
                        const l = (it.querySelector('.el-form-item__label')?.innerText || '').replace(/\\s+/g, ' ').trim().replace(/[:：]\\s*$/, '');
                        return l === targetLabel && !it.closest('.el-dialog') && it.getBoundingClientRect().width > 0;
                    });
                    if (!item) return null;
                    const trigger = item.querySelector('.el-select__wrapper, .el-select .el-input__wrapper, .el-select');
                    if (trigger) trigger.click();
                    return true;
                }""", ctrl["label"])
                if opts:
                    page.wait_for_timeout(150)
                    dropdown_opts = page.evaluate("""() => {
                        const pops = [...document.querySelectorAll('.el-select-dropdown__list')].filter(p => p.getBoundingClientRect().height > 0);
                        if (!pops.length) return [];
                        return [...pops[pops.length - 1].querySelectorAll('.el-select-dropdown__item:not(.is-disabled)')]
                            .map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 12);
                    }""")
                    ctrl["options"] = dropdown_opts or []
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(80)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:180]}"
        dom = {
            "final_path": "",
            "title": "",
            "table_count": 0,
            "headers": [],
            "rows": [],
            "controls": [],
            "search_button": "",
            "has_detail_btn": False,
            "has_error_toast": False,
        }
    finally:
        page.remove_listener("response", on_response)

    return {
        "route": route,
        "error": error,
        "dom": dom,
        "api_gets": captured_gets,
    }


def inspect_all_routes(
    routes: list[str],
    *,
    accounts_file: Path,
    cache_dir: Path,
    envs: tuple[int, ...] = (1,),
    account: str = "admin",
    refresh: bool = False,
) -> dict[str, dict]:
    """Inspect all core routes across running local environments and cache snapshots."""
    routes_dir = Path(cache_dir) / "oracle-routes"
    routes_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    todo: list[str] = []
    for r in routes:
        snap = routes_dir / f"{_route_slug(r)}.json"
        if not refresh and snap.is_file():
            results[r] = json.loads(snap.read_text(encoding="utf-8"))
        else:
            todo.append(r)
    if not todo:
        return results

    lock = threading.Lock()
    buckets: list[list[str]] = [[] for _ in envs]
    for i, r in enumerate(todo):
        buckets[i % len(envs)].append(r)

    def worker(env_id: int, batch: list[str]) -> None:
        if not batch:
            return
        origin = f"http://localhost:{40000 + env_id}"
        tok = login(origin, accounts_file, account)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
            ctx.add_init_script(sign_in_init_script(tok))
            page = ctx.new_page()
            # Warm app shell once.
            page.goto(origin + "/", wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_selector(".el-menu, .layout-container, aside", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            for r in batch:
                data = _inspect_single_route(page, origin, r)
                snap = routes_dir / f"{_route_slug(r)}.json"
                snap.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                with lock:
                    results[r] = data
            browser.close()

    with ThreadPoolExecutor(max_workers=len(envs)) as pool:
        futs = [pool.submit(worker, env_id, batch) for env_id, batch in zip(envs, buckets)]
        for fut in futs:
            fut.result()
    return results


class IndependentApiOracle:
    """Direct same-actor backend reader and exact Decimal aggregator."""

    def __init__(
        self,
        client: YudaoClient,
        *,
        refresh_client: Callable[[], YudaoClient] | None = None,
        refresh_interval_s: float = 900.0,
    ):
        self.client = client
        self._refresh_client = refresh_client
        self._refresh_interval_s = float(refresh_interval_s)
        self._last_refresh = time.monotonic()
        self._lock = threading.RLock()
        self._dict_cache: dict[str, dict[str, str]] | None = None
        self._list_cache: dict[tuple[str, tuple], dict] = {}

    def _get(self, rel: str, **params):
        with self._lock:
            if self._refresh_client is not None and (time.monotonic() - self._last_refresh) >= self._refresh_interval_s:
                self.client = self._refresh_client()
                self._last_refresh = time.monotonic()
            client = self.client
        try:
            return client.get(rel, **params)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if self._refresh_client is not None and ("401" in msg or "账号未登录" in msg):
                with self._lock:
                    if self.client is client or (time.monotonic() - self._last_refresh) >= 5.0:
                        self.client = self._refresh_client()
                        self._last_refresh = time.monotonic()
                    client = self.client
                return client.get(rel, **params)
            raise

    def dict_map(self) -> dict[str, dict[str, str]]:
        with self._lock:
            if self._dict_cache is not None:
                return self._dict_cache
        out: dict[str, dict[str, str]] = {}
        try:
            items = self._get("system/dict-data/simple-list") or ()
            for item in items:
                if isinstance(item, dict) and item.get("dictType") is not None:
                    out.setdefault(str(item["dictType"]), {})[str(item.get("value"))] = str(item.get("label") or "")
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._dict_cache = out
        return out

    def fetch_all(self, list_path: str, params: dict | None = None, *, paged: bool = True, max_rows: int = 50000) -> dict:
        rel = list_path.removeprefix("/admin-api/").lstrip("/")
        base_params = {k: v for k, v in (params or {}).items() if k not in {"pageNo", "pageSize"}}
        cache_key = (rel, tuple(sorted((k, json.dumps(v, ensure_ascii=False, sort_keys=True)) for k, v in base_params.items())))
        with self._lock:
            if cache_key in self._list_cache:
                return self._list_cache[cache_key]
        api_params = {}
        for k, v in base_params.items():
            if (
                isinstance(v, list)
                and len(v) == 2
                and all(isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x) for x in v)
            ):
                api_params[k] = [f"{v[0]} 00:00:00", f"{v[1]} 23:59:59"]
            else:
                api_params[k] = v
        rows: list[dict] = []
        declared_total: int | None = None
        complete = False
        error = ""
        try:
            if not paged:
                res = self._get(rel, **api_params)
                if isinstance(res, list):
                    rows = [r for r in res if isinstance(r, dict)]
                    declared_total = len(rows)
                    complete = True
                elif isinstance(res, dict) and isinstance(res.get("list"), list):
                    rows = [r for r in res["list"] if isinstance(r, dict)]
                    declared_total = res.get("total") if isinstance(res.get("total"), int) else len(rows)
                    complete = len(rows) == declared_total
            else:
                page = 1
                page_size = 100
                seen_ids: set[str] = set()
                while True:
                    res = self._get(rel, pageNo=page, pageSize=page_size, **api_params)
                    if isinstance(res, list):
                        rows = [r for r in res if isinstance(r, dict)]
                        declared_total = len(rows)
                        complete = True
                        break
                    if not isinstance(res, dict) or not isinstance(res.get("list"), list):
                        error = f"unexpected list payload: {type(res).__name__}"
                        break
                    batch = [r for r in res["list"] if isinstance(r, dict)]
                    if declared_total is None and isinstance(res.get("total"), int):
                        declared_total = res["total"]
                    for row in batch:
                        rid = row.get("id")
                        if rid is not None:
                            s_id = str(rid)
                            if s_id in seen_ids:
                                continue
                            seen_ids.add(s_id)
                        rows.append(row)
                    if not batch or (declared_total is not None and len(rows) >= declared_total) or len(rows) >= max_rows:
                        complete = declared_total is not None and len(rows) == declared_total
                        break
                    page += 1
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
        out = {
            "path": "/admin-api/" + rel,
            "params": base_params,
            "rows": rows,
            "total": declared_total if declared_total is not None else len(rows),
            "complete": complete,
            "error": error,
        }
        with self._lock:
            if not error and complete and len(rows) <= 25000:
                self._list_cache[cache_key] = out
        return out

    def fetch_detail(self, get_path: str, record_id: str | int) -> dict | None:
        rel = get_path.removeprefix("/admin-api/").lstrip("/")
        try:
            res = self._get(rel, id=record_id)
            return res if isinstance(res, dict) else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def aggregate_stats(rows: list[dict], column: str) -> dict | None:
        nums: list[Decimal] = []
        for row in rows:
            val = row.get(column)
            if val is None or isinstance(val, bool) or val == "":
                continue
            try:
                d = Decimal(str(val))
                if d.is_finite():
                    nums.append(d)
            except InvalidOperation:
                continue
        if not nums:
            return None
        total = sum(nums, Decimal(0))
        mn = min(nums)
        mx = max(nums)
        return {
            "row_count": len(rows),
            "non_null_count": len(nums),
            "sum": format(total.normalize(), "f"),
            "min": format(mn.normalize(), "f"),
            "max": format(mx.normalize(), "f"),
            "min_ties": sum(1 for n in nums if n == mn),
            "max_ties": sum(1 for n in nums if n == mx),
        }


def _resolve_params_for_entity(
    entity: dict,
    page_snapshot: dict | None,
    client: YudaoClient,
    today_str: str,
) -> dict:
    """Resolve valid initial query params using the live page's own GET or OpenAPI defaults."""
    params: dict = dict(entity.get("fixed_params") or {})
    list_path = entity["list_path"]
    qnames = set(entity.get("query_params") or ())
    if page_snapshot:
        for req in page_snapshot.get("api_gets") or ():
            if req.get("path") == list_path and req.get("code") == 0:
                grouped: dict[str, dict[int, str]] = {}
                for k, v in (req.get("params") or {}).items():
                    if k in {"pageNo", "pageSize"} or v == "":
                        continue
                    m_br = re.fullmatch(r"(.+)\[(\d+)\]", str(k))
                    if m_br:
                        grouped.setdefault(m_br.group(1), {})[int(m_br.group(2))] = str(v)
                    elif k in qnames:
                        params.setdefault(k, v)
                for base_k, idx_map in grouped.items():
                    if base_k in qnames and 0 in idx_map and 1 in idx_map:
                        v0, v1 = idx_map[0][:10], idx_map[1][:10]
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v0) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v1):
                            params.setdefault(base_k, [v0, v1])
                break
    if "pool" in qnames and "pool" not in params:
        params["pool"] = False
    if "sceneType" in qnames and "sceneType" not in params:
        params["sceneType"] = 1
    req = set(entity.get("required_params") or ())
    y, m, _d = (today_str or date.today().isoformat()).split("-")
    if "year" in req and "year" not in params:
        params["year"] = int(y)
    if "month" in req and "month" not in params:
        params["month"] = int(m)
    if "startMonth" in req and "startMonth" not in params:
        params["startMonth"] = f"{y}-{m}"
    if "endMonth" in req and "endMonth" not in params:
        params["endMonth"] = f"{y}-{m}"
    if "tabType" in req and "tabType" not in params:
        params["tabType"] = 0
    if "status" in req and "status" not in params:
        params["status"] = 0
    if "scope" in req and "scope" not in params:
        params["scope"] = "my"
    if "archived" in req and "archived" not in params:
        params["archived"] = False
    if "suspensionState" in req and "suspensionState" not in params:
        params["suspensionState"] = 1
    if "accountSetId" in req and "accountSetId" not in params:
        try:
            sets = client.get("fms/account-set/simple-list") or ()
            if isinstance(sets, list) and sets and isinstance(sets[0], dict) and sets[0].get("id") is not None:
                params["accountSetId"] = sets[0]["id"]
        except Exception:  # noqa: BLE001
            pass
    if "deptId" in req and "deptId" not in params:
        try:
            depts = client.get("system/dept/simple-list") or ()
            if isinstance(depts, list) and depts and isinstance(depts[0], dict) and depts[0].get("id") is not None:
                params["deptId"] = depts[0]["id"]
        except Exception:  # noqa: BLE001
            pass
    return params


def _pick_detail_fields(entity: dict, sample_row: dict, page_snapshot: dict | None) -> dict:
    """Pick up to 3 fields (including 1 coded column and 1 numeric/money or code column) present on row."""
    props = entity.get("props") or {}
    coded = [c for c in entity.get("coded_fields") or () if sample_row.get(c) is not None and not isinstance(sample_row.get(c), (dict, list))]
    numeric = [n for n in entity.get("numeric_fields") or () if isinstance(sample_row.get(n), (int, float)) and not isinstance(sample_row.get(n), bool)]
    name_f = entity.get("name_field") or ""
    chosen: list[str] = []
    if coded:
        chosen.append(coded[0])
    if numeric:
        chosen.append(numeric[0])
    if name_f and name_f not in chosen and sample_row.get(name_f) not in (None, ""):
        chosen.append(name_f)
    for k, v in sample_row.items():
        if len(chosen) >= 3:
            break
        if k in chosen or k not in props or isinstance(v, (dict, list)) or v in (None, ""):
            continue
        if _TIME_NUM_RE.search(k):
            continue
        chosen.append(k)
    return {
        "fields": chosen[:3],
        "coded_field": coded[0] if coded else "",
        "numeric_field": numeric[0] if numeric else "",
        "has_rendered_table": bool(page_snapshot and page_snapshot.get("dom", {}).get("headers")),
    }


def _match_query_param(ctrl: dict, entity: dict, page_snapshot: dict | None) -> str:
    """Heuristically associate a visible search control with its OpenAPI query param name."""
    qparams = list(entity.get("query_params") or ())
    qset = set(qparams) - {"pageNo", "pageSize"}
    label = str(ctrl.get("label") or "")
    kind = str(ctrl.get("kind") or "")
    props = entity.get("props") or {}
    # Match by prop description prefix first
    for q in qparams:
        if q in {"pageNo", "pageSize"}:
            continue
        desc = str((props.get(q) or {}).get("description") or "").split("，")[0].split(",")[0].strip()
        if desc and (desc == label or label in desc or desc in label):
            return q
    if kind in {"date", "daterange"}:
        for cand in ("createTime", "orderTime", "inTime", "outTime", "voucherTime", "entryTime"):
            if cand in qset:
                return cand
        for q in qset:
            if _TIME_NUM_RE.search(q):
                return q
    if kind in {"select", "radio"}:
        for cand in ("status", "state", "type", "auditStatus"):
            if cand in qset:
                return cand
        for q in qset:
            if _CODE_HINT_RE.search(q):
                return q
    if kind == "text":
        nf = entity.get("name_field") or ""
        if nf and nf in qset:
            return nf
        for cand in ("name", "no", "code", "title", "username", "nickname", "keyword"):
            if cand in qset:
                return cand
    return ""


def _pick_sample_filter_values(
    entity: dict,
    rows: list[dict],
    page_snapshot: dict | None,
    today_str: str,
    params: dict | None = None,
) -> list[dict]:
    """Select representative visible filter controls & user-style test values from live DOM/data."""
    if not page_snapshot:
        return []
    controls = page_snapshot.get("dom", {}).get("controls") or ()
    search_btn = page_snapshot.get("dom", {}).get("search_button") or "搜索"
    if not controls:
        return []
    picked: list[dict] = []
    seen_kinds: set[str] = set()
    qtypes = entity.get("query_param_types") or {}
    for ctrl in controls:
        kind = ctrl["kind"]
        label = ctrl["label"]
        qparam = _match_query_param(ctrl, entity, page_snapshot)
        if kind == "text" and "text" not in seen_kinds:
            val = ""
            if rows and qparam and isinstance(rows[0].get(qparam), str) and rows[0][qparam].strip():
                val = rows[0][qparam].strip()
            elif rows and entity.get("name_field") and isinstance(rows[0].get(entity["name_field"]), str):
                if qparam not in qtypes or qtypes.get(qparam) == "string":
                    val = rows[0][entity["name_field"]].strip()
            if val:
                seen_kinds.add("text")
                picked.append({
                    "label": label,
                    "kind": "text",
                    "value": val,
                    "value_mode": "exact_text",
                    "query_param": qparam,
                    "search_button": search_btn,
                    "is_month_control": False,
                })
        elif kind in {"select", "radio"} and kind not in seen_kinds:
            opts = [o for o in (ctrl.get("options") or ()) if o and o not in {"全部", "请选择"}]
            if opts:
                seen_kinds.add(kind)
                picked.append({
                    "label": label,
                    "kind": kind,
                    "value": opts[0],
                    "value_mode": "option_label",
                    "query_param": qparam,
                    "search_button": search_btn,
                    "is_month_control": False,
                })
        elif kind in {"date", "daterange"} and "date" not in seen_kinds:
            is_month = (
                "月" in label
                or "期间" in label
                or any(re.fullmatch(r"\d{4}-\d{2}", str(v)) for v in (params or {}).values())
            )
            seen_kinds.add("date")
            if is_month:
                exp_val = "2026-10 ~ 2026-10" if kind == "daterange" else "2026-10"
            else:
                exp_val = "2026-10-01 ~ 2026-10-31" if kind == "daterange" else "2026-10-15"
            picked.append({
                "label": label,
                "kind": kind,
                "value": exp_val,
                "value_mode": "explicit_date",
                "query_param": qparam,
                "search_button": search_btn,
                "is_month_control": is_month,
            })
            # Also include a relative-date test on some date controls (handled in templates via precomputed date range).
            if len(picked) < 3:
                picked.append({
                    "label": label,
                    "kind": kind,
                    "value": "__RELATIVE_MONTH__",
                    "value_mode": "relative_date",
                    "query_param": qparam,
                    "search_button": search_btn,
                    "is_month_control": is_month,
                })
    return picked[:3]


def build_census_plan(
    inventory: dict,
    route_snapshots: dict[str, dict],
    client: YudaoClient,
    *,
    today_str: str = "",
    goal_data=None,
) -> dict:
    """Build deterministic 5-family census cells and per-family coverage accounting."""
    today = today_str or date.today().isoformat()
    oracle = IndependentApiOracle(client)
    entities = inventory["core_entities"]
    pages_by_route = {p["route"]: p for p in inventory["core_pages"]}

    entity_reads: dict[str, dict] = {}
    entity_params: dict[str, dict] = {}
    entity_unresolved: dict[str, list[str]] = {}
    name_index: dict[str, list[dict]] = {}

    for ent in entities:
        key = ent["key"]
        route = ent["routes"][0] if ent["routes"] else ""
        snap = route_snapshots.get(route)
        params = _resolve_params_for_entity(ent, snap, client, today)
        entity_params[key] = params
        unresolved_req = [r for r in (ent.get("required_params") or ()) if r not in params or params[r] in (None, "")]
        entity_unresolved[key] = unresolved_req
        res = oracle.fetch_all(ent["list_path"], params, paged=ent["paged"], max_rows=25000)
        entity_reads[key] = res
        nf = ent.get("name_field") or ""
        if nf and not unresolved_req and not res.get("error"):
            for r in res["rows"][:200]:
                val = str(r.get(nf) or "").strip()
                if len(val) >= 2 and r.get("id") is not None:
                    name_index.setdefault(val, []).append({
                        "entity": key,
                        "id": str(r["id"]),
                        "name": val,
                    })

    multi_entity_names = {
        name: items for name, items in name_index.items()
        if len({it["entity"] for it in items}) >= 2
    }

    # Compiled record routes only used as candidates to probe record-page open_page if applicable.
    compiled_record_routes = {}
    if goal_data is not None and getattr(goal_data, "_ui", None) is not None:
        compiled_record_routes = dict(getattr(goal_data._ui, "records", {}) or {})

    cells: list[dict] = []
    coverage_meta: dict[str, dict] = {
        fam: {
            "applicable_entities": 0,
            "applicable_entity_keys": [],
            "inapplicable_entities": 0,
            "inapplicable_reasons": {},
            "unclassified_entities": 0,
            "unclassified_rate": 0.0,
        }
        for fam in FAMILIES
    }

    for ent in entities:
        key = ent["key"]
        domain = ent["domain"]
        route = ent["routes"][0] if ent["routes"] else ""
        snap = route_snapshots.get(route)
        params = entity_params[key]
        read = entity_reads[key]
        rows = read.get("rows") or []

        # 1. Family: find
        nf = ent.get("name_field") or ""
        unresolved_req = entity_unresolved[key]
        if not nf or unresolved_req:
            coverage_meta["find"]["inapplicable_entities"] += 1
            coverage_meta["find"]["inapplicable_reasons"][key] = (
                "no_own_name_or_doc_no" if not nf else "requires_context_params"
            )
        else:
            coverage_meta["find"]["applicable_entities"] += 1
            coverage_meta["find"]["applicable_entity_keys"].append(key)
            if not rows:
                cells.append({
                    "cell_id": f"find:{key}:empty",
                    "family": "find",
                    "entity": key,
                    "domain": domain,
                    "operation": "find_record",
                    "field_or_control": nf,
                    "field_or_control_type": "display_name" if nf == "name" else "doc_no",
                    "preflight_status": "untested",
                    "preflight_reason": "no_sampled_records",
                })
            else:
                picked_rows = []
                # Prefer a row whose name also exists in another entity if available.
                for r in rows[:50]:
                    nm = str(r.get(nf) or "").strip()
                    if nm in multi_entity_names:
                        picked_rows.append((r, True))
                        break
                for r in rows[:5]:
                    if r.get(nf) and all(r.get("id") != pr.get("id") for pr, _ in picked_rows):
                        picked_rows.append((r, str(r.get(nf) or "").strip() in multi_entity_names))
                    if len(picked_rows) >= 2:
                        break
                for idx, (r, is_multi) in enumerate(picked_rows, start=1):
                    val = str(r.get(nf) or "").strip()
                    if not val:
                        continue
                    ctype = "multi_entity_same_name" if is_multi else (
                        "doc_no" if nf in {"no", "code", "orderNo", "sn", "bizNo"} else "display_name"
                    )
                    cells.append({
                        "cell_id": f"find:{key}:{idx}",
                        "family": "find",
                        "entity": key,
                        "domain": domain,
                        "operation": "find_record",
                        "field_or_control": nf,
                        "field_or_control_type": ctype,
                        "route": route,
                        "list_path": ent["list_path"],
                        "params": params,
                        "query_text": val,
                        "expected_id": str(r.get("id") or ""),
                        "expected_multi_entities": sorted({it["entity"] for it in multi_entity_names.get(val, ())}),
                    })

        # 2. Family: detail
        if not ent["list_path"] and not ent["get_path"]:
            coverage_meta["detail"]["inapplicable_entities"] += 1
            coverage_meta["detail"]["inapplicable_reasons"][key] = "no_read_endpoint"
        else:
            coverage_meta["detail"]["applicable_entities"] += 1
            coverage_meta["detail"]["applicable_entity_keys"].append(key)
            if not rows or not (snap and snap.get("dom", {}).get("headers")):
                cells.append({
                    "cell_id": f"detail:{key}:no_oracle",
                    "family": "detail",
                    "entity": key,
                    "domain": domain,
                    "operation": "get" if ent["has_get"] else "list",
                    "field_or_control": "record",
                    "field_or_control_type": "detail_fields",
                    "preflight_status": "untested",
                    "preflight_reason": "no_sampled_records" if not rows else "no_rendered_page_table",
                })
            else:
                sample = rows[0]
                # Prefer a row whose id appears in live page's rendered rows so DOM cell text is available.
                live_ids = []
                for g in snap.get("api_gets") or ():
                    if g.get("path") == ent["list_path"] and g.get("row_ids"):
                        live_ids = g["row_ids"]
                        break
                if live_ids:
                    match = next((r for r in rows[:20] if str(r.get("id")) == str(live_ids[0])), None)
                    if match is not None:
                        sample = match
                detail_spec = _pick_detail_fields(ent, sample, snap)
                if not detail_spec["fields"]:
                    cells.append({
                        "cell_id": f"detail:{key}:no_fields",
                        "family": "detail",
                        "entity": key,
                        "domain": domain,
                        "operation": "get" if ent["has_get"] else "list",
                        "field_or_control": "none",
                        "field_or_control_type": "detail_fields",
                        "preflight_status": "untested",
                        "preflight_reason": "no_scalar_fields_sampled",
                    })
                else:
                    ctype = (
                        "coded_enum" if detail_spec["coded_field"]
                        else ("money_amount" if detail_spec["numeric_field"] else "plain_scalar")
                    )
                    cells.append({
                        "cell_id": f"detail:{key}:1",
                        "family": "detail",
                        "entity": key,
                        "domain": domain,
                        "operation": "get" if ent["has_get"] else "list",
                        "field_or_control": ",".join(detail_spec["fields"]),
                        "field_or_control_type": ctype,
                        "route": route,
                        "list_path": ent["list_path"],
                        "get_path": ent["get_path"],
                        "has_get": ent["has_get"],
                        "params": params,
                        "record_id": str(sample.get("id") or ""),
                        "name_field": nf,
                        "record_name": str(sample.get(nf) or "") if nf else "",
                        "fields": detail_spec["fields"],
                        "coded_field": detail_spec["coded_field"],
                        "numeric_field": detail_spec["numeric_field"],
                    })

        # 3. Family: aggregate
        if not ent["list_path"]:
            coverage_meta["aggregate"]["inapplicable_entities"] += 1
            coverage_meta["aggregate"]["inapplicable_reasons"][key] = "no_list_endpoint"
        else:
            num_cols = list(ent.get("numeric_fields") or ())
            if not num_cols and ent["has_id_prop"]:
                num_cols = ["id"]
            if not num_cols:
                coverage_meta["aggregate"]["inapplicable_entities"] += 1
                coverage_meta["aggregate"]["inapplicable_reasons"][key] = "no_numeric_column"
            else:
                coverage_meta["aggregate"]["applicable_entities"] += 1
                coverage_meta["aggregate"]["applicable_entity_keys"].append(key)
                if unresolved_req:
                    cells.append({
                        "cell_id": f"aggregate:{key}:unresolved_req",
                        "family": "aggregate",
                        "entity": key,
                        "domain": domain,
                        "operation": "aggregate_unfiltered",
                        "field_or_control": ",".join(unresolved_req),
                        "field_or_control_type": "unfiltered_table",
                        "preflight_status": "untested",
                        "preflight_reason": "unresolved_required_params",
                    })
                elif not rows:
                    cells.append({
                        "cell_id": f"aggregate:{key}:empty",
                        "family": "aggregate",
                        "entity": key,
                        "domain": domain,
                        "operation": "aggregate_unfiltered",
                        "field_or_control": num_cols[0],
                        "field_or_control_type": "unfiltered_table",
                        "preflight_status": "untested",
                        "preflight_reason": "no_sampled_records",
                    })
                else:
                    metric_col = next(
                        (c for c in num_cols if oracle.aggregate_stats(rows[:50], c) is not None),
                        num_cols[0],
                    )
                    cells.append({
                        "cell_id": f"aggregate:{key}:unfiltered",
                        "family": "aggregate",
                        "entity": key,
                        "domain": domain,
                        "operation": "aggregate_unfiltered",
                        "field_or_control": metric_col,
                        "field_or_control_type": "large_table" if read.get("total", 0) > 5000 else "unfiltered_table",
                        "route": route,
                        "list_path": ent["list_path"],
                        "paged": ent["paged"],
                        "params": params,
                        "metric_col": metric_col,
                        "entity_label": ent["label"],
                    })
                    # Also add 1 filtered aggregate cell if a visible filter maps cleanly to a typed query param.
                    qtypes = ent.get("query_param_types") or {}
                    qparam_set = set(ent.get("query_params") or ())
                    for fspec in _pick_sample_filter_values(ent, rows, snap, today, params):
                        qp = fspec.get("query_param") or ""
                        if not qp or qp not in qparam_set or fspec["kind"] != "text":
                            continue
                        raw_qval = rows[0].get(qp)
                        if qtypes.get(qp) == "integer":
                            if not (isinstance(raw_qval, int) and not isinstance(raw_qval, bool)):
                                continue
                            qval = raw_qval
                        elif isinstance(raw_qval, str) and raw_qval.strip():
                            qval = raw_qval.strip()
                        elif qtypes.get(qp) in {"", "string"}:
                            qval = fspec["value"]
                        else:
                            continue
                        cells.append({
                            "cell_id": f"aggregate:{key}:filtered_{qp}",
                            "family": "aggregate",
                            "entity": key,
                            "domain": domain,
                            "operation": "aggregate_filtered",
                            "field_or_control": f"{fspec['label']}={qval}",
                            "field_or_control_type": "filtered_table",
                            "route": route,
                            "list_path": ent["list_path"],
                            "paged": ent["paged"],
                            "params": {**params, qp: qval},
                            "metric_col": metric_col,
                            "entity_label": ent["label"],
                        })
                        break

        # 4. Family: open_page
        if not route:
            coverage_meta["open_page"]["inapplicable_entities"] += 1
            coverage_meta["open_page"]["inapplicable_reasons"][key] = "no_menu_route"
        else:
            coverage_meta["open_page"]["applicable_entities"] += 1
            coverage_meta["open_page"]["applicable_entity_keys"].append(key)
            page_item = pages_by_route.get(route) or {}
            cells.append({
                "cell_id": f"open_page:{key}:menu",
                "family": "open_page",
                "entity": key,
                "domain": domain,
                "operation": "open_menu_page",
                "field_or_control": route,
                "field_or_control_type": "menu_route",
                "route": route,
                "menu_label": page_item.get("label") or ent["label"],
                "list_path": ent["list_path"],
            })
            if route in compiled_record_routes and rows and rows[0].get("id") is not None:
                rec_route = compiled_record_routes[route].format(id=rows[0]["id"])
                cells.append({
                    "cell_id": f"open_page:{key}:record",
                    "family": "open_page",
                    "entity": key,
                    "domain": domain,
                    "operation": "open_record_page",
                    "field_or_control": rec_route,
                    "field_or_control_type": "record_route",
                    "route": rec_route,
                    "menu_label": page_item.get("label") or ent["label"],
                    "list_path": ent["list_path"],
                    "record_id": str(rows[0]["id"]),
                    "record_name": str(rows[0].get(nf) or "") if nf else "",
                })

        # 5. Family: filter_page
        controls = (snap or {}).get("dom", {}).get("controls") or ()
        live_paths = {g.get("path") for g in ((snap or {}).get("api_gets") or ()) if g.get("code") == 0}
        if not route or not controls:
            coverage_meta["filter_page"]["inapplicable_entities"] += 1
            coverage_meta["filter_page"]["inapplicable_reasons"][key] = (
                "no_menu_route" if not route else "no_visible_filter_controls"
            )
        else:
            coverage_meta["filter_page"]["applicable_entities"] += 1
            coverage_meta["filter_page"]["applicable_entity_keys"].append(key)
            if ent["list_path"] not in live_paths:
                cells.append({
                    "cell_id": f"filter_page:{key}:inactive_tab",
                    "family": "filter_page",
                    "entity": key,
                    "domain": domain,
                    "operation": "filter_page",
                    "field_or_control": controls[0]["label"],
                    "field_or_control_type": f"{controls[0]['kind']}_filter",
                    "preflight_status": "untested",
                    "preflight_reason": "inactive_tab_or_prerequisite_query",
                })
                continue
            fspecs = _pick_sample_filter_values(ent, rows, snap, today, params)
            if not fspecs:
                cells.append({
                    "cell_id": f"filter_page:{key}:no_sample",
                    "family": "filter_page",
                    "entity": key,
                    "domain": domain,
                    "operation": "filter_page",
                    "field_or_control": controls[0]["label"],
                    "field_or_control_type": f"{controls[0]['kind']}_filter",
                    "preflight_status": "untested",
                    "preflight_reason": "no_filter_sample_value",
                })
            else:
                for idx, fspec in enumerate(fspecs, start=1):
                    cells.append({
                        "cell_id": f"filter_page:{key}:{idx}",
                        "family": "filter_page",
                        "entity": key,
                        "domain": domain,
                        "operation": f"filter_{fspec['value_mode']}",
                        "field_or_control": f"{fspec['label']}={fspec['value']}",
                        "field_or_control_type": f"{fspec['kind']}_{fspec['value_mode']}",
                        "route": route,
                        "list_path": ent["list_path"],
                        "control_label": fspec["label"],
                        "control_kind": fspec["kind"],
                        "value": fspec["value"],
                        "value_mode": fspec["value_mode"],
                        "query_param": fspec["query_param"],
                        "search_button": fspec["search_button"],
                        "is_month_control": bool(fspec.get("is_month_control")),
                        "base_params": params,
                    })

    return {
        "today": today,
        "entities_count": len(entities),
        "cells": cells,
        "coverage_meta": coverage_meta,
    }
