"""Reviewed standard query/list handlers over the existing generic browser tools.

Source extracts control bindings, not authority: a release-bound filter review and
the actor's current catalog are mandatory. Query drafts have no business effect.
Successful applied reads, not input values alone, settle search and pagination.
"""
import hashlib
import json
import re

from web_operation_agent.execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionPermit)
from .source import _function_body
from .source_forms import _Template, _walk

PAGINATION_COMPONENT = "ui/src/components/Pagination/index.vue"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _plain(body):
    if body is None:
        return ""
    return re.sub(r"\s+", "", re.sub(r"/\*.*?\*/|//[^\n]*", "", body, flags=re.S))


def _compile(tree, review):
    review.check(tree)
    source = tree.read_text(review.component)
    if re.search(r"(?:watch|watchEffect|onUpdated)\s*\([\s\S]{0,100}?\bqueryParams\b", source):
        raise ActionAdmissionRefused("unreviewed")
    parser = _Template(); parser.feed(source); parser.close()
    nodes = [n for root in parser.roots if root.tag == "template" for n, _ in _walk(root)]
    forms = [n for n in nodes if n.tag == "el-form"]
    pagers = [n for n in nodes if n.tag == "pagination"]
    if (not parser.valid or parser.stack or len(forms) != 1 or len(pagers) != 1
            or forms[0].attrs.get(":model") != "queryParams"):
        raise ActionAdmissionRefused("unreviewed")
    pager = pagers[0]
    listing = pager.attrs.get("@pagination", "")
    if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", listing)
            or pager.attrs.get("v-model:page") != "queryParams.pageNo"
            or pager.attrs.get("v-model:limit") != "queryParams.pageSize"):
        raise ActionAdmissionRefused("unreviewed")
    body = _plain(_function_body(source, listing))
    # The reviewed standard list handler only assigns loading, returned rows and total.
    if not re.fullmatch(r"loading\.value=true;?try\{constdata=await[A-Za-z][A-Za-z0-9_]*\."
        r"[A-Za-z][A-Za-z0-9_]*\(queryParams\);?list\.value=data\.list;?"
        r"total\.value=data\.total;?\}finally\{loading\.value=false;?\}", body):
        raise ActionAdmissionRefused("unreviewed")
    buttons = []
    fields = {}
    for node, _ in _walk(forms[0]):
        if node.tag == "el-button":
            handler = node.attrs.get("@click", "")
            if (re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", handler) and re.fullmatch(
                    r"queryParams\.pageNo=1;?" + re.escape(listing) + r"\(\);?",
                    _plain(_function_body(source, handler)))):
                label = " ".join(" ".join(n.text for n, _ in _walk(node)).split())
                if label and not any(k in node.attrs for k in ("v-bind", "href", "@click.prevent", "@click.stop")):
                    buttons.append(label)
        if node.tag != "el-form-item" or not node.attrs.get("prop") or not node.attrs.get("label"):
            continue
        key, label = node.attrs["prop"], node.attrs["label"]
        inputs = [n for n, _ in _walk(node) if n.tag in {"el-input", "el-input-number"}]
        if len(inputs) != 1 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            continue
        control = inputs[0]
        if (control.tag != "el-input" or control.attrs.get("v-model") != "queryParams." + key
                or any(k.startswith(("@", "v-on", "v-bind", "v-model.")) and k != "@keyup.enter"
                       for k in control.attrs)
                or control.attrs.get("@keyup.enter") and not re.fullmatch(
                    r"queryParams\.pageNo=1;?" + re.escape(listing) + r"\(\);?",
                    _plain(_function_body(source, control.attrs["@keyup.enter"])))):
            continue
        if label in fields:
            raise ActionAdmissionRefused("unreviewed")
        fields[label] = key
    if len(buttons) != 1:
        raise ActionAdmissionRefused("unreviewed")
    return buttons[0], fields


class NativeListActions:
    actions = frozenset({"click", "fill"})

    def __init__(self, tree, reviews):
        self.tree, self.reviews = tree, tuple(reviews)
        self._turn = None
        self._checks, self._drafts = {}, {}

    def _key(self, intent, context):
        key = (context.principal.binding_digest, context.session_id, context.turn_id)
        if self._turn != key:
            self._turn = key
            self._checks.clear(); self._drafts.clear()
        return key

    def admit(self, intent, index, *, context, checkpoint):
        checkpoint(); self._key(intent, context)
        before = intent.observation
        reviews = [r for r in self.reviews if r.route == before.page_route]
        if len(reviews) != 1:
            raise ActionAdmissionRefused("unreviewed")
        review = reviews[0]
        page = index.page(review.route)
        if page is None or page.entity != review.entity:
            raise ActionAdmissionRefused("unreviewed")
        button, fields = _compile(self.tree, review)
        proof = before.page_read
        if proof is None or proof["path"] != "/admin-api/" + review.endpoint:
            raise ActionAdmissionRefused("unreviewed")
        element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
        attrs = element.attrs
        if str(attrs.get("form_scope", "")).startswith("dialog") or attrs.get("disabled") == "true":
            raise ActionAdmissionRefused("invalid_target")
        expected = {"proof": proof, "kind": ""}
        if intent.action == "fill":
            label = attrs.get("field_label", "")
            key = fields.get(label)
            value = intent.arguments["value"]
            if (key is None or element.role != "textbox" or attrs.get("page_control") != "query"
                    or attrs.get("query_field") != key
                    or not attrs.get("form_instance")
                    or not attrs.get("form_scope") or len(value) > 120 or attrs.get("readonly") == "true"):
                raise ActionAdmissionRefused("unreviewed")
            expected.update(kind="input", label=label, key=key, value=value,
                scope=attrs["form_scope"], instance=attrs["form_instance"])
        elif attrs.get("page_control") == "pagination":
            if PAGINATION_COMPONENT not in dict(review.hashes):
                raise ActionAdmissionRefused("unreviewed")
            number, size = attrs.get("page_number", ""), attrs.get("page_size", "")
            if (not number.isdigit() or not size.isdigit() or int(size) != proof["page_size"]
                    or not 1 <= int(number) <= (proof["total"] + int(size)-1)//int(size)
                    or int(number) == proof["page"]):
                raise ActionAdmissionRefused("invalid_target")
            expected.update(kind="query", page=int(number), fields=proof["query_fields"])
        elif (element.role == "button" and element.name == button and attrs.get("form_instance")
                and attrs.get("page_control") == "query"):
            draft_key = (before.page_route, attrs["form_instance"])
            changes = self._drafts.get(draft_key, {})
            expected.update(kind="query", page=1, fields={**proof["query_fields"],
                **{k: _hash([v]) for k, v in changes.items()}})
        else:
            raise ActionAdmissionRefused("unreviewed")
        self._checks[intent.binding_digest] = (review, expected)
        checkpoint()
        return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY,
            "native-standard-list:" + _hash(review.hashes), _hash(expected))

    def verify(self, permit, intent, observation, *, index, context, checkpoint):
        checkpoint(); self._key(intent, context)
        saved = self._checks.get(intent.binding_digest)
        if saved is None:
            return False
        review, expected = saved
        review.check(self.tree)
        page = index.page(review.route)
        if page is None or page.entity != review.entity:
            return False
        if (permit.postcondition != _hash(expected) or observation.url != intent.observation.url
                or observation.page_route != review.route):
            return False
        after = observation.page_read
        if expected["kind"] == "input":
            controls = [e for e in observation.elements if e.role == "textbox"
                and e.attrs.get("page_control") == "query"
                and e.attrs.get("query_field") == expected["key"]
                and e.attrs.get("form_scope") == expected["scope"]
                and e.attrs.get("form_instance") == expected["instance"]
                and e.attrs.get("field_label") == expected["label"]]
            if (len(controls) != 1 or controls[0].attrs.get("value", "") != expected["value"]
                    or after != expected["proof"]):
                return False
            self._drafts.setdefault((review.route, expected["instance"]), {})[expected["key"]] = expected["value"]
            return True
        if (after is None or after["path"] != expected["proof"]["path"]
                or after["revision"] <= expected["proof"]["revision"]
                or after["page"] != expected["page"] or after["page_size"] != expected["proof"]["page_size"]
                or after["query_fields"] != expected["fields"]):
            return False
        self._drafts.clear()
        return True
