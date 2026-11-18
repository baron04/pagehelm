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
    #: Literal single-choice search options and native dictionary bindings.
    query_choices: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    query_dicts: tuple[tuple[str, str], ...] = ()


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
    root: Path | None = None

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
        views, tables, table_dicts = {}, {}, {}
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
            text = path.read_text(encoding="utf-8")
            columns = tuple(dict.fromkeys(_COLUMN.findall(text)))
            labels = _literal_column_labels(text)
            units = _shown_units(text)
            dicts = {row: consts[name] for name, row in _DICT_TAG.findall(text) if name in consts}
            endpoint, fixed_params = _list_endpoint(src, text)
            query_choices, query_dicts = _query_choices(text, dictionary)
            if not endpoint:
                # A wrapper may hold one record-list component (a tab's table). More
                # than one is ambiguous, so leave those pages to their own adapter.
                children = _CHILD_LIST.findall(text)
                if len(children) == 1:
                    child = (path.parent / children[0][1]).resolve()
                    if child.is_relative_to((src / "views").resolve()) and child.is_file():
                        child_text = child.read_text(encoding="utf-8")
                        endpoint, fixed_params = _list_endpoint(src, child_text)
                        if endpoint:
                            query_choices, query_dicts = _query_choices(child_text, dictionary)
                            columns = tuple(dict.fromkeys(_COLUMN.findall(child_text))) or columns
                            labels = _literal_column_labels(child_text)
                            units = _shown_units(child_text)
                            dicts = {row: consts[name] for name, row in _DICT_TAG.findall(child_text)
                                     if name in consts} or dicts
            if columns or dicts or endpoint:
                key = path.relative_to(src / "views").with_suffix("").as_posix()
                views[key] = ListView(columns=columns, dicts=dicts, endpoint=endpoint,
                                      fixed_params=fixed_params, column_labels=labels,
                                      column_units=units, query_choices=query_choices, query_dicts=query_dicts)
        router = (src / "router" / "modules" / "remaining.ts").read_text(encoding="utf-8")
        return UiSource(views=views, records=_record_routes(router), tables=tables,
                        record_sections=_record_sections(src, router), table_dicts=table_dicts,
                        root=Path(root).resolve())


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


def _literal_column_labels(page: str) -> tuple[tuple[str, str], ...]:
    found = {}
    for attrs, _body in _TABLE_COLUMN.findall(page):
        prop = _COLUMN_PROP.search(attrs)
        label = re.search(r'(?<![:\w-])label="([^"{}]+)"', attrs)
        if prop and label and not re.search(r'(?:v-bind|:prop|:label)\s*[=:]', attrs):
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
