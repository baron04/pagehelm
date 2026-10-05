"""Reviewed Vue search forms, compiled locally; current API reads bind choices.

Only explicitly installed query/persistence reviews enable this distribution path.
The compiler supplies labels and bindings, never authority or executable source.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import re
from urllib.parse import urlsplit

from web_operation_agent.business.errors import BusinessToolRequestError, BusinessToolIdentityError
from web_operation_agent.goal.page_filters import PageFilterDraft, PageFilterField
from web_operation_agent.goal.resolve.words import period
from .source_forms import _Template, _walk, _select_choices


@dataclass(frozen=True)
class NativePageFilterReview:
    release: str
    entity: str
    route: str
    component: str
    endpoint: str
    hashes: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, payload, tree, read_paths):
        keys = {"schema_version", "release", "entity", "route", "component", "endpoint", "hashes"}
        if (not isinstance(payload, dict) or set(payload) != keys or type(payload["schema_version"]) is not int
                or payload["schema_version"] != 1
                or payload["release"] != tree.release
                or any(not isinstance(payload[k], str) for k in keys - {"schema_version", "hashes"})
                or not re.fullmatch(r"/[a-z0-9/_-]+", payload["route"])
                or not re.fullmatch(r"ui/src/views/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.vue", payload["component"])
                or "/admin-api/" + payload["endpoint"] not in read_paths
                or not isinstance(payload["hashes"], dict) or payload["component"] not in payload["hashes"]
                or not 2 <= len(payload["hashes"]) <= 8):
            raise ValueError("native page filters require a named source and read review")
        review = cls(payload["release"], payload["entity"], payload["route"], payload["component"],
                     payload["endpoint"], tuple(sorted(payload["hashes"].items())))
        review.check(tree)
        return review

    def check(self, tree):
        if self.release != tree.release or any(not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or hashlib.sha256(tree.read_text(path).encode()).hexdigest() != sha
                for path, sha in self.hashes):
            raise BusinessToolRequestError("the reviewed page query source changed")


class NativePageFilters:
    def __init__(self, data, tree, reviews, today):
        self.data, self.tree, self.reviews, self.today = data, tree, tuple(reviews), today

    def prepare(self, plan, observation, *, context):
        context.require_live(application_id="yudao", required_scope="yudao.read")
        reviews = [r for r in self.reviews if r.route == plan.expected_path]
        if len(reviews) != 1:
            raise BusinessToolRequestError("this page has no reviewed query entry")
        review = reviews[0]
        review.check(self.tree)
        path = urlsplit(observation.url).path
        calendars = {"dialog:" + e.attrs["aria-controls"] for e in observation.elements
            if e.role == "combobox" and e.attrs.get("aria-haspopup") == "dialog"
            and e.attrs.get("aria-controls") and not str(e.attrs.get("form_scope", "")).startswith("dialog")}
        if (path != review.route or observation.page_route != path or observation.render_state != "ready"
                or urlsplit(observation.url).query or any(str(e.attrs.get("form_scope", "")).startswith("dialog")
                    and e.attrs["form_scope"] not in calendars for e in observation.elements)):
            raise BusinessToolRequestError("the exact page query form is not ready")
        identity = self.data._client.get("system/auth/get-permission-info")
        if (not isinstance(identity, dict) or not isinstance(identity.get("user"), dict)
                or str(identity["user"].get("id")) != context.principal.actor_id
                or str(self.data._client.tenant) != context.principal.tenant_id):
            raise BusinessToolIdentityError("the page query actor changed")
        index = self.data.index(context=context)
        page = index.page(review.route)
        entity = index.entity(review.entity)
        if page is None or page.entity != review.entity or entity is None:
            raise BusinessToolRequestError("the actor does not offer this query page")
        source = self.tree.read_text(review.component)
        parser = _Template(); parser.feed(source); parser.close()
        forms = [n for root in parser.roots if root.tag == "template" for n, _ in _walk(root)
                 if n.tag == "el-form" and n.attrs.get(":model") == "queryParams"]
        if not parser.valid or parser.stack or len(forms) != 1:
            raise BusinessToolRequestError("the reviewed query form is not uniquely compiled")
        items = {}
        for node, _ in _walk(forms[0]):
            if node.tag == "el-form-item" and node.attrs.get("prop"):
                key = node.attrs["prop"]
                if key in items:
                    raise BusinessToolRequestError("the source query field is ambiguous")
                items[key] = node
        fields, parameters = [], []
        for condition in plan.conditions:
            if not condition.values:
                raise BusinessToolRequestError("a page filter needs an explicit value")
            field = entity.field_by_label(condition.field)
            node = items.get(field.key) if field else None
            if node is None or not node.attrs.get("label") or condition.op not in {"", "=", "between"}:
                raise BusinessToolRequestError("the page does not offer this exact filter")
            controls = [n for n, _ in _walk(node) if n.tag in {"el-date-picker", "el-select", "el-input"}]
            if len(controls) != 1 or controls[0].attrs.get("v-model") != "queryParams." + field.key:
                raise BusinessToolRequestError("the source query control is not uniquely compiled")
            control = controls[0]
            if any(k.startswith(("@change", "@input", "v-on:", "v-model.", "v-bind")) for k in control.attrs):
                raise BusinessToolRequestError("the query field has unreviewed input effects")
            label = node.attrs["label"]
            if control.tag == "el-date-picker" and control.attrs.get("type") == "daterange":
                if control.attrs.get("value-format") != "YYYY-MM-DD HH:mm:ss":
                    raise BusinessToolRequestError("this date range format is not supported")
                value = "..".join(condition.values) if condition.op == "between" else condition.values[0] if len(condition.values) == 1 else ""
                dates = period(value, self.today)
                if dates is None or any(d is None for d in dates) or dates[0] >= dates[1]:
                    raise BusinessToolRequestError("a bounded calendar range is required")
                start, end = dates[0].isoformat(), (dates[1] - timedelta(days=1)).isoformat()
                fields += [PageFilterField(label, start, "combobox", name=control.attrs.get("start-placeholder", ""), commit=False),
                           PageFilterField(label, end, "combobox", name=control.attrs.get("end-placeholder", ""), commit=False)]
                parameters += [(field.key + "Start", start + " 00:00:00"),
                               (field.key + "End", end + " 23:59:59")]
            elif control.tag == "el-select" and len(condition.values) == 1 and condition.op in {"", "="}:
                dictionary = self.tree.read_text("ui/src/utils/dict.ts")
                choices = _select_choices(control, source, dictionary)
                if choices and choices[1] == "dictionary":
                    rows = self.data._client.get("system/dict-data/simple-list")
                    if not isinstance(rows, list):
                        raise BusinessToolRequestError("the actor's query choices are malformed")
                    pairs = []
                    for row in rows:
                        if not isinstance(row, dict) or row.get("dictType") != choices[2]:
                            continue
                        if (not isinstance(row.get("label"), str) or not row["label"]
                                or not re.fullmatch(r"[0-9]{1,19}", str(row.get("value", "")))):
                            raise BusinessToolRequestError("the actor's query choices are malformed")
                        pairs.append((str(int(row["value"])), row["label"]))
                elif choices and choices[1] == "literal":
                    pairs = choices[0]
                else:
                    # Fixed reviewed application read pattern, not a URL supplied by source/model.
                    options = [n for n, _ in _walk(control) if n.tag == "el-option"]
                    if (field.key != "customerId" or len(options) != 1
                            or options[0].attrs.get("v-for") != "item in customerList"
                            or options[0].attrs.get(":value") != "item.id"
                            or options[0].attrs.get(":label") != "item.name"
                            or "customerList.value = await CustomerApi.getCustomerSimpleList()" not in source
                            or not re.search(r"import \{ CustomerApi, CustomerVO \} from ['\"]@/api/erp/sale/customer['\"]", source)):
                        raise BusinessToolRequestError("this dynamic page choice is not supported")
                    rows = self.data._client.get("erp/customer/simple-list")
                    if (not isinstance(rows, list) or any(not isinstance(row, dict)
                            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(row.get("id", "")))
                            or not isinstance(row.get("name"), str) or not row["name"] for row in rows)):
                        raise BusinessToolRequestError("the actor's query choices are malformed")
                    pairs = [(str(row["id"]), row["name"]) for row in rows]
                matches = [(v,l) for v,l in pairs if condition.values[0] in (v,l)]
                if len(matches) != 1:
                    raise BusinessToolRequestError("the actor's page choice is unavailable or ambiguous")
                value, choice_label = matches[0]
                fields.append(PageFilterField(label, value, "combobox", option_label=choice_label))
                parameters.append((field.key, value))
            else:
                raise BusinessToolRequestError("this query control is not qualified")
        scopes = {(e.attrs.get("form_scope"), e.attrs.get("form_instance")) for e in observation.elements
                  if e.role == "button" and e.name == "搜索" and not str(e.attrs.get("form_scope", "")).startswith("dialog")}
        if len(scopes) != 1:
            raise BusinessToolRequestError("the search form is unavailable or ambiguous")
        scope, instance = next(iter(scopes))
        if not isinstance(scope, str) or not isinstance(instance, str) or not scope or not instance:
            raise BusinessToolRequestError("the query form has no live instance")
        return PageFilterDraft(review.route, scope, instance, tuple(fields), "搜索",
                               "/admin-api/" + review.endpoint, tuple(sorted(parameters)))
