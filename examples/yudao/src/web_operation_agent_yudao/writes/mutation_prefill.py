"""Exact CRM contact proposal to a guarded native form handoff."""
from __future__ import annotations

from urllib.parse import urlsplit

from web_operation_agent.business.forms import ApplicationFormValue
from web_operation_agent.business.prefill_conversion import MutationPrefillOffer
from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.execution.draft_handoff import (
    draft_fields, field_identity, field_value, handoff_draft)
from web_operation_agent.execution.model_loop_contracts import LoopToolResult

from .live_forms import LiveRequest, plain


_ROUTE = "/crm/contact"
_FIELDS = (("customer", "客户名称"), ("name", "联系人姓名"),
           ("mobile", "手机"), ("post", "职位"))


def _refused():
    return LoopToolResult("联系人草稿未通过核验，请核对当前表单。", kind="page",
                          state="unverified", error_code="unverified")


def _customer_choice_settled(before, after, customer_key, customer):
    """Only the selected customer and one empty dependent parent may change."""
    prior, current = draft_fields(before), draft_fields(after)
    old = {field_identity(e): field_value(e) for e in prior}
    now = {field_identity(e): field_value(e) for e in current}
    added = now.keys() - old.keys()
    return (before.url == after.url and before.page_route == after.page_route
            and after.render_state == "ready"
            and len(old) == len(prior) and len(now) == len(current)
            and customer_key in old and old.keys() <= now.keys()
            and now[customer_key] == customer
            and all(now[key] == value for key, value in old.items() if key != customer_key)
            and len(added) <= 1
            and all(key[4] == "直属上级" and not now[key] for key in added))


class YudaoMutationPrefill:
    """Only a contact create on its reviewed collection page is offered.

    The customer choice is matched to a unique actor-visible simple-list row both
    before the card and after the browser selects its label. Other native writes
    have no reviewed proposal-to-form conversion and receive no third choice.
    """

    required_scope = "yudao.read"

    def __init__(self, client, forms, review):
        self.client, self.forms, self.review = client, forms, review

    def _customer(self, identifier, name):
        rows = self.client.get("crm/customer/simple-list")
        if not isinstance(rows, list):
            return False
        same = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
        return (type(identifier) is int and identifier > 0 and len(same) == 1
                and str(same[0].get("id")) == str(identifier))

    @staticmethod
    def _values(proposal):
        if (proposal.tool != "crm_contact_create"
                or proposal.effect != "新建CRM 联系人资料"
                or not isinstance(proposal.args, dict)):
            return None
        args = proposal.args
        if (set(args) != {"body", "before", "entity", "actor", "operation", "choices", "relations"}
                or args["entity"] != "crm/contact" or args["operation"] != proposal.tool
                or args["before"] != {} or args["choices"] != []
                or not isinstance(args["body"], dict)
                or set(args["body"]) != {"name", "mobile", "post", "customerId", "ownerUserId"}
                or not all(isinstance(args["body"][key], str) and args["body"][key]
                           for key in ("name", "mobile", "post"))
                or type(args["body"]["customerId"]) is not int
                or type(args["body"]["ownerUserId"]) is not int
                or not isinstance(args["relations"], list)
                or len(args["relations"]) != 1):
            return None
        relation = args["relations"][0]
        if (not isinstance(relation, dict) or set(relation) != {"entity", "id", "name"}
                or relation["entity"] != "crm/customer"
                or relation["id"] != args["body"]["customerId"]
                or not isinstance(relation["name"], str) or not relation["name"]
                or proposal.targets != (args["body"]["name"],)):
            return None
        return args["body"], relation["name"]

    def offer(self, proposal, *, context, observation):
        values = self._values(proposal)
        if values is None:
            return None
        context.require_live(application_id="yudao", required_scope=self.required_scope)
        body, customer = values
        if (str(body["ownerUserId"]) != context.principal.actor_id
                or str(proposal.args["actor"]) != context.principal.actor_id
                or observation.render_state != "ready"
                or observation.page_route != _ROUTE
                or urlsplit(observation.url).path != _ROUTE
                or any(str(e.attrs.get("form_scope", "")).startswith("dialog")
                       for e in observation.elements)
                or not self.review.permits("form_entry", _ROUTE, "新增")
                or not self.review.permits("fields", _ROUTE)
                or len([e for e in observation.elements if e.role == "button"
                        and e.name.strip() == "新增"
                        and not str(e.attrs.get("form_scope", "")).startswith("dialog")
                        and not form_control_unavailable(e.attrs)]) != 1
                or not self._customer(body["customerId"], customer)):
            return None
        return MutationPrefillOffer(
            proposal.content_digest, context.binding_digest,
            f"{_ROUTE}#crm/customer/{body['customerId']}",
            (("customerId", str(body["customerId"])), ("customer", customer),
             ("ownerUserId", str(body["ownerUserId"])),
             ("name", body["name"]), ("mobile", body["mobile"]), ("post", body["post"])))

    def handoff(self, offer, proposal, *, context):
        offer.require(proposal, context.operation)
        context.operation.require_live(application_id="yudao", required_scope=self.required_scope)
        values = self._values(proposal)
        if values is None:
            return _refused()
        body, customer = values
        expected_offer = {"customerId": str(body["customerId"]), "customer": customer,
                          "ownerUserId": str(body["ownerUserId"]),
                          **{key: body[key] for key in ("name", "mobile", "post")}}
        current = context.obs
        if (dict(offer.fields) != expected_offer
                or offer.target != f"{_ROUTE}#crm/customer/{body['customerId']}"
                or urlsplit(current.url).path != _ROUTE or current.page_route != _ROUTE
                or current.render_state != "ready"
                or not self._customer(body["customerId"], customer)):
            return _refused()
        reference = "proposal:" + proposal.content_digest[:32]
        request = LiveRequest(reference, "create", "联系人",
                              tuple((key, (label,)) for key, label in _FIELDS),
                              tuple((key, customer if key == "customer" else body[key])
                                    for key, _ in _FIELDS), ("新增",))
        self.forms.expect(request)
        fence_mark = self.forms._evidence.mark()
        entry = self.forms.plan_entry(reference, current, context=context.operation)
        opened = context.page.execute("click", {"ref": entry.control.ref})
        if opened.state != "completed" or opened.receipt is None:
            self.forms.discard_entry(entry, context=context.operation)
            return _refused()
        actual = self.forms.accept_entry(entry, current, opened.receipt, context.obs,
                                         context=context.operation)
        form = self.forms.describe_form(context.obs, context=context.operation)
        if (form is None or form.reference != actual or form.route != _ROUTE
                or plain(form.submit_label) != "确定"):
            return _refused()
        fields = {field.reference: field for field in form.fields}
        if not {key for key, _ in _FIELDS} <= fields.keys():
            return _refused()
        owner = [e for e in context.obs.elements if e.attrs.get("form_scope") == form.control_scope
                 and e.attrs.get("field_label") == "负责人"]
        if len(owner) != 1 or not owner[0].attrs.get("selected_option"):
            return _refused()
        actor = self.client.get("system/auth/get-permission-info")
        user = actor.get("user") if isinstance(actor, dict) else None
        if (not isinstance(user, dict) or str(user.get("id")) != str(body["ownerUserId"])
                or owner[0].attrs["selected_option"] != user.get("nickname")):
            return _refused()
        form_values = tuple(ApplicationFormValue(
            fields[key], customer if key == "customer" else body[key],
            "combobox" if key == "customer" else "textbox") for key, _ in _FIELDS)
        draft = self.forms.prepare_form(form, form_values, context=context.operation)
        if (draft.persistence != "transient_until_submit" or draft.route != _ROUTE
                or len(draft.fields) != len(_FIELDS)):
            return _refused()
        before, expected = context.obs, []
        for index, item in enumerate(draft.fields):
            for attempt in range(2):
                matches = [e for e in context.obs.elements
                           if e.attrs.get("form_scope") == form.control_scope
                           and e.attrs.get("field_label") == item.label and e.role == item.control]
                if len(matches) != 1:
                    return _refused()
                element = matches[0]
                args = {"ref": element.ref, "value": item.value}
                if item.option_label:
                    args["option_label"] = item.option_label
                else:
                    args["commit"] = True
                outcome = context.page.execute("select" if item.option_label else "fill", args)
                if (outcome.state == "completed" or attempt
                        or outcome.receipt is None
                        or outcome.receipt.error_code != "stale_observation"):
                    break
                if context.page.execute("observe", {}).state != "completed":
                    return _refused()
            if outcome.state != "completed":
                return _refused()
            if index == 0:
                # Choosing a customer reveals the dependent "直属上级" control.
                # Accept only that new, empty field and preserve every old value
                # other than the customer. The final generic handoff then starts
                # from this settled form and still checks every field it sees.
                customer_key = field_identity(element)
                if (item.field != "customer" or not _customer_choice_settled(
                        before, context.obs, customer_key, customer)):
                    return _refused()
                before = context.obs
                expected.append((customer_key, customer))
            else:
                expected.append((field_identity(element), item.option_label or item.value))
        if (not self._customer(body["customerId"], customer)
                or self.forms._evidence.since(fence_mark)):
            return _refused()
        return handoff_draft(context, before, expected, form.submit_label)
