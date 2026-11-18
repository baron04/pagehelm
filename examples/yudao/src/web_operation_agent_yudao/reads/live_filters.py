"""Page conditions set in the query form the page itself shows, without reading source.

A measured prototype for one adapter, like the live form port beside it. Each condition is compiled by the core resolver under the current actor's index into a
field key and the value the application stores. The live control is the one the page
binds to that key, a fact the Runtime observes. The proof is the page's own completed
list read: it must carry those values, by digest, and nothing else new.

Nothing here grants an action: the core's page-filter procedure dispatches the fixed
fill, select and click actions under the session's own policy and the method fence.
"""
from datetime import date, timedelta
import hashlib
import json
from urllib.parse import urlsplit

from web_operation_agent.business.errors import BusinessToolIdentityError, BusinessToolRequestError
from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.goal.data import Compare
from web_operation_agent.goal.page_filters import PageFilterDraft, PageFilterField

#: What this application's pages call the control that applies a query form.
QUERY_WORDS = ("搜索", "查询")
_PAGING_KEYS = frozenset({"pageNo", "pageSize", "每页条数", "每页", "每页显示", "页码", "第几页"})


def canonical_filter_pairs(filters) -> tuple[tuple[str, str], ...]:
    """Canonical sorted `(key, value)` pairs for monotonic empty-filter tracking (ACC-02)."""
    if filters is None:
        return ()
    if isinstance(filters, dict):
        raw_items = filters.items()
    elif isinstance(filters, (list, tuple)):
        raw_items = []
        for item in filters:
            if isinstance(item, dict) and "label" in item:
                raw_items.append((item.get("label"), item.get("value")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                raw_items.append((item[0], item[1]))
    else:
        return ()
    pairs: list[tuple[str, str]] = []
    for raw_key, raw_val in raw_items:
        key = str(raw_key or "").strip().rstrip(":：").strip()
        if not key or key in _PAGING_KEYS:
            continue
        if raw_val is None or raw_val == "" or raw_val == [] or raw_val == ():
            continue
        if isinstance(raw_val, (list, tuple, dict)):
            val = json.dumps(raw_val, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        elif isinstance(raw_val, bool):
            val = "true" if raw_val else "false"
        else:
            val = str(raw_val).strip()
            if not val:
                continue
        pairs.append((key, val))
    return tuple(sorted(set(pairs)))


def active_view_or_tab(observation) -> str:
    """Return stable active view or selected tab name(s) from an observation."""
    if observation is None:
        return ""
    active = str(getattr(observation, "active_view", "") or "").strip()
    if active:
        return active
    tabs = [
        str(e.name or "").strip().splitlines()[0]
        for e in getattr(observation, "elements", ()) or ()
        if getattr(e, "role", "") == "tab"
        and (getattr(e, "attrs", {}) or {}).get("aria-selected") == "true"
        and str(getattr(e, "name", "") or "").strip()
    ]
    if tabs:
        return "|".join(dict.fromkeys(tabs))
    selected = [str(x).strip() for x in getattr(observation, "selected_controls", ()) or () if str(x).strip()]
    return "|".join(dict.fromkeys(selected))


def control_identity(observation, element) -> dict[str, str]:
    """Stable control identity (`route`, `form_scope`, `label_or_name`) independent of volatile refs."""
    route = ""
    if observation is not None:
        route = str(getattr(observation, "page_route", "") or urlsplit(getattr(observation, "url", "") or "").path)
    if element is None:
        return {"route": route, "form_scope": "", "label_or_name": ""}
    attrs = getattr(element, "attrs", {}) or {}
    label = ""
    for key in ("field_label", "title", "placeholder"):
        val = str(attrs.get(key) or "").rstrip(":：").strip()
        if val:
            label = val[:80]
            break
    if not label:
        name = str(getattr(element, "name", "") or "").strip()
        label = (name.splitlines()[0][:80] if name else "") or str(getattr(element, "role", "") or "")
    return {
        "route": route,
        "form_scope": str(attrs.get("form_scope") or ""),
        "label_or_name": label,
    }


def _digest(*values: str) -> str:
    """As the page integration digests one query key's values."""
    text = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class LivePageFilters:
    def __init__(self, port):
        self._port = port
        #: The draft last issued for one actor, page and set of conditions. While a
        #: calendar of that same form is open it hides neighbouring controls, so the
        #: form cannot be read again in full until the range is entered.
        self._issued: dict[tuple, PageFilterDraft] = {}

    def prepare(self, plan, observation, *, context):
        context.require_live(application_id="yudao", required_scope="yudao.read")
        port, data = self._port, self._port.data
        path = urlsplit(observation.url).path
        calendars = {"dialog:" + e.attrs["aria-controls"] for e in observation.elements
                     if e.role == "combobox" and e.attrs.get("aria-haspopup") == "dialog"
                     and e.attrs.get("aria-controls")
                     and not str(e.attrs.get("form_scope", "")).startswith("dialog")}
        if (path != plan.expected_path or observation.page_route != path
                or observation.render_state != "ready" or urlsplit(observation.url).query
                or any(str(e.attrs.get("form_scope", "")).startswith("dialog")
                       and e.attrs["form_scope"] not in calendars for e in observation.elements)):
            raise BusinessToolRequestError("the page query form is not ready")
        identity = data._client.get("system/auth/get-permission-info")
        if (not isinstance(identity, dict) or not isinstance(identity.get("user"), dict)
                or str(identity["user"].get("id")) != context.principal.actor_id
                or str(data._client.tenant) != context.principal.tenant_id):
            raise BusinessToolIdentityError("the page query actor changed")
        asked = (context.principal.actor_id, context.principal.tenant_id, path, plan.conditions)
        issued = self._issued.get(asked)
        if (issued is not None and any(e.attrs.get("form_scope") in calendars for e in observation.elements)
                and any(e.attrs.get("form_scope") == issued.scope
                        and e.attrs.get("form_instance") == issued.instance for e in observation.elements)):
            return issued
        self._issued.pop(asked, None)
        index = port.index(context=context)
        page = index.page(path)
        spec = data._entities.get(page.entity) if page is not None and page.entity else None
        resolver = port._resolver(index)
        view = resolver.view(page.entity) if spec is not None else None
        if spec is None or view is None or not spec.list_path:
            raise BusinessToolRequestError("the actor does not offer this query page")

        def held(element):
            return (element.attrs.get("page_control") == "query"
                    and not str(element.attrs.get("form_scope", "")).startswith("dialog")
                    and not form_control_unavailable(element.attrs))
        forms = {(e.attrs.get("form_scope"), e.attrs.get("form_instance"), e.name)
                 for e in observation.elements if e.role == "button" and e.name in QUERY_WORDS and held(e)}
        if len(forms) != 1 or not all(isinstance(part, str) and part for part in next(iter(forms))):
            raise BusinessToolRequestError("the page shows no single query form")
        scope, instance, button = next(iter(forms))

        def bound(key):
            return [e for e in observation.elements if e.role in ("combobox", "textbox") and held(e)
                    and e.attrs.get("query_field") == key and e.attrs.get("form_scope") == scope
                    and e.attrs.get("form_instance") == instance]
        fields, ranges, reads = [], [], []
        for condition in plan.conditions:
            clauses, problem = resolver._condition(view, condition.field, condition.values, op=condition.op)
            if (not clauses or any(not isinstance(c, Compare) for c in clauses)
                    or len({c.field for c in clauses}) != 1 or clauses[0].field not in spec.params):
                raise BusinessToolRequestError("the page does not offer this exact filter")
            key = clauses[0].field
            info, controls = view.info(key), bound(key)
            labelled = [e for e in controls if e.attrs.get("field_label")]
            if info is None or len(labelled) != 1:
                raise BusinessToolRequestError("the page shows no single control for this filter")
            label = labelled[0].attrs["field_label"]
            if len(clauses) == 2 and info.kind in ("date", "datetime"):
                span = {c.op: c.value for c in clauses}
                start, end = span.get("ge"), span.get("lt")
                if (set(span) != {"ge", "lt"} or type(start) is not date or type(end) is not date
                        or start >= end or len(controls) != 2 or labelled[0] is not controls[0]
                        or any(e.role != "combobox" for e in controls)):
                    raise BusinessToolRequestError("a bounded calendar range is required")
                first, last = start.isoformat(), (end - timedelta(days=1)).isoformat()
                same = controls[0].name == controls[1].name
                # Ranges are entered last: their calendar covers what is beside it.
                ranges += [PageFilterField(label, day, "combobox", name=e.name, commit=False,
                                           ordinal=i + 1 if same else 0)
                           for i, (day, e) in enumerate(zip((first, last), controls))]
                reads += [(key + "[0]", (_digest(first + " 00:00:00"), _digest(first))),
                          (key + "[1]", (_digest(last + " 23:59:59"), _digest(last)))]
                continue
            value = clauses[0].value
            if clauses[0].op == "in" and isinstance(value, tuple) and len(value) == 1:
                value = value[0]
            elif clauses[0].op != "eq":
                value = None
            if (len(clauses) != 1 or len(controls) != 1 or isinstance(value, (bool, tuple))
                    or value is None or not str(value)):
                raise BusinessToolRequestError("the page does not offer this exact filter")
            if info.kind == "choice" and controls[0].role == "combobox":
                shown = [c.label for c in info.choices if c.key == value]
            elif info.kind == "link" and controls[0].role == "combobox" and len(condition.values) == 1:
                found = resolver._find_record(info.relation, resolver._own_name(condition.values[0]))
                shown = ([resolver._own_name(found[2])]
                         if isinstance(found, tuple) and len(found) == 3 and str(found[1]) == str(value) else [])
            elif info.kind == "text" and controls[0].role == "textbox":
                fields.append(PageFilterField(label, str(value), "textbox"))
                reads.append((key, (_digest(str(value)),)))
                continue
            else:
                shown = []
            if len(shown) != 1 or not shown[0]:
                raise BusinessToolRequestError("the actor's page choice is unavailable or ambiguous")
            fields.append(PageFilterField(label, str(value), "combobox", option_label=shown[0]))
            reads.append((key, (_digest(str(value)),)))
        if not reads or len(dict(reads)) != len(reads):
            raise BusinessToolRequestError("the page conditions are empty or repeat a field")
        if len(self._issued) >= 64:
            self._issued.clear()
        draft = self._issued[asked] = PageFilterDraft(
            path, scope, instance, tuple(fields + ranges), button,
            "/admin-api/" + spec.list_path, read_fields=tuple(sorted(reads)))
        return draft
