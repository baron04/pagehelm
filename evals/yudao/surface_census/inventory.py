"""Core entity and page inventory derived from live menus, OpenAPI docs, and compiled schema.

Contains zero entity names or page route literals. All core filtering is expressed via
domain module prefixes and generic path-segment rules matching Section 3.1 of the design.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Iterable

#: Non-core modules excluded by Section 3.1 (运维 infra, AI ai, 即时通讯 im, 公众号 mp).
EXCLUDED_MODULES: frozenset[str] = frozenset({"infra", "ai", "im", "mp"})

#: Top-level module prefixes mapped to the 8 core business domains + project management (pms).
MODULE_TO_DOMAIN: dict[str, str] = {
    "crm": "crm",
    "erp": "erp_wms",
    "wms": "erp_wms",
    "mall": "mall_promotion",
    "product": "mall_promotion",
    "trade": "mall_promotion",
    "promotion": "mall_promotion",
    "member": "mall_promotion",
    "statistics": "mall_promotion",
    "fms": "fms_pay",
    "pay": "fms_pay",
    "hrm": "hrm",
    "oa": "oa_bpm",
    "bpm": "oa_bpm",
    "pms": "pms",
    "mes": "mes_iot",
    "iot": "mes_iot",
    "system": "system",
}

_NAME_CANDIDATES: tuple[str, ...] = (
    "name", "no", "code", "title", "nickname", "username", "subject",
    "orderNo", "sn", "bizNo", "sourceNo", "trackNo",
)
_NUMERIC_EXCLUDE_RE = re.compile(
    r"(?:^|[a-z_])(?:id|status|state|type|sex|level|enable|flag|deleted|tenant)$"
    r"|(?:Time|Date|Id)$"
    r"|(?:token|secret|password|credential)",
    re.IGNORECASE,
)


def domain_for_module(module: str) -> str:
    return MODULE_TO_DOMAIN.get(module.strip().lower(), "")


def is_core_route(route: str, entity: str = "") -> bool:
    """Return True if the route/entity belongs to the 8 business domains + pms core scope."""
    route_clean = str(route or "").strip("/")
    entity_clean = str(entity or "").strip("/")
    route_parts = [p for p in route_clean.split("/") if p]
    entity_parts = [p for p in entity_clean.split("/") if p]
    if not route_parts and not entity_parts:
        return False
    mod_r = route_parts[0].lower() if route_parts else ""
    mod_e = entity_parts[0].lower() if entity_parts else ""
    if mod_r in EXCLUDED_MODULES or mod_e in EXCLUDED_MODULES:
        return False
    # Exclude demo/example pages such as payment demo routes without hardcoding entity names.
    if "demo" in [p.lower() for p in route_parts] or "demo" in [p.lower() for p in entity_parts]:
        return False
    if mod_e and mod_e not in MODULE_TO_DOMAIN:
        return False
    if mod_r and mod_r not in MODULE_TO_DOMAIN:
        return False
    return bool(mod_r or mod_e)


@dataclass(frozen=True)
class CorePageItem:
    route: str
    label: str
    breadcrumb: str
    domain: str
    module: str
    entity: str
    component: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CoreEntityItem:
    key: str
    label: str
    domain: str
    module: str
    routes: tuple[str, ...]
    menu_labels: tuple[str, ...]
    list_path: str
    get_path: str
    has_get: bool
    has_id_prop: bool
    paged: bool
    required_params: tuple[str, ...]
    query_params: tuple[str, ...]
    query_param_types: dict = field(default_factory=dict)
    fixed_params: dict = field(default_factory=dict)
    name_field: str = ""
    coded_fields: tuple[str, ...] = ()
    numeric_fields: tuple[str, ...] = ()
    time_fields: tuple[str, ...] = ()
    props: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def walk_menu_pages(menus: Iterable[dict]) -> list[dict]:
    """Extract all visible leaf menu pages with resolved route paths and breadcrumbs."""
    out: list[dict] = []

    def walk(nodes, names: tuple[str, ...], base: str) -> None:
        for node in nodes or ():
            if not isinstance(node, dict) or node.get("visible") is False:
                continue
            raw_path = str(node.get("path") or "").strip()
            name = str(node.get("name") or "").strip()
            full = raw_path if raw_path.startswith("/") else (
                f"{base.rstrip('/')}/{raw_path}" if raw_path else base
            )
            trail = (*names, name) if name else names
            children = node.get("children") or ()
            if children:
                walk(children, trail, full)
                continue
            component = str(node.get("component") or "").strip()
            if component and component not in {"Layout", "ParentView", "BaseLayout"} and re.fullmatch(
                r"/[a-z0-9][a-z0-9/_-]*", full
            ):
                out.append({
                    "route": full,
                    "label": name,
                    "breadcrumb": " > ".join(trail),
                    "component": component,
                })

    walk(menus, (), "")
    dedup: dict[str, dict] = {}
    for row in out:
        dedup.setdefault(row["route"], row)
    return list(dedup.values())


def _deref_schema(schema: dict, schemas: dict) -> dict:
    seen: set[str] = set()
    node = schema if isinstance(schema, dict) else {}
    while isinstance(node, dict) and "$ref" in node:
        ref = str(node["$ref"]).rsplit("/", 1)[-1]
        if ref in seen:
            return {}
        seen.add(ref)
        node = schemas.get(ref, {})
    return node if isinstance(node, dict) else {}


def _guess_list_endpoint_from_openapi(route: str, component: str, paths: dict, schemas: dict) -> str:
    """Resolve an OpenAPI list GET path from route/component segments without using UiSource."""
    parts = [p for p in route.strip("/").split("/") if p]
    if not parts:
        return ""
    module = parts[0]
    tail = parts[1:]
    comp_parts = [p for p in component.removesuffix("/index").split("/") if p]
    comp_tail = comp_parts[1:] if len(comp_parts) > 1 else []
    candidates: list[str] = []
    for seq in (tail, comp_tail):
        if seq:
            candidates.append("-".join(seq))
            candidates.append("/".join(seq))
            if len(seq) >= 2:
                candidates.append("-".join(seq[-2:]))
            candidates.append(seq[-1])
    for res in dict.fromkeys(c for c in candidates if c):
        for suffix in ("page", "list", "tree"):
            ep = f"/admin-api/{module}/{res}/{suffix}"
            op = (paths.get(ep) or {}).get("get")
            if isinstance(op, dict):
                return ep
    return ""


def _response_item_props(op: dict, schemas: dict) -> tuple[dict, bool]:
    """Return `(item_properties, is_paged)` for an OpenAPI GET operation."""
    try:
        content = next(iter((op.get("responses") or {}).get("200", {}).get("content", {}).values()))
        root = _deref_schema(content.get("schema") or {}, schemas)
        data = _deref_schema((root.get("properties") or {}).get("data") or {}, schemas)
    except (StopIteration, AttributeError, TypeError):
        return {}, False
    if data.get("type") == "array":
        item = _deref_schema(data.get("items") or {}, schemas)
        return dict(item.get("properties") or {}), False
    props = data.get("properties") or {}
    if "list" in props:
        lst = _deref_schema(props.get("list") or {}, schemas)
        item = _deref_schema(lst.get("items") or {}, schemas)
        return dict(item.get("properties") or {}), True
    return dict(props), False


def build_inventory(
    *,
    menus: list[dict],
    api_docs: dict,
    compiled_outline=None,
    compiled_entities_meta: dict | None = None,
) -> dict:
    """Build core page & entity inventory from menus + OpenAPI (+ optional compiled metadata)."""
    paths = api_docs.get("paths") or {}
    schemas = (api_docs.get("components") or {}).get("schemas") or {}
    menu_pages = walk_menu_pages(menus)

    compiled_page_by_route: dict[str, object] = {}
    compiled_schema_by_entity: dict[str, object] = {}
    if compiled_outline is not None:
        for p in getattr(compiled_outline, "pages", ()):
            compiled_page_by_route[p.key] = p
        for e in getattr(compiled_outline, "entities", ()):
            compiled_schema_by_entity[e.key] = e

    core_pages: list[CorePageItem] = []
    excluded_pages: list[dict] = []
    entities_acc: dict[str, dict] = {}

    for m in menu_pages:
        route = m["route"]
        c_page = compiled_page_by_route.get(route)
        ent_key = getattr(c_page, "entity", "") if c_page is not None else ""
        if not is_core_route(route, ent_key):
            excluded_pages.append({"route": route, "entity": ent_key, "label": m["label"]})
            continue
        mod = (ent_key.split("/", 1)[0] if ent_key else route.strip("/").split("/", 1)[0]).lower()
        dom = domain_for_module(mod)
        core_pages.append(CorePageItem(
            route=route,
            label=m["label"],
            breadcrumb=m["breadcrumb"],
            domain=dom,
            module=mod,
            entity=ent_key,
            component=m["component"],
        ))
        # Determine entity + list_path
        resolved_ent = ent_key
        list_path = ""
        fixed_params: dict = {}
        if compiled_entities_meta and ent_key in compiled_entities_meta:
            meta = compiled_entities_meta[ent_key]
            list_path = "/admin-api/" + str(getattr(meta, "list_path", "")).lstrip("/")
            fixed_params = dict(getattr(meta, "fixed_params", {}) or {})
        if not list_path:
            list_path = _guess_list_endpoint_from_openapi(route, m["component"], paths, schemas)
            if list_path and not resolved_ent:
                resolved_ent = re.sub(
                    r"/(?:page|list|tree)$",
                    "",
                    list_path.removeprefix("/admin-api/"),
                )
        if not resolved_ent or not list_path or list_path not in paths:
            continue
        bucket = entities_acc.setdefault(resolved_ent, {
            "routes": [],
            "labels": [],
            "list_path": list_path,
            "fixed_params": fixed_params,
            "module": resolved_ent.split("/", 1)[0].lower(),
            "domain": domain_for_module(resolved_ent.split("/", 1)[0].lower()) or dom,
        })
        if route not in bucket["routes"]:
            bucket["routes"].append(route)
        if m["label"] and m["label"] not in bucket["labels"]:
            bucket["labels"].append(m["label"])

    core_entities: list[CoreEntityItem] = []
    for key, info in sorted(entities_acc.items()):
        list_path = info["list_path"]
        op = (paths.get(list_path) or {}).get("get") or {}
        props, paged = _response_item_props(op, schemas)
        qparams = [q for q in op.get("parameters") or () if isinstance(q, dict) and q.get("in") == "query"]
        qnames = tuple(str(q["name"]) for q in qparams if q.get("name"))
        qparam_types = {
            str(q["name"]): str((q.get("schema") or {}).get("type") or "")
            for q in qparams if q.get("name")
        }
        req_params = tuple(
            str(q["name"]) for q in qparams
            if q.get("required") is True and q.get("name") not in {"pageNo", "pageSize"}
        )
        base = list_path.rsplit("/", 1)[0]
        get_path = f"{base}/get"
        get_detail_path = f"{base}/get-detail"
        resolved_get = ""
        for cand in (get_path, get_detail_path):
            gop = (paths.get(cand) or {}).get("get")
            if isinstance(gop, dict) and any(
                q.get("in") == "query" and q.get("name") == "id"
                for q in gop.get("parameters") or ()
            ):
                resolved_get = cand
                break
        tag = str((op.get("tags") or [""])[0]).replace("管理后台 - ", "").strip()
        label = re.sub(r"^[A-Z]+\s+", "", tag) or (info["labels"][0] if info["labels"] else key)
        c_schema = compiled_schema_by_entity.get(key)
        c_fields = {f.key: f for f in getattr(c_schema, "fields", ())}

        def _is_valid_name_prop(prop_key: str) -> bool:
            if prop_key not in props or _deref_schema(props[prop_key], schemas).get("type") != "string":
                return False
            if prop_key.endswith("Name") and len(prop_key) > 4 and (prop_key[:-4] + "Id") in props:
                return False
            if prop_key.endswith(("Time", "Date", "Url", "Avatar", "Type", "Status")):
                return False
            if "Channel" in prop_key:
                return False
            return True

        name_field = ""
        meta_obj = compiled_entities_meta.get(key) if compiled_entities_meta else None
        meta_nf = str(getattr(meta_obj, "name_field", "") or "")
        if meta_nf and _is_valid_name_prop(meta_nf):
            name_field = meta_nf
        if not name_field:
            for cand in _NAME_CANDIDATES:
                if _is_valid_name_prop(cand):
                    name_field = cand
                    break
        if not name_field:
            for k in props:
                if _is_valid_name_prop(k) and k.endswith(("Name", "Title", "No")):
                    name_field = k
                    break
        coded_fields = tuple(
            f.key for f in getattr(c_schema, "fields", ())
            if getattr(f, "kind", "") == "choice" and f.key in props
        )
        if not coded_fields:
            coded_fields = tuple(
                k for k, p in props.items()
                if any(tok in k.lower() for tok in ("status", "type", "state", "category", "mode", "sex"))
                and _deref_schema(p, schemas).get("type") in {"integer", "string"}
                and not k.endswith("Name")
            )
        numeric_fields = tuple(
            k for k, p in props.items()
            if _deref_schema(p, schemas).get("type") in {"integer", "number"}
            and k not in coded_fields
            and not _NUMERIC_EXCLUDE_RE.search(k)
        )
        time_fields = tuple(
            k for k, p in props.items()
            if _deref_schema(p, schemas).get("format") in {"date", "date-time"}
            or k.endswith(("Time", "Date"))
        )
        slim_props = {
            k: {
                "type": _deref_schema(v, schemas).get("type"),
                "format": _deref_schema(v, schemas).get("format"),
                "description": str(_deref_schema(v, schemas).get("description") or "")[:120],
            }
            for k, v in props.items()
        }
        core_entities.append(CoreEntityItem(
            key=key,
            label=label,
            domain=info["domain"],
            module=info["module"],
            routes=tuple(info["routes"]),
            menu_labels=tuple(info["labels"]),
            list_path=list_path,
            get_path=resolved_get,
            has_get=bool(resolved_get),
            has_id_prop="id" in props,
            paged=paged,
            required_params=req_params,
            query_params=qnames,
            query_param_types=qparam_types,
            fixed_params=info["fixed_params"],
            name_field=name_field,
            coded_fields=coded_fields,
            numeric_fields=numeric_fields,
            time_fields=time_fields,
            props=slim_props,
        ))
        del c_fields

    return {
        "core_pages": [p.to_dict() for p in core_pages],
        "core_entities": [e.to_dict() for e in core_entities],
        "excluded_pages": excluded_pages,
    }
