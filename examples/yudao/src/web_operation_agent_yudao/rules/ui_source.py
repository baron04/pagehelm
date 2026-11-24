"""Optional facts from the deployed admin UI's source (yudao-ui-admin-vue3).

Without it the adapter still works: every field the API returns is shown, choices come
from dictionary names that follow the modules' naming, and a record opens as its list
page. With it, four things are exact rather than guessed: the endpoint a list page reads
its records from, the columns it shows and their order, the dictionary each coded column
renders through, and the address of a record's own page. Source reads stay under the
deployed UI's utils/dict.ts, router/modules/remaining.ts, views/**/*.vue, and the
api modules those pages import.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from dataclasses import dataclass, field
from pathlib import Path

_DICT_CONST = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*'([a-z0-9_]+)'", re.M)
_COLUMN = re.compile(r"<el-table-column\b[^>]*?\bprop=\"([A-Za-z0-9_.]+)\"", re.S)
_DICT_TAG = re.compile(r"<dict-tag\b[^>]*?:type=\"DICT_TYPE\.([A-Z0-9_]+)\"[^>]*?:value=\"(?:scope\.)?row\.([A-Za-z0-9_]+)\"", re.S)
_ROUTE_PATH = re.compile(r"path:\s*'([^']+)'")
_ACTIVE_MENU = re.compile(r"activeMenu:\s*'([^']+)'")
#: A loader that populates the page's main list. Several modules name it for the
#: resource (getProjectList) rather than simply getList.
_GET_LIST = re.compile(r"(?:const (get\w*(?:List|Page)) = (?:async\s*)?[^\n]*|"
                       r"async function (get\w*(?:List|Page))\([^)]*\)[^\n]*)\n(.*?)\n}", re.S)
_LOAD_LIST = re.compile(r"(?:const (load\w*|init) = (?:async\s*)?[^\n]*|"
                        r"async function (load\w*|init)\([^)]*\)[^\n]*)\n(.*?)\n}", re.S)
_API_CALL = re.compile(r"await\s+(?:(\w+)\.)?(\w+)\(")
_API_REFERENCE = re.compile(r"\b(\w+Api)\.(get\w+(?:Page|List|Tree))\b")
_API_IMPORT = r"import\s+(?:\*\s+as\s+{name}|\{{[^}}]*\b{name}\b[^}}]*\}})\s+from\s+'@/api/([^']+)'"
_API_GET = re.compile(r"request\.get(?:<[^\n]*>)?\s*\(\s*\{\s*url:\s*[`']([^`'?$]+)")
#: A table column, its label, and the row field its body binds ("row.productPrice",
#: `${$index}.productId`), for the line tables of a record's form.
_TABLE_COLUMN = re.compile(r"<el-table-column\b([^>]*?)(?:/>|>(.*?)</el-table-column>)", re.S)
_COLUMN_LABEL = re.compile(r'\blabel="([^"]+)"')
_COLUMN_PROP = re.compile(r'(?<![:\w-])prop="([A-Za-z0-9_]+)"')
_ROW_BINDING = re.compile(r"\brow\.([A-Za-z0-9_]+)|\$\{\$index\}\.([A-Za-z0-9_]+)")
#: A route's id parameter, with or without a constraint (":id", ":id(\\d+)").
_ID_PARAM = re.compile(r":id(?:\([^)]*\))?")
_CHILD_LIST = re.compile(r"import\s+(\w*(?:List|Table))\s+from\s+'(\./[^']+\.vue)'")
_MODAL_COMPONENT = re.compile(r"(?:Form|Dialog|Modal|Assign|SelectDialog|Select)$")
_NUMERIC_OR_BOOL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|true|false", re.I)
_EXTENDED_VOID = frozenset({"input", "br", "hr", "img", "meta", "link", "wbr", "area",
                            "base", "col", "embed", "param", "source", "track"})


@dataclass(frozen=True)
class ListView:
    #: The API fields the list's columns show, in order.
    columns: tuple[str, ...] = ()
    #: field -> dictionary type its column renders through.
    dicts: dict = field(default_factory=dict)
    #: The endpoint the page reads its records from ("/bpm/process-instance/my-page").
    endpoint: str = ""
    #: Literal query arguments that the page supplies to that endpoint.
    fixed_params: tuple[tuple[str, object], ...] = ()
    #: Only unique literal prop/label bindings from this list component.
    column_labels: tuple[tuple[str, str], ...] = ()
    #: field -> the unit its column's label states, where the column shows the value as read.
    column_units: tuple[tuple[str, str], ...] = ()
    #: (column_label, arithmetic_expression) for template columns computing multi-field formulas.
    derived_columns: tuple[tuple[str, str], ...] = ()
    #: Literal single-choice search options and native dictionary bindings.
    query_choices: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    query_dicts: tuple[tuple[str, str], ...] = ()
    #: Full page vocabulary compiled from generic Vue/TypeScript page rules.
    all_column_labels: tuple[str, ...] = ()
    query_labels: tuple[str, ...] = ()
    query_option_labels: tuple[str, ...] = ()
    page_dicts: tuple[str, ...] = ()
    tab_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class UiSource:
    #: Keyed by the menu's component ("crm/contract/index").
    views: dict = field(default_factory=dict)
    #: List page path ("/crm/contract") -> a record's page ("/crm/contract/detail/{id}").
    records: dict = field(default_factory=dict)
    #: A page file under views ("erp/sale/order/components/SaleOrderItemForm") -> the row
    #: fields its tables show, by the label of their column.
    tables: dict = field(default_factory=dict)
    #: Record route template -> static tab labels from its deployed Vue component.
    record_sections: dict = field(default_factory=dict)
    #: Table component -> literal row-field dictionary bindings; empty means unresolved.
    table_dicts: dict = field(default_factory=dict)
    #: Non-list or detail/config views keyed by component ("crm/customer/detail/index").
    detail_views: dict = field(default_factory=dict)
    #: List page path -> component keys whose remaining.ts meta.activeMenu points there.
    active_menu_views: dict = field(default_factory=dict)
    #: Normalized remaining.ts route template -> component key ("crm/customer/detail/index").
    route_views: dict = field(default_factory=dict)
    root: Path | None = None

    def page_vocabulary(
        self,
        component: str = "",
        *,
        route: str = "",
        dict_labels: dict | None = None,
        dict_names: dict | None = None,
    ) -> tuple[str, ...]:
        """Collect all UI vocabulary labels observed on a component and its active-menu/detail views."""
        clean_route = str(route or "").split("#", 1)[0].split("?", 1)[0].rstrip("/")
        comp_keys: list[str] = []
        if component:
            comp_keys.append(component)
            if "/" in component:
                idx_key = component.rsplit("/", 1)[0] + "/index"
                if idx_key not in comp_keys:
                    comp_keys.append(idx_key)
        if clean_route:
            for extra_key in self.active_menu_views.get(clean_route, ()):
                if extra_key not in comp_keys:
                    comp_keys.append(extra_key)
            matched_comp = _match_route_component(self.route_views, clean_route)
            if matched_comp and matched_comp not in comp_keys:
                comp_keys.append(matched_comp)

        words: list[str] = []
        seen: set[str] = set()
        dict_keys: list[str] = []
        seen_dicts: set[str] = set()

        def _add_word(raw: str) -> None:
            cleaned = str(raw or "").strip().rstrip(":：").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                words.append(cleaned)

        def _add_dict(dict_type: str) -> None:
            dt = str(dict_type or "").strip()
            if dt and dt not in seen_dicts:
                seen_dicts.add(dt)
                dict_keys.append(dt)

        for key in comp_keys:
            view = self.views.get(key) or self.detail_views.get(key)
            if view is None:
                continue
            for lbl in getattr(view, "all_column_labels", ()):
                _add_word(lbl)
            for _, lbl in getattr(view, "column_labels", ()):
                _add_word(lbl)
            for lbl, _ in getattr(view, "derived_columns", ()):
                _add_word(lbl)
            for lbl in getattr(view, "query_labels", ()):
                _add_word(lbl)
            for lbl in getattr(view, "query_option_labels", ()):
                _add_word(lbl)
            for lbl in getattr(view, "tab_labels", ()):
                _add_word(lbl)
            for _, opts in getattr(view, "query_choices", ()):
                for _, opt_lbl in opts:
                    _add_word(opt_lbl)
            for dt in getattr(view, "page_dicts", ()):
                _add_dict(dt)
            for dt in getattr(view, "dicts", {}).values():
                _add_dict(dt)
            for _, dt in getattr(view, "query_dicts", ()):
                _add_dict(dt)

        if clean_route:
            rec_template = self.records.get(clean_route, "")
            for sec in self.record_sections.get(rec_template, ()):
                _add_word(sec)
            if self.record_sections.get(clean_route):
                for sec in self.record_sections[clean_route]:
                    _add_word(sec)

        if dict_names:
            for dt in dict_keys:
                name = dict_names.get(dt)
                if name:
                    _add_word(name)
        if dict_labels:
            for dt in dict_keys:
                for item in dict_labels.get(dt) or ():
                    if isinstance(item, (tuple, list)) and len(item) >= 2:
                        _add_word(item[1])
                    elif isinstance(item, str):
                        _add_word(item)
        return tuple(words)

    def line_labels(self, component_dir: str) -> dict:
        """The labels the tables of a page's own files give row fields (`productPrice` ->
        产品单价), the first file's first."""
        out: dict = {}
        for path in sorted(self.tables):
            if path == component_dir or path.startswith(component_dir + "/"):
                for key, label in self.tables[path].items():
                    out.setdefault(key, label)
        return out

    def line_dicts(self, component_dir: str) -> dict:
        """Only unambiguous literal bindings within this page's own table components."""
        found: dict[str, set[str]] = {}
        for path, fields in self.table_dicts.items():
            if path == component_dir or path.startswith(component_dir + "/"):
                for key, dictionary in fields.items():
                    found.setdefault(key, set()).add(dictionary)
        return {key: next(iter(names)) for key, names in found.items()
                if len(names) == 1 and "" not in names}

    @staticmethod
    def none() -> "UiSource":
        return UiSource()

    @staticmethod
    def read(root) -> "UiSource":
        src = Path(root) / "src"
        dictionary = (src / "utils" / "dict.ts").read_text(encoding="utf-8")
        consts = dict(_DICT_CONST.findall(dictionary))
        known_dict_values = frozenset(consts.values())
        views, detail_views, tables, table_dicts = {}, {}, {}, {}
        vocab_cache: dict[Path, _PageVocab] = {}
        views_root = (src / "views").resolve()

        def _compile_list_view(path: Path) -> tuple[ListView, bool]:
            text = path.read_text(encoding="utf-8")
            columns = tuple(dict.fromkeys(_COLUMN.findall(text)))
            labels = _literal_column_labels(text)
            units = _shown_units(text)
            derived = _derived_columns(text)
            dicts = {row: consts[name] for name, row in _DICT_TAG.findall(text) if name in consts}
            endpoint, fixed_params = _list_endpoint(src, text)
            query_choices, query_dicts = _query_choices(text, dictionary)
            if not endpoint:
                # A wrapper may hold one record-list component (a tab's table). More
                # than one is ambiguous, so leave those pages to their own adapter.
                children = _CHILD_LIST.findall(text)
                if len(children) == 1:
                    child = (path.parent / children[0][1]).resolve()
                    if child.is_relative_to(views_root) and child.is_file():
                        child_text = child.read_text(encoding="utf-8")
                        endpoint, fixed_params = _list_endpoint(src, child_text)
                        if endpoint:
                            query_choices, query_dicts = _query_choices(child_text, dictionary)
                            columns = tuple(dict.fromkeys(_COLUMN.findall(child_text))) or columns
                            labels = _literal_column_labels(child_text)
                            units = _shown_units(child_text)
                            derived = _derived_columns(child_text)
                            dicts = {row: consts[name] for name, row in _DICT_TAG.findall(child_text)
                                     if name in consts} or dicts
            vocab = _extract_page_vocab(src, path, consts, known_dict_values, cache=vocab_cache)
            view = ListView(
                columns=columns,
                dicts=dicts,
                endpoint=endpoint,
                fixed_params=fixed_params,
                column_labels=labels,
                column_units=units,
                derived_columns=derived,
                query_choices=query_choices,
                query_dicts=query_dicts,
                all_column_labels=vocab.column_labels,
                query_labels=vocab.query_labels,
                query_option_labels=vocab.query_option_labels,
                page_dicts=vocab.page_dicts,
                tab_labels=vocab.tab_labels,
            )
            has_core = bool(columns or dicts or endpoint)
            return view, has_core

        for path in sorted((src / "views").rglob("*.vue")):
            text = path.read_text(encoding="utf-8")
            columns = _row_columns(text)
            key = path.relative_to(src / "views").with_suffix("").as_posix()
            if columns:
                tables[key] = columns
            dictionaries = _row_dictionaries(text, consts)
            if dictionaries:
                table_dicts[key] = dictionaries
        for path in sorted((src / "views").rglob("index.vue")):
            key = path.relative_to(src / "views").with_suffix("").as_posix()
            view, has_core = _compile_list_view(path)
            if has_core:
                views[key] = view
            elif (view.all_column_labels or view.query_labels or view.query_option_labels
                  or view.page_dicts or view.tab_labels):
                detail_views[key] = view
        router = (src / "router" / "modules" / "remaining.ts").read_text(encoding="utf-8")
        route_views, active_menu_views = _router_view_bindings(src, router)
        for comp_key in (*route_views.values(), *(k for ks in active_menu_views.values() for k in ks)):
            if comp_key in views or comp_key in detail_views:
                continue
            comp_path = (src / "views" / f"{comp_key}.vue").resolve()
            if not comp_path.is_relative_to(views_root) or not comp_path.is_file():
                continue
            view, _ = _compile_list_view(comp_path)
            if (view.all_column_labels or view.query_labels or view.query_option_labels
                    or view.page_dicts or view.tab_labels):
                detail_views[comp_key] = view
        return UiSource(
            views=views,
            records=_record_routes(router),
            tables=tables,
            record_sections=_record_sections(src, router),
            table_dicts=table_dicts,
            detail_views=detail_views,
            active_menu_views=active_menu_views,
            route_views=route_views,
            root=Path(root).resolve(),
        )


def _query_choices(text: str, dictionary: str):
    # Reuse the form compiler; only one literal queryParams form owns these choices.
    from ..writes.source_forms import _Template, _walk, _select_choices
    parser = _Template()
    try:
        parser.feed(text); parser.close()
    except (ValueError, RecursionError):
        return (), ()
    templates = [n for n in parser.roots if n.tag == "template"]
    if not parser.valid or parser.stack or len(templates) != 1:
        return (), ()
    forms = [n for n, _ in _walk(templates[0]) if n.tag == "el-form" and n.attrs.get(":model") == "queryParams"]
    if len(forms) != 1:
        return (), ()
    choices, dicts, seen = {}, {}, set()
    for item, _ in _walk(forms[0]):
        key = item.attrs.get("prop")
        if item.tag != "el-form-item" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key or ""):
            continue
        if key in seen:
            choices.pop(key, None); dicts.pop(key, None)
            continue
        seen.add(key)
        controls = [n for n, _ in _walk(item) if n.tag == "el-select"]
        if len(controls) != 1 or controls[0].attrs.get("v-model") != "queryParams." + key:
            continue
        compiled = _select_choices(controls[0], text, dictionary)
        if compiled and compiled[1] == "literal":
            choices[key] = compiled[0]
        elif compiled and compiled[1] == "dictionary":
            dicts[key] = compiled[2]
    return tuple(choices.items()), tuple(dicts.items())


def _row_dictionaries(page: str, consts: dict) -> dict:
    found: dict[str, set[str]] = {}
    for attrs, body in _TABLE_COLUMN.findall(page):
        if _COLUMN_LABEL.search(attrs) is None:
            continue
        for name, row in _DICT_TAG.findall(body or ""):
            found.setdefault(row, set()).add(consts.get(name, ""))
    return {key: next(iter(names)) if len(names) == 1 else ""
            for key, names in found.items()}


_ROW_FIELD_REFS = re.compile(r"\brow\.([A-Za-z0-9_]+)\b")
_ARITH_EXPR = re.compile(
    r"(?:scope\.)?row\.([A-Za-z0-9_]+)\s*([+\-])\s*(?:scope\.)?row\.([A-Za-z0-9_]+)"
)


def _body_renders_other_fields(prop_name: str, body: str) -> bool:
    refs = set(_ROW_FIELD_REFS.findall(body or ""))
    return bool(refs and refs != {prop_name})


def _derived_columns(page: str) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for attrs, body in _TABLE_COLUMN.findall(page):
        label = re.search(r'(?<![:\w-])label="([^"{}]+)"', attrs)
        if not label or re.search(r'(?:v-bind|:label)\s*[=:]', attrs):
            continue
        m = _ARITH_EXPR.search(body or "")
        if m is None:
            continue
        lbl = label[1].strip()
        expr = f"{m.group(1)} {m.group(2)} {m.group(3)}"
        if lbl and lbl not in seen:
            seen.add(lbl)
            out.append((lbl, expr))
    return tuple(out)


def _literal_column_labels(page: str) -> tuple[tuple[str, str], ...]:
    found = {}
    for attrs, body in _TABLE_COLUMN.findall(page):
        prop = _COLUMN_PROP.search(attrs)
        label = re.search(r'(?<![:\w-])label="([^"{}]+)"', attrs)
        if prop and label and not re.search(r'(?:v-bind|:prop|:label)\s*[=:]', attrs):
            if _body_renders_other_fields(prop[1], body or ""):
                continue
            found.setdefault(prop[1], set()).add(label[1].strip())
    unique = {key:next(iter(labels)) for key,labels in found.items() if len(labels) == 1}
    return tuple((key,label) for key,label in unique.items() if label
                 and sum(label == other for other in unique.values()) == 1)


_LABEL_UNIT = re.compile(r"[（(]\s*(元|分)\s*[）)]\s*$")
_CONVERTS = re.compile(r"fen\s*to\s*yuan|fen2yuan|yuan\s*to\s*fen|/\s*100\b|\*\s*100\b", re.I)


def _shown_units(page: str) -> tuple[tuple[str, str], ...]:
    """Field -> the unit its column's label states, where the page shows the value as read.

    A label says what unit the number on the page is in. That is the unit of the value the
    API returned only when the column shows it unchanged: a column with its own body, or a
    formatter that converts between units, shows something else and says nothing here. A
    field two columns label differently says nothing either."""
    found = {}
    for attrs, body in _TABLE_COLUMN.findall(page):
        prop = _COLUMN_PROP.search(attrs)
        label = re.search(r'(?<![:\w-])label="([^"{}]+)"', attrs)
        if not prop or not label or re.search(r'(?:v-bind|:prop|:label)\s*[=:]', attrs):
            continue
        if _body_renders_other_fields(prop[1], body or ""):
            continue
        unit = _LABEL_UNIT.search(label[1].strip())
        shown_as_read = not (body or "").strip() and not _CONVERTS.search(attrs)
        found.setdefault(prop[1], set()).add(unit[1] if unit and shown_as_read else "")
    return tuple((key, next(iter(units))) for key, units in found.items()
                 if len(units) == 1 and "" not in units)


def _row_columns(page: str) -> dict:
    """Row field -> column label, for the table columns of a page whose bodies bind a row."""
    out: dict = {}
    for attrs, body in _TABLE_COLUMN.findall(page):
        label = _COLUMN_LABEL.search(attrs)
        if label is None:
            continue
        prop = _COLUMN_PROP.search(attrs)
        bound = _ROW_BINDING.search(body or "")
        key = (bound.group(1) or bound.group(2)) if bound else (prop.group(1) if prop else "")
        if key and "row." in (body or "") + attrs or (bound is not None):
            out.setdefault(key, label.group(1))
    return out


def _list_endpoint(src: Path, page: str) -> tuple[str, tuple[tuple[str, object], ...]]:
    """The URL of the API function a page's `getList` awaits, read from the module the
    page imports it from; empty when any step is not written that way. Literal query
    arguments supplied by the page are carried with that endpoint."""
    loaders = list(_GET_LIST.finditer(page))
    if not loaders:
        loaders = [m for m in _LOAD_LIST.finditer(page)
                   if re.search(r"\b\w*(?:list|records|slips)\.value\s*=", m.group(3), re.I)]
    # A page may also load categories or favourites. Its main list assignment is
    # the evidence for which loader corresponds to the table.
    loaders.sort(key=lambda m: ("list.value" not in m.group(3),
                                (m.group(1) or m.group(2)) != "getList"))

    def url(module: str | None, function: str) -> str:
        # A function imported by name (`import { getSpuPage } from ...`), or a namespace's.
        imported = re.search(_API_IMPORT.format(name=re.escape(module or function)), page)
        if imported is None:
            return ""
        for candidate in (src / "api" / f"{imported.group(1)}.ts",
                          src / "api" / imported.group(1) / "index.ts"):
            if not candidate.is_file():
                continue
            source = candidate.read_text(encoding="utf-8")
            # Keep the lookup inside this function: another GET in the file must not
            # turn an unsupported page into a guessed destination.
            start = re.search(
                rf"^\s*(?:(?:export\s+)?(?:const\s+|(?:async\s+)?function\s+))?"
                rf"{re.escape(function)}\s*(?:=\s*(?:async\s*)?|:\s*(?:async\s*)?)?\([^)]*\)",
                source, re.M)
            if start is None:
                return ""
            end = re.search(r"^}\s*,?\s*$", source[start.end():], re.M)
            body = source[start.end():start.end() + end.start()] if end else source[start.end():start.end() + 800]
            found = _API_GET.search(body)
            return found.group(1) if found else ""
        return ""

    found_urls = []
    for loader in loaders:
        body = loader.group(3)
        # Calls in Promise.all and scene-dependent function choices are not themselves
        # awaited; the named API references still identify their documented endpoint.
        calls = list(dict.fromkeys(_API_REFERENCE.findall(body)))
        calls += [call.groups() for call in _API_CALL.finditer(body)
                  if call.groups() not in calls and call.group(1) != "Promise"]
        for module, function in calls:
            found = url(module, function)
            if found and re.search(r"[/-](?:page|list|tree)$", found):
                argument = re.search(rf"\b{re.escape(module + '.' if module else '')}"
                                     rf"{re.escape(function)}\s*\(\s*\{{([^}}]*)\}}", body)
                fixed = []
                if argument:
                    for key, value in re.findall(r"\b([A-Za-z]\w*)\s*:\s*(true|false|\d+|'[^']*')",
                                                 argument.group(1)):
                        fixed.append((key, int(value) if value.isdigit() else
                                      value == "true" if value in ("true", "false") else value[1:-1]))
                found_urls.append((found, tuple(fixed)))
    if not found_urls:
        return "", ()
    # A simple-list usually supplies a selector beside a page's main records. When
    # another documented list is present, prefer it; otherwise keep the simple-list.
    return max(enumerate(found_urls), key=lambda item: (
        not item[1][0].endswith("simple-list"), -item[0]))[1]


def _record_routes(text: str) -> dict:
    """Hidden `.../detail/:id` routes by the list page they belong to (`activeMenu`),
    under the absolute path of the route that holds them."""
    out = {}
    parent = ""
    for match in _ROUTE_PATH.finditer(text):
        path = match.group(1)
        if path.startswith("/"):
            parent = path.rstrip("/")
            continue
        if not re.search(r"(^|/)detail/:id\b", path):
            continue
        tail = text[match.end():match.end() + 400]
        menu = _ACTIVE_MENU.search(tail.split("path:", 1)[0])
        if menu is None:
            continue
        template = parent + "/" + _ID_PARAM.sub("{id}", path)
        out.setdefault(menu.group(1), template)
    return out


__all__ = ["ListView", "UiSource"]


class _LiteralTabs(HTMLParser):
    """Literal labels are location hints; live DOM still verifies visibility and arrival."""
    def __init__(self):
        super().__init__()
        self.labels = []

    def handle_starttag(self, tag, attrs):
        if tag != "el-tab-pane":
            return
        attrs = dict(attrs)
        label = attrs.get("label", "")
        if label and len(label) <= 120 and not any(k in attrs for k in (":label", "v-bind:label", "v-for")):
            if label not in self.labels and len(self.labels) < 16:
                self.labels.append(label)


def _record_sections(src: Path, router: str) -> dict:
    routes = _record_routes(router)
    out = {}
    for match in _ROUTE_PATH.finditer(router):
        if not re.search(r"(^|/)detail/:id\b", match.group(1)):
            continue
        tail = router[match.end():match.end() + 400].split("path:", 1)[0]
        menu = _ACTIVE_MENU.search(tail)
        component = re.search(r"component:\s*\(\)\s*=>\s*import\(['\"]@/views/([^'\"]+\.vue)['\"]\)", tail)
        if menu is None or component is None or menu.group(1) not in routes:
            continue
        path = (src / "views" / component.group(1)).resolve()
        if not path.is_relative_to((src / "views").resolve()) or not path.is_file():
            continue
        parser = _LiteralTabs()
        parser.feed(path.read_text(encoding="utf-8"))
        if parser.labels:
            out[routes[menu.group(1)]] = tuple(parser.labels)
    return out


@dataclass(frozen=True)
class _PageVocab:
    column_labels: tuple[str, ...] = ()
    query_labels: tuple[str, ...] = ()
    query_option_labels: tuple[str, ...] = ()
    page_dicts: tuple[str, ...] = ()
    tab_labels: tuple[str, ...] = ()


@dataclass
class _HtmlNode:
    tag: str
    attrs: dict[str, str | None]
    children: list[_HtmlNode] = field(default_factory=list)
    text: str = ""


class _VocabTemplateParser(HTMLParser):
    """Tolerant Vue template parser that preserves `<template>` and `<script>` blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.roots: list[_HtmlNode] = []
        self.stack: list[_HtmlNode] = []

    def handle_starttag(self, tag, attrs):
        if self.stack and self.stack[-1].tag in {"script", "style"}:
            return
        node = _HtmlNode(tag, dict(attrs))
        (self.stack[-1].children if self.stack else self.roots).append(node)
        if tag not in _EXTENDED_VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self.stack and self.stack[-1].tag in {"script", "style"}:
            return
        node = _HtmlNode(tag, dict(attrs))
        (self.stack[-1].children if self.stack else self.roots).append(node)

    def handle_endtag(self, tag):
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].text += data


def _clean_ui_label(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = " ".join(raw.split()).strip().rstrip(":：").strip()
    if not cleaned or "{{" in cleaned or "}}" in cleaned:
        return ""
    return cleaned


def _node_plain_text(node: _HtmlNode) -> str:
    parts = [node.text] if node.text else []
    for child in node.children:
        if child.tag not in {"script", "style"}:
            parts.append(_node_plain_text(child))
    return _clean_ui_label(" ".join(parts))


def _walk_html(node: _HtmlNode):
    yield node
    for child in node.children:
        yield from _walk_html(child)


def _walk_non_modal(node: _HtmlNode):
    if node.tag in {"el-dialog", "dialog", "el-drawer"}:
        return
    yield node
    for child in node.children:
        yield from _walk_non_modal(child)


def _is_query_form(form_node: _HtmlNode) -> bool:
    model = str(form_node.attrs.get(":model") or form_node.attrs.get("model") or "")
    ref = str(form_node.attrs.get("ref") or form_node.attrs.get(":ref") or "")
    if re.search(r"query|search", f"{model} {ref}", re.I):
        return True
    if model == "formData" or ref == "formRef":
        return False
    return any(
        re.search(r"\b(?:handleQuery|resetQuery|getList)\b", str(val or ""))
        for desc in _walk_html(form_node)
        for val in desc.attrs.values()
    )


def _strip_ts_comments(source: str) -> str:
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch in {"'", '"', "`"}:
            quote = ch
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(source[i:j])
            i = j
        elif ch == "/" and i + 1 < n and source[i + 1] == "/":
            end = source.find("\n", i + 2)
            i = n if end == -1 else end
        elif ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            out.append(" ")
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _find_balanced(source: str, start_idx: int, open_ch: str, close_ch: str) -> int:
    """Return index of matching `close_ch` for `source[start_idx] == open_ch`, or -1."""
    depth, i, n = 0, start_idx, len(source)
    while i < n:
        ch = source[i]
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _extract_array_labels(body: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\blabel\s*:\s*(['\"])([^'\"\n]{1,120})\1", body):
        lbl = _clean_ui_label(match.group(2))
        if lbl and lbl not in seen:
            seen.add(lbl)
            found.append(lbl)
    return tuple(found)


def _extract_ts_option_arrays(source: str) -> dict[str, tuple[str, ...]]:
    clean = _strip_ts_comments(source)
    arrays: dict[str, tuple[str, ...]] = {}
    decl = re.compile(
        r"(?:^|[;\n])\s*(?:export\s+)?(?:const|let)\s+([A-Za-z_]\w*)\b[^=\n]*=\s*"
        r"(?:(?:ref|reactive|computed)\s*(?:<[^>\n]*>)?\s*\(\s*)?\["
    )
    for match in decl.finditer(clean):
        bracket_pos = clean.find("[", match.start())
        end_pos = _find_balanced(clean, bracket_pos, "[", "]")
        if end_pos == -1:
            continue
        labels = _extract_array_labels(clean[bracket_pos:end_pos + 1])
        if labels:
            arrays[match.group(1)] = labels
    return arrays


def _resolve_ts_module(src: Path, base_dir: Path, spec: str) -> Path | None:
    if spec.startswith("@/"):
        target = (src / spec[2:]).resolve()
    elif spec.startswith("."):
        target = (base_dir / spec).resolve()
    else:
        return None
    if not target.is_relative_to(src.resolve()):
        return None
    for candidate in (
        target,
        Path(f"{target}.ts"),
        Path(f"{target}.js"),
        target / "index.ts",
        target / "index.js",
    ):
        if candidate.is_file():
            return candidate
    return None


def _resolve_vue_module(src: Path, base_dir: Path, spec: str) -> Path | None:
    views_root = (src / "views").resolve()
    if spec.startswith("@/views/"):
        target = (src / spec[2:]).resolve()
    elif spec.startswith("."):
        target = (base_dir / spec).resolve()
    else:
        return None
    if not target.is_relative_to(views_root):
        return None
    for candidate in (target, target.with_suffix(".vue"), target / "index.vue"):
        if candidate.is_file():
            return candidate
    return None


def _parse_barrel_vue_exports(src: Path, index_path: Path) -> dict[str, Path]:
    clean = _strip_ts_comments(index_path.read_text(encoding="utf-8"))
    views_root = (src / "views").resolve()
    exports: dict[str, Path] = {}
    re_named = re.compile(r"export\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]")
    for names_raw, mod in re_named.findall(clean):
        resolved = _resolve_vue_module(src, index_path.parent, mod)
        if resolved is None or not resolved.is_relative_to(views_root):
            continue
        for part in names_raw.split(","):
            piece = part.strip()
            if not piece:
                continue
            alias = piece.split(" as ")[-1].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", alias):
                exports[alias] = resolved
    return exports


def _depth1_entries(array_body: str) -> list[str]:
    entries: list[str] = []
    i, n = 0, len(array_body)
    while i < n:
        ch = array_body[i]
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            while i < n:
                if array_body[i] == "\\":
                    i += 2
                    continue
                if array_body[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            end = _find_balanced(array_body, i, "{", "}")
            if end == -1:
                break
            entries.append(array_body[i:end + 1])
            i = end + 1
            continue
        i += 1
    return entries


def _depth1_fields(entry_text: str) -> dict[str, str]:
    """Extract only top-level `key: value` segments of one object literal."""
    inner = entry_text.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    fields: dict[str, str] = {}
    i, n = 0, len(inner)
    seg_start = 0
    depth_brace = depth_brack = depth_paren = 0

    def _commit(seg: str) -> None:
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*:\s*([\s\S]+?)\s*$", seg)
        if m:
            fields[m.group(1)] = m.group(2)

    while i < n:
        ch = inner[i]
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            while i < n:
                if inner[i] == "\\":
                    i += 2
                    continue
                if inner[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "," and depth_brace == depth_brack == depth_paren == 0:
            _commit(inner[seg_start:i])
            seg_start = i + 1
        i += 1
    _commit(inner[seg_start:])
    return fields


def _extract_crud_schema_vocab(
    src: Path,
    data_path: Path,
    consts: dict[str, str],
    known_dict_values: frozenset[str],
) -> _PageVocab:
    raw = data_path.read_text(encoding="utf-8")
    clean = _strip_ts_comments(raw)
    option_arrays = dict(_extract_ts_option_arrays(raw))
    for names_raw, spec in re.findall(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]", clean):
        if spec.endswith("/dict") or spec.startswith("@/api/"):
            continue
        mod = _resolve_ts_module(src, data_path.parent, spec)
        if mod is not None and mod.is_file():
            mod_arrays = _extract_ts_option_arrays(mod.read_text(encoding="utf-8"))
            for part in names_raw.split(","):
                orig, _, alias = part.strip().partition(" as ")
                orig_name = orig.strip()
                alias_name = (alias or orig).strip()
                if orig_name in mod_arrays:
                    option_arrays[alias_name] = mod_arrays[orig_name]

    col_labels: list[str] = []
    q_labels: list[str] = []
    q_opts: list[str] = []
    p_dicts: list[str] = []

    schema_starts = [
        m.end() - 1
        for m in re.finditer(
            r"(?:useCrudSchemas\s*\(\s*(?:reactive\s*\()?|(?:\bexport\s+)?\b(?:const|let)\s+\w*Schemas?\b[^=]*=\s*(?:reactive\s*(?:<[^>\n]*>)?\s*\()?)\[",
            clean,
        )
    ]
    for start in schema_starts:
        end = _find_balanced(clean, start, "[", "]")
        if end == -1:
            continue
        for entry in _depth1_entries(clean[start + 1:end]):
            fields = _depth1_fields(entry)
            raw_lbl = fields.get("label") or fields.get("title") or ""
            m_lbl = re.match(r"^(['\"])([^'\"]+)\1$", raw_lbl.strip())
            lbl = _clean_ui_label(m_lbl.group(2)) if m_lbl else ""
            is_table = fields.get("isTable", "").strip() != "false"
            is_search = fields.get("isSearch", "").strip() == "true"
            is_detail = fields.get("isDetail", "").strip() == "true"
            if lbl and (is_table or is_detail):
                col_labels.append(lbl)
            if lbl and is_search:
                q_labels.append(lbl)
            dict_expr = fields.get("dictType", "")
            for const_name in re.findall(r"\bDICT_TYPE\.([A-Z0-9_]+)\b", entry):
                if const_name in consts:
                    p_dicts.append(consts[const_name])
            m_lit_dict = re.match(r"^(['\"])([a-z0-9_]+)\1$", dict_expr.strip())
            if m_lit_dict and m_lit_dict.group(2) in known_dict_values:
                p_dicts.append(m_lit_dict.group(2))
            if is_search:
                search_blk = fields.get("search", "")
                for opt_match in re.finditer(r"\boptions\s*:\s*([A-Za-z_]\w*|\[)", search_blk):
                    tok = opt_match.group(1)
                    if tok == "[":
                        opt_end = _find_balanced(search_blk, opt_match.end() - 1, "[", "]")
                        if opt_end != -1:
                            q_opts.extend(_extract_array_labels(search_blk[opt_match.end() - 1:opt_end + 1]))
                    elif tok in option_arrays:
                        q_opts.extend(option_arrays[tok])

    return _PageVocab(
        column_labels=tuple(dict.fromkeys(col_labels)),
        query_labels=tuple(dict.fromkeys(q_labels)),
        query_option_labels=tuple(dict.fromkeys(q_opts)),
        page_dicts=tuple(dict.fromkeys(p_dicts)),
    )


def _extract_page_vocab(
    src: Path,
    path: Path,
    consts: dict[str, str],
    known_dict_values: frozenset[str],
    *,
    cache: dict[Path, _PageVocab] | None = None,
    visiting: set[Path] | None = None,
    depth: int = 0,
) -> _PageVocab:
    resolved = path.resolve()
    if cache is not None and resolved in cache:
        return cache[resolved]
    seen_stack = visiting if visiting is not None else set()
    if resolved in seen_stack or depth > 4:
        return _PageVocab()
    seen_stack.add(resolved)
    try:
        text = resolved.read_text(encoding="utf-8")
        parser = _VocabTemplateParser()
        parser.feed(text)
        parser.close()

        templates = [n for n in parser.roots if n.tag == "template"]
        if not templates:
            templates = [n for n in parser.roots if n.tag not in {"script", "style"}]
        scripts = "\n".join(n.text for n in parser.roots if n.tag == "script")
        clean_script = _strip_ts_comments(scripts)

        local_arrays = dict(_extract_ts_option_arrays(scripts))
        namespace_arrays: dict[str, dict[str, tuple[str, ...]]] = {}
        child_vues: list[Path] = []
        crud_vocabs: list[_PageVocab] = []

        for spec in re.findall(r"import\s+[A-Za-z_]\w*\s+from\s+['\"]([^'\"]+)['\"]", clean_script):
            child = _resolve_vue_module(src, resolved.parent, spec)
            if child is not None and _MODAL_COMPONENT.search(child.stem) is None:
                child_vues.append(child)

        for names_raw, spec in re.findall(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]", clean_script):
            if spec.endswith(".data") or spec.endswith(".data.ts"):
                data_path = _resolve_ts_module(src, resolved.parent, spec)
                if data_path is not None:
                    crud_vocabs.append(_extract_crud_schema_vocab(src, data_path, consts, known_dict_values))
                continue
            ts_mod = _resolve_ts_module(src, resolved.parent, spec)
            if ts_mod is not None and ts_mod.name in {"index.ts", "index.js"}:
                barrel_map = _parse_barrel_vue_exports(src, ts_mod)
                for part in names_raw.split(","):
                    orig, _, _alias = part.strip().partition(" as ")
                    name = orig.strip()
                    child = barrel_map.get(name)
                    if child is not None and _MODAL_COMPONENT.search(child.stem) is None:
                        child_vues.append(child)
                continue
            if ts_mod is not None and not spec.startswith("@/api/") and not spec.endswith("/dict"):
                mod_arrays = _extract_ts_option_arrays(ts_mod.read_text(encoding="utf-8"))
                for part in names_raw.split(","):
                    orig, _, alias = part.strip().partition(" as ")
                    orig_name = orig.strip()
                    alias_name = (alias or orig).strip()
                    if orig_name in mod_arrays:
                        local_arrays[alias_name] = mod_arrays[orig_name]

        for ns_name, spec in re.findall(r"import\s+\*\s+as\s+([A-Za-z_]\w*)\s+from\s+['\"]([^'\"]+)['\"]", clean_script):
            if spec.startswith("@/api/") or spec.endswith("/dict"):
                continue
            ts_mod = _resolve_ts_module(src, resolved.parent, spec)
            if ts_mod is not None:
                mod_arrays = _extract_ts_option_arrays(ts_mod.read_text(encoding="utf-8"))
                if mod_arrays:
                    namespace_arrays[ns_name] = mod_arrays

        col_labels: list[str] = []
        q_labels: list[str] = []
        q_opts: list[str] = []
        p_dicts: list[str] = []
        tab_labels: list[str] = []
        referenced_option_vars: list[str] = []

        def _collect_dict_refs(expr: object) -> None:
            s = str(expr or "")
            for const_name in re.findall(r"\bDICT_TYPE\.([A-Z0-9_]+)\b", s):
                if const_name in consts:
                    p_dicts.append(consts[const_name])

        _collect_dict_refs(clean_script)

        for tmpl in templates:
            for node in _walk_non_modal(tmpl):
                attrs = node.attrs
                for val in attrs.values():
                    _collect_dict_refs(val)
                if node.tag == "dict-tag":
                    lit_type = attrs.get("type")
                    if lit_type and lit_type in known_dict_values:
                        p_dicts.append(lit_type)
                elif node.tag == "el-table-column":
                    if not any(k in attrs for k in (":label", "v-bind:label")):
                        lbl = _clean_ui_label(attrs.get("label"))
                        if lbl:
                            col_labels.append(lbl)
                elif node.tag == "el-descriptions-item":
                    if not any(k in attrs for k in (":label", "v-bind:label")):
                        lbl = _clean_ui_label(attrs.get("label"))
                        if lbl:
                            col_labels.append(lbl)
                elif node.tag == "el-tab-pane":
                    if not any(k in attrs for k in (":label", "v-bind:label", "v-for")):
                        lbl = _clean_ui_label(attrs.get("label"))
                        if lbl and len(lbl) <= 120:
                            tab_labels.append(lbl)
                elif node.tag == "el-form" and _is_query_form(node):
                    for qnode in _walk_non_modal(node):
                        qattrs = qnode.attrs
                        if qnode.tag == "el-form-item":
                            if not any(k in qattrs for k in (":label", "v-bind:label")):
                                lbl = _clean_ui_label(qattrs.get("label"))
                                if lbl:
                                    q_labels.append(lbl)
                        elif qnode.tag == "el-option":
                            v_for = str(qattrs.get("v-for") or "")
                            if v_for:
                                _collect_dict_refs(v_for)
                                m_in = re.search(r"\bin\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)", v_for)
                                if m_in:
                                    referenced_option_vars.append(m_in.group(1))
                            elif not any(k in qattrs for k in (":label", "v-bind:label")):
                                lbl = _clean_ui_label(qattrs.get("label")) or _node_plain_text(qnode)
                                if lbl:
                                    q_opts.append(lbl)
                        elif qnode.tag in {"el-radio", "el-radio-button"}:
                            v_for = str(qattrs.get("v-for") or "")
                            if v_for:
                                _collect_dict_refs(v_for)
                                m_in = re.search(r"\bin\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)", v_for)
                                if m_in:
                                    referenced_option_vars.append(m_in.group(1))
                            else:
                                txt = _node_plain_text(qnode)
                                if txt:
                                    q_opts.append(txt)
                                elif not any(k in qattrs for k in (":label", "v-bind:label")):
                                    lbl = _clean_ui_label(qattrs.get("label"))
                                    if lbl and not _NUMERIC_OR_BOOL.fullmatch(lbl):
                                        q_opts.append(lbl)
                        for vfor_attr in (qattrs.get("v-for"), qattrs.get(":options")):
                            if not vfor_attr:
                                continue
                            m_in = re.search(r"\bin\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)", str(vfor_attr))
                            if m_in:
                                referenced_option_vars.append(m_in.group(1))
                            elif re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", str(vfor_attr).strip()):
                                referenced_option_vars.append(str(vfor_attr).strip())

        for ref_var in referenced_option_vars:
            if "." in ref_var:
                ns, member = ref_var.split(".", 1)
                q_opts.extend(namespace_arrays.get(ns, {}).get(member, ()))
            elif ref_var in local_arrays:
                q_opts.extend(local_arrays[ref_var])

        for schema_vocab in crud_vocabs:
            col_labels.extend(schema_vocab.column_labels)
            q_labels.extend(schema_vocab.query_labels)
            q_opts.extend(schema_vocab.query_option_labels)
            p_dicts.extend(schema_vocab.page_dicts)

        for child_path in child_vues:
            child_vocab = _extract_page_vocab(
                src, child_path, consts, known_dict_values,
                cache=cache, visiting=seen_stack, depth=depth + 1,
            )
            col_labels.extend(child_vocab.column_labels)
            q_labels.extend(child_vocab.query_labels)
            q_opts.extend(child_vocab.query_option_labels)
            p_dicts.extend(child_vocab.page_dicts)
            tab_labels.extend(child_vocab.tab_labels)

        result = _PageVocab(
            column_labels=tuple(dict.fromkeys(col_labels)),
            query_labels=tuple(dict.fromkeys(q_labels)),
            query_option_labels=tuple(dict.fromkeys(q_opts)),
            page_dicts=tuple(dict.fromkeys(p_dicts)),
            tab_labels=tuple(dict.fromkeys(tab_labels)),
        )
        if cache is not None:
            cache[resolved] = result
        return result
    finally:
        seen_stack.discard(resolved)


def _normalize_router_path(path: str) -> str:
    cleaned = _ID_PARAM.sub("{id}", path)
    return re.sub(r":([A-Za-z_]\w*)(?:\([^)]*\))?", r"{\1}", cleaned)


def _router_view_bindings(src: Path, router: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Parse remaining.ts route objects generically into route -> component and activeMenu -> components."""
    clean = _strip_ts_comments(router)
    route_views: dict[str, str] = {}
    active_menu: dict[str, list[str]] = {}
    views_root = (src / "views").resolve()

    def _split_top_objects(array_text: str) -> list[str]:
        return _depth1_entries(array_text)

    def _join_route(parent: str, child: str) -> str:
        if child.startswith("/"):
            return child.rstrip("/") or "/"
        base = parent.rstrip("/")
        rel = child.strip("/")
        if not rel:
            return base or "/"
        return f"{base}/{rel}" if base else f"/{rel}"

    def _walk_route_obj(obj_text: str, parent_path: str) -> None:
        fields = _depth1_fields(obj_text)
        raw_path_expr = (fields.get("path") or "").strip()
        m_path = re.match(r"^(['\"])([^'\"]*)\1$", raw_path_expr)
        full_path = _join_route(parent_path, m_path.group(2)) if m_path else parent_path
        norm_path = _normalize_router_path(full_path)

        comp_expr = fields.get("component") or ""
        m_comp = re.search(r"@/views/([^'\"]+?)\.vue\b", comp_expr)
        comp_key = m_comp.group(1) if m_comp else ""
        if comp_key:
            comp_file = (src / "views" / f"{comp_key}.vue").resolve()
            if comp_file.is_relative_to(views_root) and comp_file.is_file():
                if m_path and norm_path:
                    route_views.setdefault(norm_path, comp_key)
                meta_expr = fields.get("meta") or ""
                m_active = re.search(r"\bactiveMenu\s*:\s*(['\"])([^'\"]+)\1", meta_expr)
                if m_active:
                    menu_path = m_active.group(2).rstrip("/")
                    bucket = active_menu.setdefault(menu_path, [])
                    if comp_key not in bucket:
                        bucket.append(comp_key)

        children_expr = (fields.get("children") or "").strip()
        if children_expr.startswith("["):
            for child_obj in _split_top_objects(children_expr[1:-1]):
                _walk_route_obj(child_obj, full_path)

    m_arr = re.search(r"=\s*\[", clean)
    arr_start = (m_arr.end() - 1) if m_arr else clean.find("[")
    if arr_start != -1:
        arr_end = _find_balanced(clean, arr_start, "[", "]")
        if arr_end != -1:
            for top_obj in _split_top_objects(clean[arr_start + 1:arr_end]):
                _walk_route_obj(top_obj, "")

    return route_views, {k: tuple(v) for k, v in active_menu.items()}


def _match_route_component(route_views: dict[str, str], route: str) -> str:
    if not route or not route_views:
        return ""
    if route in route_views:
        return route_views[route]
    for template, comp in route_views.items():
        if "{" not in template:
            continue
        pattern = re.sub(r"\\\{[A-Za-z_]\w*\\\}", r"[^/]+", re.escape(template))
        if re.fullmatch(pattern, route):
            return comp
    return ""
