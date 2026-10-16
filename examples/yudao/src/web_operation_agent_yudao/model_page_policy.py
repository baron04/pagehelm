"""Release-reviewed handlers for model-driven native page operations.

The method fence prevents HTTP writes. This separate application review names the
routes and handler families whose navigation, query or transient-form effects the
deployment has inspected. Neither a control label nor the fence grants a handler.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import urlsplit

from web_operation_agent.business.draft_steps import DraftStepPermit
from web_operation_agent.execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionPermit)
from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.business.forms import ApplicationFormValue
from web_operation_agent.business.errors import BusinessToolRequestError
from .bound_data_reads import BoundDataReads
from .read_actions import NativeReadActions


HANDLERS = frozenset({"navigation", "tabs", "query", "paging", "form_entry", "fields", "lines"})


def _matches_route(template, path):
    return re.fullmatch(re.escape(template).replace(r"\{id\}", r"[1-9][0-9]*"), path) is not None
_UNSAFE_BUTTON = re.compile(
    r"导出|导入|下载|批量|删除|移除|清除|重置密码|审核|审批|提交|保存|确定|确认|发布|"
    r"停用|启用|归档|同步|推送|发送|执行|复制|打印|上传|支付|退款"
)
_UNSAFE_ICON = re.compile(
    r"(?:download|upload|delete|remove|trash|check|confirm|save|printer|print|send|export|import)"
)


def _unsafe_button(element):
    labels = " ".join(str(value) for value in (element.name, element.attrs.get("title"),
        element.attrs.get("aria-label"), element.attrs.get("action_name")) if value)
    icon = str(element.attrs.get("action_icon") or "")
    return bool(_UNSAFE_BUTTON.search(labels) or _UNSAFE_ICON.search(icon)
                or not labels.strip() and icon not in {"ep:search", "ep:refresh", "ep:plus", "ep:edit"})


def _entry_button(element):
    return (element.tag == "button" and not _unsafe_button(element)
            and (bool(re.search(r"新增|新建|添加|编辑|修改|详情", element.name))
                 or element.attrs.get("action_icon") in {"ep:plus", "ep:edit"}))


def _line_add_button(element):
    return (element.tag == "button" and not _unsafe_button(element)
            and (bool(re.search(r"新增.*行|添加.*行|添加明细|新增明细|添加产品|添加商品", element.name))
                 or element.attrs.get("action_icon") == "ep:plus"))


@dataclass(frozen=True)
class NativePageReview:
    release: str
    reference: str
    # family, exact route/template, inspected source/effect reference and hash
    scopes: tuple[tuple[str, str, str, str, tuple[str, ...]], ...]
    menu_routes: tuple[str, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()
    menu_digest: str = ""
    fence_digest: str = ""
    column_labels: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()

    @classmethod
    def load(cls, document, release):
        if (not isinstance(document, dict) or document.get("schema_version") not in {1, 2}
                or set(document) != ({"schema_version", "release", "review", "handlers"}
                    if document.get("schema_version") == 1 else
                    {"schema_version", "release", "review", "handlers", "generated"})
                or not release or document["release"] != release
                or not isinstance(document["review"], str) or not 0 < len(document["review"]) <= 200
                or not isinstance(document["handlers"], list) or not 1 <= len(document["handlers"]) <= 2000):
            raise ValueError("native page policy must match the deployment release and reviewed handlers")
        scopes = []
        for item in document["handlers"]:
            if (not isinstance(item, dict) or set(item) != {"kind", "route", "evidence", "sha256", "controls"}
                    or item["kind"] not in HANDLERS or not isinstance(item["route"], str)
                    or not re.fullmatch(r"/[A-Za-z0-9_/{\}-]{1,200}", item["route"])
                    or "{" in item["route"].replace("{id}", "")
                    or "}" in item["route"].replace("{id}", "")
                    or not isinstance(item["evidence"], str) or not 0 < len(item["evidence"]) <= 300
                    or not isinstance(item["controls"], list) or len(item["controls"]) > 32
                    or any(not isinstance(c, str) or not 0 < len(c) <= 100 for c in item["controls"])
                    or len(set(item["controls"])) != len(item["controls"])
                    or (document["schema_version"] == 1 and item["kind"] in {"form_entry", "lines"}
                        and not item["controls"])
                    or (item["kind"] not in {"form_entry", "lines", "query"} and item["controls"])
                    or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("native page policy contains an unknown or unbound handler")
            scope = (*tuple(item[k] for k in ("kind", "route", "evidence", "sha256")), tuple(item["controls"]))
            if any(s[:2] == scope[:2] for s in scopes):
                raise ValueError("native page policy handler scopes must be unique")
            scopes.append(scope)
        if document["schema_version"] == 1:
            return cls(release, document["review"], tuple(scopes))
        generated = document["generated"]
        if (not isinstance(generated, dict)
                or set(generated) != {"routes", "excluded", "menu_sha256", "fence_sha256",
                                      "columns", "columns_sha256"}
                or not isinstance(generated["routes"], list)
                or not 1 <= len(generated["routes"]) <= 600
                or not isinstance(generated["excluded"], list)
                or len(generated["excluded"]) > 600
                or any(not isinstance(route, str)
                       or not re.fullmatch(r"/[A-Za-z0-9_/-]{1,200}", route)
                       for route in generated["routes"])
                or len(set(generated["routes"])) != len(generated["routes"])
                or any(not isinstance(item, list) or len(item) != 2
                       or item[0] not in set(generated["routes"]) | {scope[1] for scope in scopes}
                       or item[1] not in HANDLERS
                       for item in generated["excluded"])
                or len({tuple(item) for item in generated["excluded"]}) != len(generated["excluded"])
                or any(not isinstance(generated[key], str)
                       or not re.fullmatch(r"[0-9a-f]{64}", generated[key])
                       for key in ("menu_sha256", "fence_sha256", "columns_sha256"))
                or not isinstance(generated["columns"], dict)
                or any(route not in generated["routes"] or not isinstance(fields, dict)
                       or len(fields) > 80 or any(not isinstance(key, str)
                       or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", key)
                       or not isinstance(label, str) or not 0 < len(label) <= 40
                       for key, label in fields.items())
                       for route, fields in generated["columns"].items())):
            raise ValueError("native page policy generated menu routes are invalid")
        routes = tuple(generated["routes"])
        digest = hashlib.sha256(json.dumps(routes, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        if digest != generated["menu_sha256"]:
            raise ValueError("native page policy menu digest does not match routes")
        column_digest = hashlib.sha256(json.dumps(generated["columns"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if column_digest != generated["columns_sha256"]:
            raise ValueError("native page policy column digest does not match labels")
        return cls(release, document["review"], tuple(scopes), routes,
                   tuple(tuple(item) for item in generated["excluded"]),
                   digest, generated["fence_sha256"], tuple((route, tuple(fields.items()))
                       for route, fields in generated["columns"].items()))

    def permits(self, kind, path, control=None):
        if any(family == kind and _matches_route(route, path) for route, family in self.excluded):
            return False
        if (kind in HANDLERS and path in self.menu_routes and (path, kind) not in self.excluded):
            return True
        return any(family == kind and _matches_route(route, path)
                   and (control is None or control in controls)
                   for family, route, _, _, controls in self.scopes)

    def reviewed_control(self, kind, path, control):
        """An exact control named by the application review, not a generated route grant."""
        return (bool(control) and self.permits(kind, path, control)
                and any(family == kind and _matches_route(route, path) and control in controls
                        for family, route, _, _, controls in self.scopes))

    @property
    def digest(self):
        return hashlib.sha256(json.dumps([self.release, self.reference, self.scopes,
            self.menu_routes, self.excluded, self.menu_digest, self.fence_digest,
            self.column_labels]).encode()).hexdigest()

    def labels_on(self, route):
        return dict(next((fields for page, fields in self.column_labels if page == route), ()))

    @property
    def capabilities(self):
        installed = {kind for kind, *_ in self.scopes}
        if self.menu_routes:
            installed |= HANDLERS
        return frozenset({"handoffs"} | {name for kind, name in (
            ("tabs", "sections"), ("query", "facets"), ("paging", "paging"),
            ("fields", "fill"), ("lines", "lines")) if kind in installed})


class ModelPageGoalPort:
    """Expose only capabilities the installed model profile implements."""
    def __init__(self, goal_port, review):
        self._goal_port, self.review = goal_port, review

    def __getattr__(self, name): return getattr(self._goal_port, name)

    def capabilities(self):
        # Page procedures expose only their reviewed delivery capabilities.
        # Deployment source tools are supplied independently by the common loop.
        return (self._goal_port.capabilities() - {"source", "facets", "paging", "sections", "fill", "lines"}) \
            | self.review.capabilities


class NativeModelReadActions(NativeReadActions):
    actions = frozenset({"goto", "click", "fill", "select"})

    def __init__(self, origin, data, record_routes, goal_port, review):
        super().__init__(origin, data, record_routes, review=review.reference)
        self.actions = type(self).actions
        self.goal_port, self.page_review, self._queries, self._drafts = goal_port, review, {}, {}
        self._query_turn = None

    def _index(self, context, bound):
        return bound._invoke(self.goal_port.outline_index, context=context)

    def admit(self, intent, *, context, checkpoint):
        before, args = intent.observation, intent.arguments
        path = self._path(before.url)
        if intent.action == "goto":
            if not self.page_review.permits("navigation", self._path(args["url"])):
                raise ActionAdmissionRefused("unreviewed")
            return super().admit(intent, context=context, checkpoint=checkpoint)
        element = next(e for e in before.elements if e.ref == args["ref"])
        navigation = element.attrs.get("navigation_kind")
        if navigation in {"page", "branch"}:
            target = element.attrs.get("navigation_path", "")
            reviewed = (self.page_review.permits("navigation", target) if navigation == "page" else
                any(family == "navigation" and route.startswith(target.rstrip("/") + "/")
                    for family, route, *_ in self.page_review.scopes))
            if not reviewed:
                raise ActionAdmissionRefused("unreviewed")
            return super().admit(intent, context=context, checkpoint=checkpoint)
        category = "tabs" if element.role == "tab" else "navigation"
        if element.attrs.get("page_control") in {"query", "pagination", "page_size"} or element.role == "option":
            return self._query(intent, element, context, checkpoint)
        if not self.page_review.permits(category, path):
            raise ActionAdmissionRefused("unreviewed")
        return super().admit(intent, context=context, checkpoint=checkpoint)

    def _query(self, intent, element, context, checkpoint):
        self._actor(context, checkpoint)
        turn = (context.principal.binding_digest, context.session_id, context.turn_id)
        if turn != self._query_turn:
            self._query_turn = turn
            self._queries.clear()
            self._drafts.clear()
        before, attrs, args = intent.observation, element.attrs, intent.arguments
        path = self._path(before.url)
        family = "query" if attrs.get("page_control") == "query" else "paging"
        if (not self.page_review.permits(family, path) or before.page_route != path
                or str(attrs.get("form_scope", "")).startswith("dialog") or form_control_unavailable(attrs)
                or before.page_read is None):
            raise ActionAdmissionRefused("unreviewed")
        expected = {"route": path, "family": family, "read": before.page_read,
                    "generation": self.data._client.read_generation}
        if intent.action in {"fill", "select"}:
            if (attrs.get("page_control") != "query" or not attrs.get("query_field")
                    or not attrs.get("form_scope") or not attrs.get("form_instance")
                    or len(args["value"]) > 4000):
                raise ActionAdmissionRefused("invalid_target")
            from .slim_loop import _date_pair, _commits
            pair = _date_pair(element, before.elements)
            field = attrs["query_field"]
            if pair:
                if args.get("commit", True) != _commits(element, before.elements):
                    raise ActionAdmissionRefused("invalid_target")
                expected["pair_position"] = pair.index(element)
                field += "[" + str(pair.index(element)) + "]"
            value = args["value"]
            if intent.action == "select":
                choice = args.get("option_label", "")
                if (not attrs.get("field_label") or not choice or value != choice
                        or element.role != "combobox" or attrs.get("aria-haspopup") != "listbox"
                        or not attrs.get("aria-controls")):
                    raise ActionAdmissionRefused("invalid_target")
                # The Runtime selects exactly one enabled owned option and checks
                # its visible label. Native query-value proof comes from the same
                # current application form after selection, not from source lookup.
                expected["choice_readback"] = True
            values = [value]
            if pair:
                values.append(value + (" 00:00:00" if pair.index(element) == 0 else " 23:59:59"))
            expected["digests"] = tuple(hashlib.sha256(json.dumps([v], ensure_ascii=False,
                separators=(",", ":")).encode()).hexdigest() for v in values)
            expected.update(value=args["value"], field=attrs["query_field"], key=field, scope=attrs["form_scope"],
                            instance=attrs["form_instance"], commit=args.get("commit", True),
                            option=args.get("option_label", ""))
        elif intent.action == "click":
            if family == "query":
                if (element.tag != "button" or _unsafe_button(element) or _entry_button(element)
                        or not attrs.get("form_instance")):
                    raise ActionAdmissionRefused("unreviewed")
                if element.name.strip() in {"展开", "收起"}:
                    expected.update(toggle=element.name.strip(), scope=attrs.get("form_scope"),
                                    instance=attrs["form_instance"])
                    digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
                    self._queries[intent.binding_digest] = expected
                    return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY,
                                        self.page_review.reference, digest)
                if element.name.strip() == "重置" or attrs.get("action_icon") == "ep:refresh":
                    expected.update(reset=True, scope=attrs.get("form_scope"),
                                    instance=attrs["form_instance"],
                                    before_fields=before.page_read["query_fields"])
                else:
                    expected["fields"] = {**before.page_read["query_fields"],
                        **self._drafts.get((path, attrs["form_instance"]), {})}
            elif attrs.get("page_control") == "pagination":
                if not attrs.get("page_number", "").isdigit():
                    raise ActionAdmissionRefused("invalid_target")
                expected["page"] = int(attrs["page_number"])
                expected["fields"] = before.page_read["query_fields"]
            elif attrs.get("page_control") == "page_size":
                expected["open_paging"] = True
            elif element.role == "option":
                number = re.fullmatch(r"\s*([1-9][0-9]*)\s*条/页\s*", element.name)
                # The option must be owned by the exact reviewed paging popup.
                owners = [e for e in before.elements if e.attrs.get("page_control") == "page_size"
                          and e.attrs.get("aria-controls") and e.attrs["aria-controls"] == attrs.get("listbox_owner")]
                if number is None or len(owners) != 1:
                    raise ActionAdmissionRefused("unreviewed")
                expected["page_size"] = int(number[1])
                expected["fields"] = before.page_read["query_fields"]
            else:
                raise ActionAdmissionRefused("unreviewed")
        else:
            raise ActionAdmissionRefused("unreviewed")
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        self._queries[intent.binding_digest] = expected
        return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY, self.page_review.reference, digest)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        expected = self._queries.get(intent.binding_digest)
        if expected is None:
            return super().verify(permit, intent, receipt, observation=observation, context=context, checkpoint=checkpoint)
        self._actor(context, checkpoint)
        if (observation.page_route != expected["route"] or observation.url != intent.observation.url
                or self.data._client.read_generation != expected["generation"]):
            return False
        if "toggle" in expected:
            buttons = [e for e in observation.elements if e.tag == "button"
                       and e.attrs.get("form_scope") == expected["scope"]
                       and e.attrs.get("form_instance") == expected["instance"]
                       and e.name.strip() == ({"展开": "收起", "收起": "展开"}[expected["toggle"]])]
            return len(buttons) == 1 and observation.page_read == expected["read"]
        if "field" in expected:
            controls = [e for e in observation.elements if e.attrs.get("query_field") == expected["field"]
                        and e.attrs.get("form_scope") == expected["scope"]
                        and e.attrs.get("form_instance") == expected["instance"]]
            if "pair_position" in expected:
                from .slim_loop import _date_pair
                starts = [e for e in controls if e.attrs.get("field_label")]
                pair = _date_pair(starts[0], observation.elements) if len(starts) == 1 else []
                controls = [pair[expected["pair_position"]]] if pair else []
            if len(controls) != 1:
                return False
            shown = controls[0].attrs.get("selected_option") if expected["option"] else controls[0].attrs.get("value", "")
            if shown != (expected["option"] or expected["value"]):
                return False
            digests = expected["digests"]
            if expected.get("choice_readback"):
                if observation.page_read != expected["read"]:
                    return False
                value = controls[0].attrs.get("query_value", "")
                if not re.fullmatch(r"(?:[0-9]{1,20}|true|false)", value):
                    return False
                digests = (hashlib.sha256(json.dumps([value], ensure_ascii=False,
                    separators=(",", ":")).encode()).hexdigest(),)
            self._drafts.setdefault((expected["route"], expected["instance"]), {})[expected["key"]] = digests
            return True
        if expected.get("open_paging"):
            return any(e.role == "option" for e in observation.elements)
        read = observation.page_read
        if expected.get("reset"):
            if (read is None or read["path"] != expected["read"]["path"]
                    or read["revision"] <= expected["read"]["revision"]
                    or read["page"] != 1):
                return False
            observed_fields = set()
            for control in observation.elements:
                attrs = control.attrs
                if (attrs.get("page_control") != "query" or not attrs.get("query_field")
                        or attrs.get("form_scope") != expected["scope"]
                        or attrs.get("form_instance") != expected["instance"]):
                    continue
                key = attrs["query_field"]
                if "query_value" in attrs:
                    value = attrs["query_value"]
                elif "value" in attrs:
                    value = attrs["value"]
                elif control.role in {"textbox", "searchbox", "combobox"}:
                    # The browser omits the value attribute for an empty native
                    # input; the live control still proves the empty value.
                    value = ""
                else:
                    continue
                if not isinstance(value, str):
                    return False
                digest = hashlib.sha256(json.dumps([value], ensure_ascii=False,
                    separators=(",", ":")).encode()).hexdigest()
                if key in read["query_fields"]:
                    if read["query_fields"][key] != digest:
                        return False
                elif value:
                    return False
                observed_fields.add(key)
            changed = {key for key in set(expected["before_fields"]) | set(read["query_fields"])
                       if expected["before_fields"].get(key) != read["query_fields"].get(key)}
            return bool(observed_fields) and changed <= observed_fields
        return (read is not None and read["path"] == expected["read"]["path"]
                and read["revision"] > expected["read"]["revision"]
                and read["page"] == expected.get("page", 1)
                and set(read["query_fields"]) == set(expected.get("fields", expected["read"]["query_fields"]))
                and all(read["query_fields"][key] in (values if isinstance(values, tuple) else (values,))
                    for key, values in expected.get("fields", expected["read"]["query_fields"]).items())
                and ("page_size" not in expected or read["page_size"] == expected["page_size"]))


class NativeDraftSteps:
    application_id = "yudao"
    required_scope = "yudao.read"
    actions = frozenset({"click", "fill", "select"})

    def __init__(self, data, forms, review):
        self.data, self.forms, self.review, self._prepared = data, forms, review, {}
        self._turn = None

    @staticmethod
    def _target(intent):
        return next(e for e in intent.observation.elements if e.ref == intent.arguments["ref"])

    def handles(self, intent):
        if intent.action not in self.actions:
            return False
        element = self._target(intent)
        scope = str(element.attrs.get("form_scope", ""))
        return scope.startswith("dialog") or (intent.action == "click" and _entry_button(element)
            and self.review.permits("form_entry", urlsplit(intent.observation.url).path, element.name.strip()))

    @staticmethod
    def _lines(observation, scope):
        return frozenset(int(m[1]) for e in observation.elements
                         if e.attrs.get("form_scope") == scope
                         and (m := re.fullmatch(r"明细第([1-9][0-9]*)行·.+", e.attrs.get("title", ""))))

    def prepare_step(self, intent, *, context, checkpoint):
        bound = BoundDataReads(self.data, context, checkpoint)
        turn = (context.principal.binding_digest, context.session_id, context.turn_id)
        if self._turn != turn:
            self._turn = turn
            self._prepared.clear()
        before, element, args = intent.observation, self._target(intent), intent.arguments
        route = urlsplit(before.url).path
        if before.page_route != route or form_control_unavailable(element.attrs):
            raise ActionAdmissionRefused("invalid_target")
        attrs = element.attrs
        scope, instance = attrs.get("form_scope", ""), attrs.get("form_instance", "")
        label = attrs.get("field_label") or attrs.get("title", "")
        mark = self.forms._evidence.mark()
        prepared = {"route": route, "generation": self.data._client.read_generation, "scope": scope,
                    "instance": instance, "label": label, "mark": mark, "ref": element.ref,
                    "lifetime": attrs.get("form_lifetime", "")}
        values = ()
        if intent.action in {"fill", "select"}:
            line = re.fullmatch(r"明细第([1-9][0-9]*)行·(.+)", attrs.get("title", ""))
            from .slim_loop import _date_pair, _commits
            pair = _date_pair(element, before.elements)
            if pair:
                label = pair[0].attrs.get("field_label", label)
            if line:
                label = attrs["title"]
            prepared["label"] = label
            family = "lines" if line else "fields"
            if (not self.review.permits(family, route) or not scope.startswith("dialog")
                    or not instance or not label or len(args["value"]) > 4000):
                raise ActionAdmissionRefused("unreviewed")
            if pair:
                if args.get("commit", True) != _commits(element, before.elements):
                    raise ActionAdmissionRefused("invalid_target")
                prepared["pair_position"] = pair.index(element)
                prepared["pair_label"] = pair[0].attrs.get("field_label", label)
            elif line:
                prepared["line"] = line[1]
            else:
                # Scalar entry uses the existing application-owned form preparation.
                # Composite dates/lines use only this optional single-step contract.
                try:
                    with self.data._client.read_window(bound._check):
                        _, requested = self.forms.adopt_open(before, ((label, args["value"]),), context=context)
                        form = self.forms.describe_form(before, context=context,
                                                        allow_unobserved_submit=True)
                        fields = [f for f in form.fields if f.reference == requested[0][0]] if form else []
                        if len(fields) != 1:
                            raise ActionAdmissionRefused("unreviewed")
                        draft = self.forms.prepare_form(form, (ApplicationFormValue(
                            fields[0], args["value"], element.role),), context=context)
                        if draft.persistence != "transient_until_submit" or draft.fields[0].value != args["value"]:
                            raise ActionAdmissionRefused("unsafe_effect")
                except BusinessToolRequestError as exc:
                    raise ActionAdmissionRefused("unreviewed") from exc
            step = "enter_value"
            values = ((label, args["value"]),)
            prepared.update(value=args["value"], option=args.get("option_label", ""))
        elif scope.startswith("dialog"):
            if not instance:
                raise ActionAdmissionRefused("invalid_target")
            if (element.tag == "button" and not _unsafe_button(element)
                    and self.review.permits("lines", route, element.name.strip())
                    and (_line_add_button(element) or self.review.reviewed_control(
                        "lines", route, element.name.strip()))):
                step = "add_line"
                if not prepared["lifetime"]:
                    raise ActionAdmissionRefused("invalid_target")
                prepared["lines"] = sorted(self._lines(before, scope))
            elif element.role in {"combobox", "select"} and self.review.permits("fields", route):
                if not label or not attrs.get("aria-controls"):
                    raise ActionAdmissionRefused("invalid_target")
                prepared.update(role=element.role, tag=element.tag, owner=attrs["aria-controls"])
                step = "open_choice"
            else:
                raise ActionAdmissionRefused("unsafe_effect")
        else:
            if not _entry_button(element) or not self.review.permits("form_entry", route, element.name.strip()):
                raise ActionAdmissionRefused("unreviewed")
            step, scope, instance = "open_entry", "entry", "document-" + str(before.epoch)
        prepared["step"] = step
        digest = hashlib.sha256(json.dumps(prepared, sort_keys=True).encode()).hexdigest()
        self._prepared[intent.binding_digest] = prepared
        bound._check()
        return DraftStepPermit(intent.binding_digest, self.review.reference, digest,
                               route + ":" + scope, instance, step, values)

    def verify_step(self, permit, intent, receipt, *, observation, context, checkpoint):
        BoundDataReads(self.data, context, checkpoint)
        expected = self._prepared.get(intent.binding_digest)
        if (expected is None or self.data._client.read_generation != expected["generation"]
                or self.forms._evidence.since(expected["mark"]) or observation.url != intent.observation.url
                or observation.page_route != expected["route"]):
            return False
        if expected["step"] == "open_entry":
            forms = {(e.attrs.get("form_scope"), e.attrs.get("form_instance")) for e in observation.elements
                     if str(e.attrs.get("form_scope", "")).startswith("dialog") and e.attrs.get("form_instance")}
            return len(forms) == 1
        controls = [e for e in observation.elements if e.attrs.get("form_scope") == expected["scope"]
                    and e.attrs.get("form_instance") == expected["instance"]]
        if not controls and expected["step"] != "add_line":
            return False
        if expected["step"] == "add_line":
            controls = [e for e in observation.elements if e.attrs.get("form_scope") == expected["scope"]
                        and e.attrs.get("form_lifetime") == expected["lifetime"]]
            lines = self._lines(replace(observation, elements=controls), expected["scope"])
            return bool(controls) and set(expected["lines"]) < lines and len(lines) == len(expected["lines"]) + 1
        if expected["step"] == "open_choice":
            matches = [e for e in controls if (e.attrs.get("field_label") or e.attrs.get("title")) == expected["label"]]
            return (len(matches) == 1 and matches[0].role == expected["role"] and matches[0].tag == expected["tag"]
                    and matches[0].attrs.get("aria-controls") == expected["owner"]
                    and matches[0].attrs.get("aria-expanded") == "true"
                    and not form_control_unavailable(matches[0].attrs))
        if "pair_position" in expected:
            from .slim_loop import _date_pair
            starts = [e for e in controls if e.attrs.get("field_label") == expected["pair_label"]]
            pair = _date_pair(starts[0], controls) if len(starts) == 1 else None
            matches = [pair[expected["pair_position"]]] if pair else []
        else:
            matches = [e for e in controls if (e.attrs.get("field_label") or e.attrs.get("title")) == expected["label"]]
        if len(matches) != 1:
            return False
        shown = (matches[0].attrs.get("value", "") if matches[0].role == "radiogroup" else
                 matches[0].attrs.get("selected_option") if expected["option"] else matches[0].attrs.get("value", ""))
        if not expected["option"] and matches[0].role == "spinbutton":
            try:
                actual, requested = Decimal(shown), Decimal(expected["value"])
                return actual.is_finite() and requested.is_finite() and actual == requested
            except (InvalidOperation, ValueError):
                return False
        return shown == (expected["option"] or expected["value"])

    def verify_no_effect(self, permit, intent, receipt, *, observation, context, checkpoint):
        BoundDataReads(self.data, context, checkpoint)
        expected = self._prepared.get(intent.binding_digest)
        if (expected is None or permit.step != "enter_value" or intent.action != "select"
                or not receipt.no_effect or self.data._client.read_generation != expected["generation"]
                or self.forms._evidence.since(expected["mark"])
                or observation.url != intent.observation.url
                or observation.page_route != expected["route"]):
            return False
        from web_operation_agent.execution.draft_handoff import draft_fields, field_identity, field_value
        def state(obs):
            fields = [e for e in draft_fields(obs) if e.attrs.get("form_scope") == expected["scope"]]
            return sorted((field_identity(e), field_value(e), e.tag,
                           e.attrs.get("aria-controls", ""), form_control_unavailable(e.attrs)) for e in fields)
        before, after = state(intent.observation), state(observation)
        return bool(before) and before == after

    def may_continue_unverified(self, permit, intent, receipt, *, observation, context, checkpoint):
        """The page may have changed locally; guarded business state did not."""
        BoundDataReads(self.data, context, checkpoint)
        expected = self._prepared.get(intent.binding_digest)
        return bool(expected and self.data._client.read_generation == expected["generation"]
                    and not self.forms._evidence.since(expected["mark"])
                    and observation.url == intent.observation.url
                    and observation.page_route == expected["route"])
