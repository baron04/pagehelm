"""yudao's goal data: the schema and reads a goal-directed turn resolves against.

The schema is compiled under the acting account from three things the application
itself serves: the account's menus and button permissions (which pages it reaches and
what it may do there), the API documentation (each list endpoint's fields, their Chinese
descriptions and its query parameters), and the dictionaries (coded values' names). A
page becomes an entity only when the account may query it and the documentation has its
list endpoint.

yudao's list endpoints filter only by the parameters each page defines. A read pushes
down the conditions a parameter can narrow by without losing a match, reads every page
of the result up to a bound, and filters the rows with the core's `matches`. CRM lists
apply the account's record permissions only for the scene they are asked for, so they
are read for each scene (owned, involved, subordinates') and merged; a scene is never
left out.
"""
from __future__ import annotations

from collections import OrderedDict
import re
import threading
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from web_operation_agent.goal.data import (EVERYTHING, ID, NAME, Choice, Compare, Cond,
                                            DataRefused, EntitySchema, FieldSchema, Group,
                                            LinkValue, PageSchema, Ref, Row, Schema, as_bool,
                                            matches, parts)
from web_operation_agent.goal.index import ApplicationIndex
from web_operation_agent.goal.resolve.indexing import index_from_schema

from .client import YudaoClient
from .ui_source import UiSource

#: Modules whose pages are offered by default.
#: The modules offered. Empty: every module whose menus the account reaches -- a person
#: asks about whatever their admin UI shows them, not only the modules tested first.
MODULES: tuple[str, ...] = ()
#: Fields that hold credentials or a channel's secret configuration (a payment channel's
#: keys, a storage's access secret, a mail account's password): never read, even where
#: the account may read the record.
_SECRET = re.compile(r"(?i)secret|password|passwd|private_?key|api_?key|token|credential|"
                     r"sign_?key|mch_?key|^config$|config$")
#: Scenes a CRM list is read under: the account's own records, the ones it takes part
#: in, and its subordinates'.
_SCENES = (1, 2, 3)
_PAGE_SIZE = 200
#: The most records one read filters in memory; beyond it the read is refused rather
#: than answered from part of the records.
_MAX_ROWS = 4000
#: Built-in entities every account may list: the people and departments links name.
_USERS, _DEPTS = "system/user", "system/dept"

#: How long a linked entity's names are kept for rows that carry only its numbers.
_SHOWN_FOR_S = 30.0
#: How long a list read is reused: within one turn, never across a person's pause.
_READ_FOR_S = 15.0
_READ_CACHE_SIZE = 128
#: The link a line carries to the record whose detail holds it.
_OWNER = "__owner"
#: The most records whose details are read for a question about all their lines.
_MAX_LINE_OWNERS = 400

_MONEY = re.compile(r"金额|价格|单价|总价|费用|工资|薪|price|amount", re.I)


class YudaoGoalData:
    def __init__(self, client: YudaoClient, api_docs: dict, *, ui: UiSource | None = None,
                 modules: tuple[str, ...] = MODULES, time_zone: str = "Asia/Shanghai",
                 max_rows: int = _MAX_ROWS):
        self._client, self._docs = client, api_docs
        self._ui = ui or UiSource.none()
        self._modules, self._zone_name, self._max_rows = tuple(modules), time_zone, max_rows
        self._lock = threading.Lock()
        self._schema: Schema | None = None
        self._index: ApplicationIndex | None = None
        self._conventions: tuple = ()
        self._source = None
        self._actor = ""
        #: line entity key -> its line table.
        self._lines: dict[str, _Lines] = {}
        #: entity key -> its endpoints, query parameters and field bookkeeping.
        self._entities: dict[str, _Entity] = {}
        self._dicts: dict[str, list[tuple[str, str]]] = {}
        self._dict_names: dict[str, str] = {}
        #: entity -> (when read, id -> name), for links whose rows carry only the number.
        self._shown: dict[str, tuple[float, dict]] = {}
        #: (entity, pushed-down parameters) -> (when read, rows): one answer reads the same
        #: list several times (a count, its rows, a total).
        self._read: OrderedDict[tuple, tuple] = OrderedDict()
        self._read_generation = getattr(client, "read_generation", 0)

    # -- index and schema ---------------------------------------------------------------------
    def attach_conventions(self, conventions) -> None:
        with self._lock:
            self._conventions, self._index = tuple(conventions), None

    def attach_source(self, source) -> None:
        """Compile the deployment's source rules (`YudaoSource`) into the index."""
        with self._lock:
            self._source, self._index = source, None

    def capabilities(self) -> frozenset[str]:
        # Candidate values are records found by name, which every entity here offers.
        return frozenset({"values", "lines"} | ({"source"} if self._source is not None else set()))

    def index(self, *, context=None) -> ApplicationIndex:
        schema = self.schema()
        with self._lock:
            if self._index is None:
                rules, digest = {}, ""
                if self._source is not None:
                    # The rules only: each names its control, and they are shown only for a
                    # question about how the application behaves. Listing every control on
                    # every entity would lengthen what a question about data is read with.
                    rules, _controls = self._source.rules(self._entity_sources(schema))
                    digest = self._source.release
                self._index = index_from_schema(
                    schema, breadcrumbs=self._breadcrumbs, aliases=self._aliases,
                    conventions=self._conventions, rules=rules, source_digest=digest,
                    actor=self._actor)
            return self._index

    def _entity_sources(self, schema: Schema):
        from .source import EntitySource
        out = []
        for key, spec in self._entities.items():
            entity = schema.entity(key)
            if entity is None or not spec.component:
                continue
            out.append(EntitySource(
                key=key, component_dir=spec.component.rsplit("/", 1)[0],
                api_base=spec.list_path.rsplit("/", 1)[0],
                labels={f.key: f.label for f in entity.fields},
                choices={f.key: {c.key: c.label for c in f.choices} for f in entity.fields if f.choices}))
        return tuple(out)

    def schema(self) -> Schema:
        with self._lock:
            if self._schema is None:
                self._schema = self._compile()
            return self._schema

    def _compile(self) -> Schema:
        info = self._client.get("system/auth/get-permission-info")
        permissions = set(info.get("permissions") or ())
        user = info.get("user") or {}
        # Records name a person by their nickname (负责人: 评测员).
        self._actor = str(user.get("nickname") or user.get("username") or "")
        self._dicts = {}
        for item in self._client.get("system/dict-data/simple-list") or ():
            self._dicts.setdefault(str(item["dictType"]), []).append((str(item["value"]), str(item["label"])))
        # What each dictionary is called ("CRM 商机结束状态类型"): the name of a coded field
        # the API leaves undescribed. An account that may not read it goes without.
        self._dict_names = {}
        try:
            for item in self._client.get("system/dict-type/simple-list") or ():
                self._dict_names[str(item.get("type") or "")] = str(item.get("name") or "")
        except DataRefused:
            pass
        self._breadcrumbs, self._aliases = {}, {}
        found: list[tuple[dict, str, str, list[str]]] = []
        for menu, trail in _leaves(info.get("menus") or ()):
            path = "/" + "/".join(trail[1])
            module = trail[1][0] if trail[1] else ""
            if self._modules and module not in self._modules:
                continue
            listing = self._listing(module, trail[1][1:], str(menu.get("component") or ""), permissions)
            if listing is None:
                continue
            found.append((menu, path, listing, trail[0]))
        entities, pages = {}, []
        labels = {}
        for menu, path, (key, list_path), names in found:
            if key not in self._entities:
                self._entities[key] = self._entity(key, list_path, str(menu.get("component") or ""),
                                                   permissions, str(menu["name"]))
            labels.setdefault(key, []).append(str(menu["name"]))
            ops = (("create",) if f"{key.replace('/', ':')}:create" in permissions else ())
            pages.append(PageSchema(key=path, entity=key, label=str(menu["name"]), operations=ops))
            self._breadcrumbs[path] = " / ".join(names)
            self._aliases[path] = ()
        # A label shared by entities of several modules ("客户" in CRM and ERP) keeps its
        # module's name so that it names one of them.
        plain = {k: e.label for k, e in self._entities.items()}
        counts: dict[str, int] = {}
        for label in plain.values():
            counts[label] = counts.get(label, 0) + 1
        for key, entity in self._entities.items():
            label = entity.label if counts[entity.label] == 1 else entity.full_label
            self._aliases[key] = tuple(dict.fromkeys(n for n in labels.get(key, ()) if n != label))
            entities[key] = self._entity_schema(key, label)
        for key, label in ((_USERS, "用户"), (_DEPTS, "部门")):
            entities[key] = EntitySchema(key=key, label=label, fields=(
                FieldSchema("name", "名称", "text", listed=True),
                FieldSchema("id", "编号", "number", listed=True),), operations=frozenset({"read"}),
                name_field="name")
        self._lines = {}
        for key in list(entities):
            for line in self._discover_lines(key, entities[key]):
                self._lines[line.key] = line
                entities[line.key] = line.schema
                owner = entities[key]
                entities[key] = replace(owner, fields=owner.fields + (FieldSchema(
                    key=line.list_key, label=line.schema.label, kind="lines", relation=line.key,
                    on_form=True, inverse=_OWNER, columns=line.columns),))
        return Schema(entities=tuple(entities.values()), pages=tuple(pages))

    def _discover_lines(self, key: str, owner: EntitySchema) -> list["_Lines"]:
        """The line tables a record's detail carries (an order's items, a contract's
        products), named as the page's own tables label their columns. Found by reading one
        record's detail: yudao's documentation gives every nested item the same schema."""
        spec = self._entities.get(key)
        if spec is None or not spec.has_get or not spec.component:
            return []
        labels = self._ui.line_labels(spec.component.rsplit("/", 1)[0])
        if not labels:
            return []
        try:
            listed = self._client.get(spec.list_path, pageNo=1, pageSize=1,
                                      **{**({"sceneType": 1} if "sceneType" in spec.params else {}),
                                         **spec.fixed_params}) or {}
            first = listed if isinstance(listed, list) else (listed.get("list") or [])
            detail = self._client.get(spec.get_path, id=first[0]["id"]) if first else None
        except (DataRefused, KeyError, TypeError):
            return []
        if not isinstance(detail, dict):
            return []
        out = []
        for list_key, items in (detail or {}).items():
            if not (isinstance(items, list) and items and isinstance(items[0], dict)):
                continue
            sample = items[0]
            api_units = self._line_units(spec, list_key)
            fields = [FieldSchema(_OWNER, owner.label, "link", relation=key, listed=True, on_form=True)]
            columns, money = [], {}
            for field_key, value in sample.items():
                label = labels.get(field_key)
                if not label or field_key == "id" or isinstance(value, (list, dict)):
                    continue
                unit = api_units.get(field_key) or _money_unit(label)
                label = re.sub(r"（[^）]*）$", "", label).strip()
                if field_key.endswith("Id"):
                    relation = self._relation(spec, field_key[:-2])
                    if not relation:
                        continue
                    fields.append(FieldSchema(field_key, re.sub(r"(名称|名字)$", "", label) or label,
                                              "link", relation=relation, listed=True, on_form=True))
                    continue
                kind = ("money" if _MONEY.search(field_key) or _MONEY.search(label) or unit in ("元", "分")
                        else "number" if isinstance(value, (int, float)) and not isinstance(value, bool)
                        else "text")
                fields.append(FieldSchema(field_key, label, kind, listed=True, on_form=True))
                if kind == "money":
                    money[field_key] = unit
                if kind in ("money", "number"):
                    columns.append(field_key)
            if len(fields) < 3:
                continue
            line_key = f"{key}/{list_key}"
            label = f"{owner.label}明细"
            out.append(_Lines(key=line_key, owner=key, list_key=list_key, columns=tuple(columns),
                              money=money, schema=EntitySchema(key=line_key, label=label, fields=tuple(fields),
                                                  operations=frozenset({"read"}), lines_of=key)))
        return out

    def _line_units(self, spec, list_key: str) -> dict[str, str]:
        """Raw API units take precedence over a column's formatted display units."""
        schemas = self._docs.get("components", {}).get("schemas", {})

        def dereference(node):
            seen = set()
            while isinstance(node, dict) and "$ref" in node:
                key = node["$ref"].rsplit("/", 1)[-1]
                if key in seen:
                    return {}
                seen.add(key)
                node = schemas.get(key, {})
            return node if isinstance(node, dict) else {}

        operation = self._docs.get("paths", {}).get("/admin-api/" + spec.get_path, {}).get("get", {})
        content = operation.get("responses", {}).get("200", {}).get("content", {})
        node = dereference(next(iter(content.values()), {}).get("schema", {}))
        detail = dereference(node.get("properties", {}).get("data", {}))
        lines = dereference(detail.get("properties", {}).get(list_key, {}))
        item = dereference(lines.get("items", {}))
        return {key: _money_unit(str(dereference(prop).get("description") or ""))
                for key, prop in item.get("properties", {}).items()}

    def _listing(self, module: str, tail: list[str], component: str,
                 permissions: set) -> tuple[str, str] | None:
        """(entity key, list endpoint) of a page: the endpoint its source reads its records
        from, else the one its path's words, or fewer of them, or its component's name --
        whichever the documentation lists and the account may query."""
        paths = self._docs.get("paths", {})
        view = self._ui.views.get(component) if component else None
        endpoint = view.endpoint if view is not None else ""
        if (endpoint and re.search(r"[/-](page|list|tree)$", endpoint) and f"/admin-api{endpoint}" in paths
                and _page_item(paths[f"/admin-api{endpoint}"].get("get", {}),
                               self._docs.get("components", {}).get("schemas", {}))):
            # The page is in the account's menu, so the account may open it; what the
            # endpoint then returns is the application's to decide, as for the page. Its
            # module is the endpoint's own (the mall's pages read `product/...`).
            key = re.sub(r"([/-]page|[/-]list|[/-]tree)$", "", endpoint.strip("/"))
            if "/" in key:
                return key, endpoint.strip("/")
        words = [w for w in component.split("/")[1:-1]] if component.endswith("/index") else []
        candidates = ["-".join(tail), "-".join(tail[-2:]), tail[-1] if tail else "",
                      "-".join(words), words[-1] if words else ""]
        for resource in dict.fromkeys(c for c in candidates if c):
            if (f"{module}:{resource}:query" in permissions
                    and f"/admin-api/{module}/{resource}/page" in paths):
                return f"{module}/{resource}", f"{module}/{resource}/page"
        # The page's own address names a list the documentation describes the items of,
        # from its last word back to its first: a resource several words deep (an
        # accounting ledger, a shift calendar) whose endpoint is a `/list` rather than a
        # `/page`, and whose endpoint's permission is not its path's words. The menu is
        # the evidence the account may open the page, as it is for a page whose own
        # source reads its list.
        for start in range(len(tail)):
            resource = "/".join(tail[start:])
            for suffix in ("page", "list"):
                endpoint = f"{module}/{resource}/{suffix}"
                if (f"/admin-api/{endpoint}" in paths
                        and _page_item(paths[f"/admin-api/{endpoint}"].get("get", {}),
                                       self._docs.get("components", {}).get("schemas", {}))):
                    return f"{module}/{resource}", endpoint
        return None

    def _entity(self, key: str, list_path: str, component: str, permissions: set,
                menu_name: str) -> "_Entity":
        module, resource = key.split("/", 1)
        page = self._docs["paths"][f"/admin-api/{list_path}"]["get"]
        schemas = self._docs.get("components", {}).get("schemas", {})
        item = _page_item(page, schemas)
        props = schemas.get(item, {}).get("properties", {})
        tag = str((page.get("tags") or [""])[0]).replace("管理后台 - ", "").strip()
        # A resource's own list is named as its documentation names it; a view of it
        # ("my-page", "todo-page") is named as its menu is.
        label = (re.sub(r"^[A-Z]+\s+", "", tag) or resource) if list_path.endswith(("/page", "/list")) \
            else menu_name
        params = {q["name"]: q.get("schema", {}) for q in page.get("parameters", ())
                  if q.get("in") == "query" and q["name"] not in ("pageNo", "pageSize")}
        # The parameters the endpoint itself requires: a list read under a parent
        # (an account set, a folder) refuses without them.
        required = tuple(str(q["name"]) for q in page.get("parameters", ())
                         if q.get("in") == "query" and q.get("required") is True
                         and q["name"] not in ("pageNo", "pageSize"))
        #: What the documentation calls each of them ("部门 id", "时间范围"), for a read
        #: that has to say which conditions the page is still waiting for.
        required_labels = {str(q["name"]): str(q.get("description") or q["name"])
                           for q in page.get("parameters", ())
                           if q.get("in") == "query" and q.get("required") is True
                           and q["name"] not in ("pageNo", "pageSize")}
        base = list_path.rsplit("/", 1)[0]
        get_path = f"/admin-api/{base}/get"
        get = self._docs.get("paths", {}).get(get_path, {}).get("get", {})
        # Some endpoints named /get take a userId and create the resource as a
        # side effect when it does not exist (pay/wallet/get). A list record's id
        # is safe to pass only to a documented id-based detail endpoint.
        has_get = any(q.get("in") == "query" and q.get("name") == "id"
                      for q in get.get("parameters", ()))
        view = self._ui.views.get(component.rsplit("/", 1)[0] + "/index") if component else None
        ops = {"read"}
        for suffix, op in (("create", "create"), ("update", "write"), ("delete", "delete")):
            if f"{module}:{resource.replace('/', '-')}:{suffix}" in permissions:
                ops.add(op)
        return _Entity(key=key, module=module, resource=resource, label=label,
                       full_label=(tag or label) if list_path.endswith(("/page", "/list")) else label,
                       props=props, params=params, required=required,
                       required_labels=required_labels, has_get=has_get,
                       get_path=get_path.removeprefix("/admin-api/"), list_path=list_path,
                       view=view, operations=frozenset(ops), component=component,
                       fixed_params={name: value for name, value in view.fixed_params if name in params}
                       if view and view.endpoint.lstrip("/") == list_path
                       else {})

    def _entity_schema(self, key: str, label: str) -> EntitySchema:
        entity = self._entities[key]
        props = entity.props
        columns = list(entity.view.columns) if entity.view else []
        fields, links = [], {}
        # A `<x>Id` whose `<x>` is a record this account lists is a link, displayed by
        # its `<x>Name`; one whose record it does not list (a status, a unit) is shown by
        # its name alone, and the number is left out.
        related = {name[:-2]: self._relation(entity, name[:-2]) for name in props
                   if name.endswith("Id") and len(name) > 2 and _kind(props[name]) in ("number", "text")}
        for name, prop in props.items():
            kind = _kind(prop)
            items = prop.get("items") or {}
            if (kind is None and prop.get("type") == "array" and isinstance(items, dict)
                    and items.get("type") in ("string", "integer") and not _SECRET.search(name)):
                # Several codes in one field ("已配置的支付渠道编码"): read as their names, one
                # after another, and matched as text.
                kind = "text"
                entity.lists[name] = ""
                entity.lists[name] = self._dict_type(entity, name)
                said = _plain_label(str(prop.get("description") or name))
                fields.append(FieldSchema(key=name, label=re.sub(r"编码$", "", said) or said,
                                          kind=kind, searchable=True,
                                          listed=(name in columns) if columns else True, on_form=True))
                continue
            if kind is None or name == "id" or _SECRET.search(name):
                continue
            base = name[:-4] if name.endswith("Name") else ""
            if base and f"{base}Id" in props and related.get(base):
                continue  # the display of a link, carried on the link's value
            if (name.endswith("Id") and not related.get(name[:-2], "~")
                    and f"{name[:-2]}Name" in props):
                continue  # a number whose name is its own field
            if not name.endswith("Id") and f"{name}Name" in props:
                continue  # a code ("oa") whose name ("办公审批") is its own field
            desc = _plain_label(str(prop.get("description") or name))
            coded = f"{base}Id" if base and f"{base}Id" in props else (base if base in props else "")
            if coded:
                # Named as its number or code is ("商机状态编号" -> 商机状态, "流程分类");
                # the name field's own description is the fallback.
                desc = _link_label(_plain_label(str(props[coded].get("description") or "")), desc)
                entity.derived.add(name)
            relation, choices = "", ()
            if kind in ("number", "text") and name.endswith("Id") and len(name) > 2:
                relation = related.get(name[:-2], "")
                if relation:
                    kind = "link"
                    sibling = props.get(f"{name[:-2]}Name", {})
                    desc = _link_label(str(sibling.get("description") or ""), desc)
                    links[name] = f"{name[:-2]}Name" if f"{name[:-2]}Name" in props else ""
            if kind == "number":
                dict_type = self._dict_type(entity, name)
                if dict_type:
                    kind = "choice"
                    choices = tuple(Choice(key=v, label=l) for v, l in self._dicts[dict_type])
                    if desc == name and self._dict_names.get(dict_type):
                        desc = _dict_label(self._dict_names[dict_type])
                elif (_MONEY.search(name) or _MONEY.search(desc)
                      or _money_unit(str(prop.get("description") or "")) in ("元", "分")):
                    kind = "money"
            shown = name in columns or links.get(name, "~") in columns
            if kind == "money":
                entity.money[name] = _money_unit(str(prop.get("description") or ""))
            fields.append(FieldSchema(key=name, label=desc, kind=kind, relation=relation,
                                      choices=choices, searchable=True,
                                      listed=shown if columns else True, on_form=True))
        # The list's column order first, then the rest as the API lists them.
        order = {c: i for i, c in enumerate(columns)}
        fields.sort(key=lambda f: order.get(f.key, order.get(links.get(f.key, ""), len(order))))
        entity.links = links
        # A record is called by its name or number; without either, by the first text
        # its list shows (a leave request by its reason).
        name_field = next((n for n in ("name", "no", "nickname") if n in props), "") or next(
            (c for c in columns if c in props and _kind(props[c]) == "text"
             and not (re.search(r"(Name|No)$", c) and re.sub(r"(Name|No)$", "Id", c) in props)), "")
        entity.name_field = name_field
        return EntitySchema(key=key, label=label, fields=tuple(fields), operations=entity.operations,
                            name_field=name_field if name_field == "name" else "",
                            total="totalPrice" if "totalPrice" in props else "")

    def _relation(self, entity: "_Entity", base: str) -> str:
        """The entity a `<base>Id` field links to: a person or a department, or the entity
        of the same module the base's last words name ("signContact" -> contact)."""
        if base.endswith("User") or base in ("user", "creator"):
            return _USERS
        if base.endswith("Dept") or base == "dept":
            return _DEPTS
        words = re.findall(r"[A-Z]?[a-z0-9]+", base)
        for i in range(len(words)):
            resource = "-".join(w.lower() for w in words[i:])
            modules = self._modules or tuple(dict.fromkeys(k.split("/")[0] for k in self._entities))
            for module in (entity.module,) + tuple(m for m in modules if m != entity.module):
                if f"{module}/{resource}" in self._entities:
                    return f"{module}/{resource}"
                # A resource listed only through views of it ("my", "manager", "copy"):
                # the account's own records are the ones its links name.
                views = sorted(k for k in self._entities if k.startswith(f"{module}/{resource}/"))
                if views:
                    return next((k for k in views if k.endswith("/my")), views[0])
        return ""

    def _dict_type(self, entity: "_Entity", field: str) -> str:
        """The dictionary a coded field's values are named by: the one the list page
        renders the column through, else the one the module's naming gives."""
        if entity.view is not None and field in entity.view.dicts:
            return entity.view.dicts[field] if entity.view.dicts[field] in self._dicts else ""
        snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", field).lower()
        snake = snake[:-3] if snake.endswith("_id") else snake
        last = entity.resource.split("-")[-1].replace("-", "_")
        # A field of several ("channel_codes") is named by the dictionary of one.
        words = (snake, snake[:-1]) if field in entity.lists and snake.endswith("s") else (snake,)
        for word in words:
            for name in (f"{entity.module}_{last}_{word}", f"{entity.module}_{word}",
                         f"{entity.module}_{last}_{word}_type", f"{entity.module}_{word}_type"):
                if name in self._dicts:
                    return name
        # yudao's own convention: a record's status is on or off (CommonStatusEnum).
        if snake == "status" and "common_status" in self._dicts:
            return "common_status"
        return ""

    # -- reads ------------------------------------------------------------------------------
    def time_zone(self) -> str:
        return self._zone_name

    def name_of(self, display: str) -> str:
        return display

    def find(self, entity: str, text: str, *, limit: int = 8) -> tuple[Ref, ...]:
        wanted = text.strip().casefold()
        if not wanted:
            return ()
        told = re.fullmatch(r"(.+?)（(.+)）", text.strip())
        if told is not None:
            # A name as it was offered to choose from, with whose it is ("张伟（温州天成食品
            # 有限公司）"): that person at that customer.
            name, whose = told.group(1), told.group(2)
            rows = self._fetch(entity, self._narrowing(entity, name))
            picked = [r for r in rows if self._display(entity, r) == name and self._whose(entity, r) == whose]
            if picked:
                return tuple(Ref(entity=entity, id=str(r["id"]), display=text.strip(), name=name)
                             for r in picked[:limit])
        rows = self._people(entity) if entity in (_USERS, _DEPTS) else self._fetch(
            entity, self._narrowing(entity, text))
        hits = [(self._display(entity, r), r) for r in rows]
        hits = [(d, r) for d, r in hits if wanted in d.casefold()]
        hits.sort(key=lambda h: (h[0].casefold() != wanted, not h[0].casefold().startswith(wanted), len(h[0])))
        hits = hits[:limit]
        # Two records shown alike ("张伟" at two customers) are told apart by whose they
        # are, so a person asked which can answer; the name itself stays what it is.
        shown = [d for d, _r in hits]
        return tuple(Ref(entity=entity, id=str(r["id"]), name=d,
                         display=(f"{d}（{self._whose(entity, r)}）"
                                  if shown.count(d) > 1 and self._whose(entity, r) else d))
                     for d, r in hits)

    def _whose(self, entity: str, row: dict) -> str:
        """The record a row belongs to, by name: its customer, else its first other link
        that is not a person or a department."""
        spec = self._entities.get(entity)
        links = spec.links if spec is not None else {}
        if row.get("customerName"):
            return str(row["customerName"])
        for key, name_field in links.items():
            if name_field and row.get(name_field) and not key.endswith(("UserId", "DeptId")):
                return str(row[name_field])
        return ""

    def read(self, entity: str, id: str, fields: tuple[str, ...]) -> Row:
        if entity in self._lines:
            owner_id = id.split(":", 1)[0]
            row = next((r for r in self._line_rows(self._lines[entity], Compare(_OWNER, "eq", owner_id))
                        if r["id"] == id), None)
            if row is None:
                raise DataRefused(f"{entity} {id} is not readable")
            return self._row(entity, row, fields)
        spec = self._entities.get(entity)
        if spec is not None and spec.has_get:
            try:
                row = self._client.get(spec.get_path, id=id)
            except DataRefused:
                # A detail endpoint may check the record more strictly than the list the
                # account reads it from; the record as that list shows it is still its own.
                row = next((r for r in self._all(entity) if str(r.get("id")) == str(id)), None)
                if row is None:
                    raise
            if row is None:
                raise DataRefused(f"{entity} {id} is not readable")
            if any(row.get(f) is None for f in fields if f in spec.derived or f in spec.lists):
                # A detail endpoint may leave a number's name unfilled that its list fills.
                listed = next((r for r in self._all(entity) if str(r.get("id")) == str(id)), None)
                if listed is not None:
                    row = {**row, **{f: listed.get(f) for f in (*spec.derived, *spec.lists)
                                     if row.get(f) is None}}
        else:
            row = next((r for r in self._all(entity) if str(r.get("id")) == str(id)), None)
            if row is None:
                raise DataRefused(f"{entity} {id} is not readable")
        return self._row(entity, row, fields)

    def count(self, entity: str, where: Cond) -> int:
        return len(self._matching(entity, where))

    def rows(self, entity: str, where: Cond, *, fields: tuple[str, ...] = (),
             order: tuple[tuple[str, bool], ...] = (), limit: int = 80) -> tuple[Row, ...]:
        rows = [self._row(entity, r, tuple(fields) + tuple(f for f, _d in order))
                for r in self._matching(entity, where)]
        for field, descending in reversed(order):
            rows.sort(key=lambda r: _sortable(r.id if field == ID else r.display if field == NAME
                                              else r.values.get(field)), reverse=descending)
        return tuple(Row(r.id, r.display, {k: v for k, v in r.values.items() if k in fields},
                         {k: v for k, v in r.shown.items() if k in fields}) for r in rows[:limit])

    def total(self, entity: str, where: Cond, field: str) -> tuple[float, str]:
        amount = Decimal(0)
        for row in self._matching(entity, where):
            value = self._row(entity, row, (field,)).values.get(field)
            if isinstance(value, (int, float)):
                amount += Decimal(str(value))
        unit = self._money_for(entity, field)
        return float(amount), "元" if unit in ("元", "分") else unit

    def groups(self, entity: str, where: Cond, field: str, *, limit: int = 12,
               by_month: bool = False) -> tuple[Group, ...]:
        counts: dict[tuple[str, str], int] = {}
        for raw in self._matching(entity, where):
            row = self._row(entity, raw, (field,))
            value = row.values.get(field)
            if by_month and isinstance(value, datetime):
                key = label = value.strftime("%Y-%m")
            elif isinstance(value, LinkValue):
                key, label = value.id, value.display
            else:
                key, label = str(value), str(row.shown.get(field, value))
            counts[(key, label)] = counts.get((key, label), 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        return tuple(Group(key=k, label=l, count=n) for (k, l), n in ordered)

    def page_address(self, page: str, *, view: str = "") -> str:
        return page

    def record_address(self, entity: str, id: str) -> str:
        page = next((p.key for p in self.schema().pages if p.entity == entity), "")
        template = self._ui.records.get(page)
        return template.format(id=id) if template else page

    # -- helpers ---------------------------------------------------------------------------
    def _matching(self, entity: str, where: Cond) -> list[dict]:
        return [r for r in self._fetch(entity, where)
                if matches(where, self._row(entity, r, _fields_in(where)))]

    def _fetch(self, entity: str, where: Cond) -> list[dict]:
        """Every record the account may read that the pushed-down parameters admit."""
        if entity in (_USERS, _DEPTS):
            return self._people(entity)
        self.schema()
        if entity in self._lines:
            return self._line_rows(self._lines[entity], where)
        spec = self._entities.get(entity)
        if spec is None:
            raise DataRefused(f"{entity} is not an entity of this account")
        params = self._pushdown(spec, where)
        params = self._with_parent(spec, params)
        key = (entity, tuple(sorted((k, str(v)) for k, v in params.items())))
        generation = getattr(self._client, "read_generation", 0)
        # Only complete lists are cached. A broader read with the SAME required
        # parent scope can serve a narrower condition; _matching still applies every
        # condition locally. Never borrow a list from another required parent.
        broad = (entity, tuple(sorted((k, str(v)) for k, v in params.items()
                                      if k in spec.required)))
        held = self._cached_read(key, generation)
        if held is None and broad != key:
            held = self._cached_read(broad, generation)
        if held is not None:
            return list(held)
        try:
            rows = self._fetch_pages(entity, spec, params)
        except DataRefused as exc:
            raise self._unmet_conditions(spec, params, exc) from None
        self._keep_read(key, tuple(rows), generation)
        return rows

    def _fetch_pages(self, entity: str, spec: "_Entity", params: dict) -> list[dict]:
        scenes = (_SCENES if "sceneType" in spec.params and "sceneType" not in spec.fixed_params
                  else (None,))
        rows: dict[str, dict] = {}
        for scene in scenes:
            page = 1
            while True:
                query = {**params, **spec.fixed_params}
                if scene is not None:
                    query["sceneType"] = scene
                data = self._client.get(spec.list_path, pageNo=page,
                                        pageSize=_PAGE_SIZE, **query) or {}
                if isinstance(data, list):
                    # An unpaged list: every record at once.
                    pending = list(reversed(data))
                    while pending:
                        row = pending.pop()
                        if isinstance(row, dict):
                            if row.get("id") is not None:
                                rows.setdefault(str(row["id"]), row)
                            # A tree endpoint returns its nested records under children.
                            if spec.list_path.endswith("/tree") and isinstance(row.get("children"), list):
                                pending.extend(reversed(row["children"]))
                        if len(rows) > self._max_rows:
                            raise DataRefused(f"{entity}: more than {self._max_rows} records to filter")
                    break
                batch = data.get("list") or []
                for row in batch:
                    rows.setdefault(str(row.get("id")), row)
                if len(rows) > self._max_rows:
                    raise DataRefused(f"{entity}: more than {self._max_rows} records to filter")
                if len(batch) < _PAGE_SIZE or page * _PAGE_SIZE >= int(data.get("total") or 0):
                    break
                page += 1
        return list(rows.values())

    def _line_rows(self, lines: "_Lines", where: Cond) -> list[dict]:
        """The lines of the owners a condition names, or of every owner the account may
        read, each carrying its owner as a link."""
        owners = [str(c.value if not isinstance(c.value, tuple) else v)
                  for c in parts(where) if isinstance(c, Compare) and c.field == _OWNER
                  and c.op in ("eq", "in")
                  for v in ((c.value,) if not isinstance(c.value, tuple) else c.value)]
        if not owners:
            owners = [str(r.get("id")) for r in self._fetch(lines.owner, EVERYTHING)]
        if len(owners) > _MAX_LINE_OWNERS:
            raise DataRefused(f"{lines.key}: the lines of more than {_MAX_LINE_OWNERS} records")
        spec = self._entities[lines.owner]
        out = []
        for owner_id in dict.fromkeys(owners):
            detail = self._detail(spec, owner_id)
            if lines.list_key not in detail or (detail[lines.list_key] is not None
                                               and not isinstance(detail[lines.list_key], list)):
                raise DataRefused(f"{lines.key} {owner_id}: line table is unavailable")
            for item in (detail or {}).get(lines.list_key) or ():
                out.append({**item, "id": f"{owner_id}:{item.get('id')}", _OWNER: owner_id})
        return out

    def _detail(self, spec: "_Entity", id: str) -> dict | None:
        """A record's detail, kept as briefly as list reads are."""
        key = (spec.get_path, id)
        generation = getattr(self._client, "read_generation", 0)
        held = self._cached_read(key, generation)
        if held is not None:
            return held
        # A failure is not an empty table, and is not cached for a later request.
        detail = self._client.get(spec.get_path, id=id)
        if not isinstance(detail, dict):
            raise DataRefused(f"{spec.key} {id}: detail is unavailable")
        self._keep_read(key, detail, generation)
        return detail

    def _cached_read(self, key, generation):
        with self._lock:
            if generation != getattr(self._client, "read_generation", 0):
                return None
            if generation != self._read_generation:
                self._read.clear()
                self._shown.clear()
                self._read_generation = generation
            held = self._read.get(key)
            if held is None:
                return None
            if time.monotonic() - held[0] >= _READ_FOR_S:
                del self._read[key]
                return None
            self._read.move_to_end(key)
            return held[1]

    def _keep_read(self, key, value, generation):
        with self._lock:
            if generation != getattr(self._client, "read_generation", 0):
                return  # An in-flight old credential read must not seed the new cache.
            self._read[key] = (time.monotonic(), value)
            self._read.move_to_end(key)
            while len(self._read) > _READ_CACHE_SIZE:
                self._read.popitem(last=False)

    def _all(self, entity: str) -> list[dict]:
        return self._fetch(entity, EVERYTHING)

    def _people(self, entity: str) -> list[dict]:
        path = "system/user/simple-list" if entity == _USERS else "system/dept/simple-list"
        rows = self._client.get(path) or []
        return [{"id": r["id"], "name": r.get("nickname") or r.get("name") or str(r["id"])} for r in rows]

    def _narrowing(self, entity: str, text: str) -> Cond:
        """A name search pushed down where the list has a name or number parameter."""
        spec = self._entities.get(entity)
        if spec is not None and spec.name_field in spec.params:
            return Compare(spec.name_field, "contains", text)
        return EVERYTHING

    def _pushdown(self, spec: "_Entity", where: Cond) -> dict:
        """Query parameters that narrow the list without losing a match of `where`: an
        exact number or code, or a name the list matches by containment."""
        params = {}
        for cond in parts(where):
            if not isinstance(cond, Compare) or cond.field not in spec.params:
                continue
            if spec.money.get(cond.field) == "分":
                # Conditions use the same yuan values as facts and totals. Filter in
                # memory rather than passing yuan to an endpoint that expects cents.
                continue
            kind = spec.params[cond.field].get("type")
            value = cond.value
            if cond.op == "in" and isinstance(value, tuple) and len(value) == 1:
                value, op = value[0], "eq"
            else:
                op = cond.op
            if kind in ("integer", "number") and op == "eq" and str(value).lstrip("-").isdigit():
                params[cond.field] = value
            elif kind == "boolean" and op == "eq":
                # Only a value that is a yes/no word is pushed down: the application
                # ignores a word it does not know, and the read that comes back
                # unfiltered is then counted as if the condition had been applied.
                flag = as_bool(value)
                if flag is not None:
                    params[cond.field] = "true" if flag else "false"
            elif kind == "string" and op in ("eq", "contains") and value:
                params[cond.field] = value
        return params

    def _with_parent(self, spec: "_Entity", params: dict) -> dict:
        """The parent a list needs that the question did not name, when the account can
        read exactly one such record: the account set a ledger is read under, the one
        official account a page shows. The application itself would show that one -- a
        ledger page restores the account set it may use, a mall page selects the first
        account -- so the read is the account's own, not a choice made for it. Several
        candidates means the person must choose, and the list refuses as it did."""
        for name in spec.required:
            if name in params or name in spec.fixed_params or not name.endswith("Id"):
                continue
            parent = self._sole_parent(spec, name[:-2])
            if parent != "":
                params[name] = parent
        return params

    def _sole_parent(self, spec: "_Entity", base: str) -> object:
        """The id of the one record the required parameter's name points at, or "" when
        the name points at no entity of this account, at an entity whose own list needs a
        parent, or at several records."""
        key = self._parent_entity(spec, base)
        target = self._entities.get(key) if key else None
        if target is None or target.required:
            return ""
        try:
            answer = self._client.get(target.list_path, pageNo=1, pageSize=2,
                                      **target.fixed_params) or {}
        except DataRefused:
            return ""
        rows = answer if isinstance(answer, list) else (answer.get("list") or [])
        if len(rows) == 1 and rows[0].get("id") is not None:
            return rows[0]["id"]
        return ""

    def _unmet_conditions(self, spec: "_Entity", params: dict, exc: DataRefused) -> DataRefused:
        """A refused read, with the conditions it left out named as the documentation names
        them. The application reports one missing parameter at a time, and a page may need
        several before it shows anything ("部门 id" and "时间范围"); only the endpoint's own
        required parameters are named, and none of them is supplied."""
        said = exc.reason
        if not said:
            return exc
        given = set(params) | set(spec.fixed_params)
        labels = [spec.required_labels.get(name, name) for name in spec.required
                  if name not in given]
        # A label the application's own sentence already names is not repeated: the two
        # sources word the same parameter their own way ("部门 id" and "部门 id 不能为空",
        # "文件夹标识" and "文件夹类型"), and they share their opening characters.
        rest = [label for label in labels if label and label[:2] not in said]
        if not rest:
            return exc
        more = "、".join(rest)
        return DataRefused(f"{exc}（还需要：{more}）",
                           reason=f"{said}（这项列表还需要先给出：{more}）")

    def _parent_entity(self, spec: "_Entity", base: str) -> str:
        """The entity a required parameter's words name: the one of this entity's own
        module whose resource ends with those words ("accountSet" -> the accounting
        module's account set, "account" -> the official account of the mall's pages),
        else one of another module when that alone names it."""
        resource = "-".join(w.lower() for w in re.findall(r"[A-Z]?[a-z0-9]+", base))
        if not resource:
            return ""
        same, other = [], []
        for key in self._entities:
            module, _, tail = key.partition("/")
            last = tail.rsplit("/", 1)[-1]
            if last == resource or last.endswith("-" + resource):
                (same if module == spec.module else other).append(key)
        found = same or other
        return found[0] if len(found) == 1 else ""

    def _display(self, entity: str, row: dict) -> str:
        if entity in (_USERS, _DEPTS):
            return str(row.get("name") or row.get("id"))
        if entity in self._lines:
            # A line is called by what it is of: its product.
            return str(row.get("productName") or row.get("name") or row.get("id"))
        spec = self._entities.get(entity)
        for key in ((spec.name_field,) if spec and spec.name_field else ()) + ("name", "no", "nickname"):
            if row.get(key):
                return str(row[key])
        # Neither name nor number: the first values its list shows of its own (a
        # receivable plan: "期数 2 · 计划回款金额 21,650.00"), not of the records it names.
        schema = self.schema().entity(entity) if spec is not None else None
        shown = []
        for info in (schema.fields if schema is not None else ()):
            if len(shown) == 2:
                break
            if (not info.listed or info.kind in ("link", "text") or info.key in spec.derived
                    or row.get(info.key) in (None, "")):
                continue
            shown.append(f"{info.label} {self._value(info, row.get(info.key), row, spec)[1]}")
        return " · ".join(shown) or str(row.get("id"))

    def _row(self, entity: str, row: dict, fields) -> Row:
        schema = self.schema().entity(entity)
        spec = self._entities.get(entity)
        values, shown = {}, {}
        for key in fields:
            if key in (NAME, ID):
                continue
            info = schema.field(key) if schema is not None else None
            raw = row.get(key)
            if info is None:
                values[key] = raw
                continue
            value, text = self._value(info, raw, row, spec,
                                      money_unit=self._money_for(entity, key))
            values[key], shown[key] = value, text
        return Row(id=str(row.get("id")), display=self._display(entity, row), values=values, shown=shown)

    def _money_for(self, entity: str, key: str) -> str:
        spec = self._entities.get(entity) or self._lines.get(entity)
        return spec.money.get(key, "") if spec is not None else ""

    def _value(self, info: FieldSchema, raw, row: dict, spec, *, money_unit: str = ""):
        if raw is None or raw == "":
            return None, ("否" if info.kind == "bool" else "(empty)")
        if info.kind == "link":
            name_key = (spec.links.get(info.key) if spec is not None else "") or ""
            display = str(row.get(name_key) or "") if name_key else ""
            if not display and info.relation in (_USERS, _DEPTS):
                display = next((p["name"] for p in self._people(info.relation)
                                if str(p["id"]) == str(raw)), "")
            elif not display and info.relation in self._entities:
                display = self._display_of(info.relation, str(raw))
            return LinkValue(str(raw), display or str(raw)), display or str(raw)
        if info.kind in ("date", "datetime") and isinstance(raw, (int, float)):
            moment = datetime.fromtimestamp(raw / 1000, self._zone()).replace(tzinfo=None)
            return moment, (moment.strftime("%Y-%m-%d") if moment.hour == moment.minute == 0
                            else moment.strftime("%Y-%m-%d %H:%M"))
        if spec is not None and info.key in spec.lists and isinstance(raw, (list, tuple)):
            names = dict(self._dicts.get(spec.lists[info.key], ()))
            text = "、".join(names.get(str(v), str(v)) for v in raw)
            return (text, text) if text else (None, "(empty)")
        if info.kind == "choice":
            key = str(raw)
            return key, next((c.label for c in info.choices if c.key == key), key)
        if info.kind == "money" and isinstance(raw, (int, float)):
            unit = money_unit or (spec.money.get(info.key, "") if spec is not None else "")
            amount = Decimal(str(raw)) / (100 if unit == "分" else 1)
            suffix = " 元" if unit in ("元", "分") else (" " + unit if unit else "")
            return float(amount), f"{amount:,.2f}{suffix}"
        if info.kind == "bool":
            return bool(raw), "是" if raw else "否"
        return raw, str(raw)

    def _display_of(self, entity: str, id: str) -> str:
        """A linked record's name when the row carries only its number, from the list the
        account reads it from (kept briefly: one answer reads many rows)."""
        generation = getattr(self._client, "read_generation", 0)
        self._cached_read(("generation_check",), generation)
        now = time.monotonic()
        with self._lock:
            held = self._shown.get(entity)
        if held is None or now - held[0] > _SHOWN_FOR_S:
            try:
                rows = self._all(entity)
            except DataRefused:
                rows = []
            held = (now, {str(r.get("id")): self._display(entity, r) for r in rows})
            with self._lock:
                if generation == getattr(self._client, "read_generation", 0):
                    self._shown[entity] = held
        return held[1].get(id, "")

    def _zone(self):
        try:
            return ZoneInfo(self._zone_name)
        except ZoneInfoNotFoundError:
            return None


@dataclass(frozen=True)
class _Lines:
    """A line table of a record's detail, as an entity of its own."""
    key: str
    owner: str
    list_key: str
    columns: tuple[str, ...]
    schema: EntitySchema
    money: dict[str, str] = field(default_factory=dict)


class _Entity:
    __slots__ = ("key", "module", "resource", "label", "full_label", "props", "params", "required",
                 "required_labels", "has_get", "view", "operations", "links", "name_field",
                 "derived", "get_path", "list_path", "component", "lists", "fixed_params", "money")

    def __init__(self, **kw):
        self.links, self.name_field, self.derived = {}, "", set()
        #: The parameters the endpoint requires of every read, and what the
        #: documentation calls them.
        self.required: tuple[str, ...] = ()
        self.required_labels: dict[str, str] = {}
        #: Fields holding several codes or words (a payment app's channels) -> the
        #: dictionary naming each, or "" when they are shown as they are.
        self.lists = {}
        self.money: dict[str, str] = {}
        for k, v in kw.items():
            setattr(self, k, v)


def _leaves(menus, names=(), path=()):
    """(menu, (menu names from the top, path words from the top)) for each page menu."""
    for menu in menus:
        words = tuple(w for w in str(menu.get("path") or "").strip("/").split("/") if w)
        if str(menu.get("path") or "").startswith("http"):
            continue
        here = (names + (str(menu.get("name") or ""),), path + words)
        children = menu.get("children") or []
        if children:
            yield from _leaves(children, *here)
        elif str(menu.get("component") or "").endswith("index"):
            yield menu, here


def _dict_label(name: str) -> str:
    """A dictionary's name as a field's: without the module's tag and the word for a kind
    ("CRM 商机结束状态类型" -> "商机结束状态")."""
    name = re.sub(r"^[A-Za-z]+\s+", "", name.strip())
    return re.sub(r"类型$", "", name) or name


def _page_item(operation: dict, schemas: dict) -> str:
    """The item schema of a list endpoint's `CommonResult<PageResult<Item>>`, or of an
    unpaged one's `CommonResult<List<Item>>` (a tree's nodes, a short list of levels)."""
    try:
        ref = operation["responses"]["200"]["content"]["*/*"]["schema"]["$ref"].split("/")[-1]
        data = schemas[ref]["properties"]["data"]
        if data.get("type") == "array":
            return data["items"]["$ref"].split("/")[-1]
        page = data["$ref"].split("/")[-1]
        return schemas[page]["properties"]["list"]["items"]["$ref"].split("/")[-1]
    except (KeyError, TypeError):
        return ""


def _kind(prop: dict) -> str | None:
    kind, fmt = prop.get("type"), prop.get("format")
    if fmt == "date-time":
        return "datetime"
    if fmt == "date":
        return "date"
    return {"string": "text", "integer": "number", "number": "number", "boolean": "bool"}.get(kind)


def _link_label(name_desc: str, id_desc: str) -> str:
    """What a link is called: its display field's name without 名称/名字, else its id
    field's without 编号 ("客户名称" -> 客户, "负责人的用户编号" -> 负责人)."""
    for text in (name_desc, id_desc):
        label = re.sub(r"(的)?(用户)?(名称|名字|昵称|编号|ID|Id)$", "", _plain_label(text))
        if label:
            return label
    return id_desc


def _money_unit(text: str) -> str:
    """Keep the API or column's declared monetary unit; never infer a currency."""
    found = re.search(r"单位\s*[:：]?\s*([^，,；;（）()\s]+)|[（(]\s*(元|分)\s*[）)]", text)
    return (found.group(1) or found.group(2)) if found else ""


def _plain_label(text: str) -> str:
    """A field description as a label: without the unit or format note after a comma
    ("合计产品价格，单位：元" -> 合计产品价格)."""
    return re.split(r"[，,]\s*(?:单位|百分比|格式|例如|参见)", text.strip(), maxsplit=1)[0].strip() or text.strip()


def _fields_in(cond) -> tuple[str, ...]:
    out: list[str] = []

    def walk(c):
        if isinstance(c, Compare):
            out.append(c.field)
        for child in getattr(c, "items", ()) or ():
            walk(child)
        if hasattr(c, "item"):
            walk(c.item)
    walk(cond)
    return tuple(dict.fromkeys(out))


def _sortable(value):
    if value is None:
        return (0, "")
    if isinstance(value, LinkValue):
        return (1, value.display)
    if isinstance(value, (int, float)):
        return (1, float(value))
    return (1, str(value))


__all__ = ["MODULES", "YudaoGoalData"]
