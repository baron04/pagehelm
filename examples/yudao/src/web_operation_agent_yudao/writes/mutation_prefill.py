"""Exact CRM contact and general mappable Yudao form proposal to guarded native prefill."""
from __future__ import annotations

from urllib.parse import urlsplit

from web_operation_agent.business.forms import ApplicationFormValue
from web_operation_agent.business.prefill_conversion import MutationPrefillOffer
from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.business.write_pipeline import DEFAULT_SECRET_FIELD_PATTERN
from web_operation_agent.execution.draft_handoff import (
    draft_fields, field_identity, field_value, handoff_draft)
from web_operation_agent.execution.model_loop_contracts import LoopToolResult

from .live_forms import CREATE_WORDS, LiveRequest, SUBMIT_WORDS, UPDATE_WORDS, plain
from .write_definitions import RECORD_CONTRACTS, RECORD_OPERATIONS


_ROUTE = "/crm/contact"
_FIELDS = (("customer", "客户名称"), ("name", "联系人姓名"),
           ("mobile", "手机"), ("post", "职位"))
_COMMON_FIELD_LABELS = {
    "name": "名称",
    "title": "标题",
    "mobile": "手机",
    "telephone": "固定电话",
    "email": "邮箱",
    "wechat": "微信",
    "remark": "备注",
    "detailAddress": "详细地址",
    "areaId": "所在地区",
    "contactNextTime": "下次联系时间",
    "qq": "QQ",
    "industryId": "所属行业",
    "level": "客户等级",
    "source": "客户来源",
    "sex": "性别",
    "post": "职位",
    "contact": "联系人",
    "fax": "传真",
    "status": "状态",
    "sort": "排序",
    "taxNo": "税号",
    "taxPercent": "税率",
    "bankName": "开户行",
    "bankAccount": "银行账号",
    "bankAddress": "开户地址",
    "address": "地址",
    "principal": "负责人",
    "warehousePrice": "仓储费",
    "truckagePrice": "搬运费",
    "code": "编码",
    "description": "描述",
    "barCode": "条码",
    "standard": "规格",
    "expiryDay": "保质期",
    "weight": "重量",
    "purchasePrice": "采购价格",
    "salePrice": "销售价格",
    "minPrice": "最低价格",
    "phone": "联系电话",
    "type": "类型",
    "content": "内容",
    "picUrl": "图片",
    "no": "账号",
    "categoryId": "分类",
    "unitId": "单位",
    "parentId": "上级",
    "leaderUserId": "负责人",
}


def _refused(message="联系人草稿未通过核验，请核对当前表单。", error_code="unverified"):
    return LoopToolResult(message, kind="page", state="unverified", error_code=error_code)


def _form_closed_or_readonly_result():
    return LoopToolResult(
        "表单已关闭或为只读状态，无法继续预填；请先重新核对页面后再决定。",
        kind="page", state="unverified", error_code="form_closed_or_readonly",
    )


def _is_restricted_element(element) -> bool:
    attrs = getattr(element, "attrs", None) or {}
    return bool(
        form_control_unavailable(attrs)
        or attrs.get("restricted")
        or str(attrs.get("input_type", "")).lower() in {"password", "file"}
    )


def _record_field_outcomes(target_obj, *, applied=None, rejected=None, previous=None, restricted=()):
    if target_obj is None:
        return
    if applied:
        cur = dict(getattr(target_obj, "applied_fields", {}) or {})
        cur.update(applied)
        setattr(target_obj, "applied_fields", cur)
    if rejected:
        cur = dict(getattr(target_obj, "rejected_fields", {}) or {})
        cur.update(rejected)
        setattr(target_obj, "rejected_fields", cur)
    if previous:
        cur = dict(getattr(target_obj, "previous_fields", {}) or {})
        for key, value in previous.items():
            cur.setdefault(key, value)
        setattr(target_obj, "previous_fields", cur)
    if restricted:
        existing = list(getattr(target_obj, "restricted_fields", ()) or ())
        for item in restricted:
            if item not in existing:
                existing.append(item)
        setattr(target_obj, "restricted_fields", tuple(existing))


def _attach_handoff_facts(context, *, applied_fields, rejected_fields, restricted_controls, pending_fields):
    seen_pending: set[str] = set()
    pending = []
    for name in [*rejected_fields.keys(), *pending_fields, *restricted_controls]:
        label = str(name)
        if label and label not in seen_pending:
            seen_pending.add(label)
            pending.append({"name": label})
    handoff: dict = {
        "completed_fields": [{"name": str(k), "value": str(v)} for k, v in applied_fields.items()],
        "pending_fields": pending,
        "can_undo": bool(applied_fields),
    }
    if restricted_controls:
        handoff["restricted_controls"] = [
            {"label": str(lbl), "kind": "password" if "密码" in str(lbl) else "restricted"}
            for lbl in restricted_controls
        ]
    setattr(context, "handoff_facts", handoff)
    operation = getattr(context, "operation", None)
    if operation is not None:
        try:
            setattr(operation, "handoff_facts", handoff)
        except Exception:
            pass
    return handoff


def _receipt_error_code(outcome) -> str:
    receipt = getattr(outcome, "receipt", None)
    return str(getattr(receipt, "error_code", "") or getattr(outcome, "error_code", "") or "")


def _collect_receipt_outcomes(outcome, label: str, fallback_value: str, fallback_prev: str,
                              applied: dict, rejected: dict, previous: dict, restricted: list):
    receipt = getattr(outcome, "receipt", None)
    r_applied = getattr(receipt, "applied_fields", None) if receipt is not None else None
    r_rejected = getattr(receipt, "rejected_fields", None) if receipt is not None else None
    r_previous = getattr(receipt, "previous_fields", None) if receipt is not None else None
    r_restricted = getattr(receipt, "restricted_fields", None) if receipt is not None else None
    if isinstance(r_previous, dict):
        previous.update(r_previous)
    elif label and label not in previous:
        previous[label] = fallback_prev
    if isinstance(r_restricted, (list, tuple)):
        for item in r_restricted:
            if item not in restricted:
                restricted.append(item)
    if outcome.state == "completed":
        if isinstance(r_applied, dict) and r_applied:
            applied.update(r_applied)
        else:
            applied[label] = fallback_value
    else:
        if isinstance(r_rejected, dict) and r_rejected:
            rejected.update(r_rejected)
        else:
            rejected[label] = _receipt_error_code(outcome) or "rejected"


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
    """Offer guarded native form prefill for CRM contacts and general mappable Yudao forms.

    The exact `crm_contact_create` path verifies the customer simple-list row and
    dependent parent control; general mappable native forms verify actor, route,
    reviewed fields/opener permission, non-secret scalar fields, and live dialog controls.
    """

    required_scope = "yudao.read"

    def __init__(self, client, forms, review):
        self.client, self.forms, self.review = client, forms, review
        self.last_handoff_facts = None

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

    @staticmethod
    def _contract_for(proposal):
        contract = RECORD_OPERATIONS.get(proposal.tool)
        if contract is not None:
            return contract
        if proposal.tool == "update_record" and isinstance(proposal.args, dict):
            entity = proposal.args.get("entity")
            for item in RECORD_CONTRACTS:
                if item.entity == entity:
                    return item
        return None

    def _general_values(self, proposal, *, context, observation):
        if proposal.tool == "crm_contact_create" or not isinstance(proposal.args, dict):
            return None
        args = proposal.args
        body = args.get("body")
        entity = args.get("entity")
        if not isinstance(body, dict) or not isinstance(entity, str) or not entity:
            return None
        actor = str(args.get("actor", "") or "")
        principal = getattr(context, "principal", None) or getattr(getattr(context, "operation", None), "principal", None)
        actor_id = str(getattr(principal, "actor_id", "") or "")
        if actor and actor_id and actor != actor_id:
            return None
        if "ownerUserId" in body and actor_id and str(body["ownerUserId"]) != actor_id:
            return None
        route = getattr(observation, "page_route", "") or ""
        if (observation.render_state != "ready"
                or not route
                or urlsplit(observation.url).path != route
                or not self.review.permits("fields", route)):
            return None
        contract = self._contract_for(proposal)
        if contract is None or contract.entity != entity:
            return None
        if any(DEFAULT_SECRET_FIELD_PATTERN.search(str(k)) for k in body):
            return None
        allowed = set(contract.fields) | set(contract.required) | {r.field for r in contract.relations} | {"id"}
        if set(body) - allowed:
            return None
        dialog_controls = [
            e for e in observation.elements
            if str(e.attrs.get("form_scope", "")).startswith("dialog")
        ]
        mode = ("update" if (proposal.tool.endswith("_update")
                             or proposal.tool == "update_record"
                             or bool(args.get("before")))
                else "create")
        openers = UPDATE_WORDS if mode == "update" else CREATE_WORDS
        opener_btn = None
        if not dialog_controls:
            matching_btns = [
                e for e in observation.elements
                if e.role == "button"
                and plain(e.name) in {plain(w) for w in openers}
                and not str(e.attrs.get("form_scope", "")).startswith("dialog")
                and not form_control_unavailable(e.attrs)
                and self.review.permits("form_entry", route, e.name.strip())
            ]
            if len(matching_btns) != 1:
                return None
            opener_btn = matching_btns[0]
        labels_fn = getattr(self.review, "labels_on", None)
        reviewed_labels = labels_fn(route) if callable(labels_fn) else {}
        if not isinstance(reviewed_labels, dict):
            reviewed_labels = {}
        choice_labels = {
            str(item["field"]): str(item["label"])
            for item in (args.get("choices") or ())
            if isinstance(item, dict) and item.get("field") and item.get("label")
        }
        relation_names = {
            item["id"]: str(item["name"])
            for item in (args.get("relations") or ())
            if isinstance(item, dict) and "id" in item and item.get("name")
        }
        field_specs = []
        for key, raw_val in body.items():
            if key in {"id", "ownerUserId"}:
                continue
            if key in choice_labels:
                text_val = choice_labels[key]
            elif isinstance(raw_val, bool):
                text_val = ("开启" if raw_val else "关闭") if key == "status" else ("是" if raw_val else "否")
            elif key == "status" and str(raw_val) in {"0", "1"}:
                text_val = "开启" if str(raw_val) == "0" else "关闭"
            elif isinstance(raw_val, (str, int, float)) and not isinstance(raw_val, bool):
                if key.endswith("Id") and raw_val in relation_names:
                    text_val = relation_names[raw_val]
                else:
                    text_val = str(raw_val)
            else:
                return None
            label = str(reviewed_labels.get(key) or _COMMON_FIELD_LABELS.get(key) or "").strip()
            if not label and dialog_controls:
                for el in dialog_controls:
                    fl = str(el.attrs.get("field_label") or el.name or "").strip()
                    if fl and plain(fl) == plain(key):
                        label = fl
                        break
            if not label:
                return None
            field_specs.append((key, label, text_val))
        if not 1 <= len(field_specs) <= 32:
            return None
        record_id = body.get("id")
        target = f"{route}#{entity}" + (f"/{record_id}" if record_id not in (None, "") else "")
        return {
            "route": route,
            "entity": entity,
            "mode": mode,
            "target": target[:300],
            "opener": opener_btn.name.strip() if opener_btn is not None else "",
            "fields": tuple(field_specs),
        }

    def offer(self, proposal, *, context, observation):
        values = self._values(proposal)
        if values is not None:
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
        general = self._general_values(proposal, context=context, observation=observation)
        if general is None:
            return None
        context.require_live(application_id="yudao", required_scope=self.required_scope)
        offer_pairs = tuple((key, text_val) for key, _label, text_val in general["fields"])
        return MutationPrefillOffer(
            proposal.content_digest,
            context.binding_digest,
            general["target"],
            offer_pairs,
        )

    def _handoff_general(self, offer, proposal, *, context, general):
        route = general["route"]
        expected_pairs = {key: text_val for key, _label, text_val in general["fields"]}
        if dict(offer.fields) != expected_pairs or offer.target != general["target"]:
            return _refused("表单预填草稿与审批内容不一致，请重新核对当前表单。")
        current = context.obs
        dialog_controls = [
            e for e in current.elements
            if str(e.attrs.get("form_scope", "")).startswith("dialog")
        ]
        if not dialog_controls and general["opener"]:
            openers = [
                e for e in current.elements
                if e.role == "button"
                and e.name.strip() == general["opener"]
                and not str(e.attrs.get("form_scope", "")).startswith("dialog")
                and not form_control_unavailable(e.attrs)
                and self.review.permits("form_entry", route, e.name.strip())
            ]
            if len(openers) != 1:
                return _refused("未找到唯一的表单入口按钮，未填写。")
            opened = context.page.execute("click", {"ref": openers[0].ref})
            if _receipt_error_code(opened) == "form_closed_or_readonly":
                return _form_closed_or_readonly_result()
            if opened.state != "completed" or opened.receipt is None:
                return _refused("打开表单失败，未继续预填。")
            current = context.obs
        before = current
        applied, rejected, previous, restricted, expected = {}, {}, {}, [], []
        pending = []
        for _key, label, value in general["fields"]:
            matches = [
                e for e in context.obs.elements
                if str(e.attrs.get("form_scope", "")).startswith("dialog")
                and plain(e.attrs.get("field_label") or e.name) == plain(label)
            ]
            if len(matches) != 1:
                pending.append(label)
                continue
            element = matches[0]
            if _is_restricted_element(element):
                restricted.append(label)
                pending.append(label)
                continue
            prev_val = str(
                element.attrs.get("selected_option")
                or element.attrs.get("value")
                or ""
            )
            action = "select" if element.role in {"combobox", "listbox"} else "fill"
            args = {"ref": element.ref, "value": value}
            if action == "select":
                args["option_label"] = value
            else:
                args["commit"] = True
            outcome = context.page.execute(action, args)
            if _receipt_error_code(outcome) == "form_closed_or_readonly":
                _record_field_outcomes(context, applied=applied, rejected={**rejected, label: "form_closed_or_readonly"},
                                       previous=previous, restricted=restricted)
                _attach_handoff_facts(context, applied_fields=applied, rejected_fields=rejected,
                                      restricted_controls=restricted, pending_fields=[*pending, label])
                return _form_closed_or_readonly_result()
            _collect_receipt_outcomes(outcome, label, value, prev_val, applied, rejected, previous, restricted)
            if outcome.state != "completed":
                pending.append(label)
                continue
            expected.append((field_identity(element), value))
        _record_field_outcomes(context, applied=applied, rejected=rejected, previous=previous, restricted=restricted)
        self.last_handoff_facts = _attach_handoff_facts(
            context, applied_fields=applied, rejected_fields=rejected,
            restricted_controls=restricted, pending_fields=pending)
        if not expected:
            return _refused("没有成功填入任何表单字段，请核对当前表单。")
        submits = [
            e for e in context.obs.elements
            if str(e.attrs.get("form_scope", "")).startswith("dialog")
            and e.role == "button"
            and plain(e.name) in SUBMIT_WORDS
        ]
        submit_label = submits[0].name if len(submits) == 1 else "确定"
        return handoff_draft(context, before, expected, submit_label)

    def handoff(self, offer, proposal, *, context):
        offer.require(proposal, context.operation)
        context.operation.require_live(application_id="yudao", required_scope=self.required_scope)
        values = self._values(proposal)
        if values is None:
            general = self._general_values(proposal, context=context, observation=context.obs)
            if general is None:
                return _refused()
            return self._handoff_general(offer, proposal, context=context, general=general)
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
        if _receipt_error_code(opened) == "form_closed_or_readonly":
            self.forms.discard_entry(entry, context=context.operation)
            return _form_closed_or_readonly_result()
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
        applied, rejected, previous, restricted = {}, {}, {}, []
        for index, item in enumerate(draft.fields):
            for attempt in range(2):
                matches = [e for e in context.obs.elements
                           if e.attrs.get("form_scope") == form.control_scope
                           and e.attrs.get("field_label") == item.label and e.role == item.control]
                if len(matches) != 1:
                    return _refused()
                element = matches[0]
                if _is_restricted_element(element):
                    restricted.append(item.label)
                    _record_field_outcomes(context, applied=applied, rejected=rejected,
                                           previous=previous, restricted=restricted)
                    return _refused()
                prev_val = str(
                    element.attrs.get("selected_option")
                    or element.attrs.get("value")
                    or ""
                )
                args = {"ref": element.ref, "value": item.value}
                if item.option_label:
                    args["option_label"] = item.option_label
                else:
                    args["commit"] = True
                outcome = context.page.execute("select" if item.option_label else "fill", args)
                if _receipt_error_code(outcome) == "form_closed_or_readonly":
                    _record_field_outcomes(context, applied=applied,
                                           rejected={**rejected, item.label: "form_closed_or_readonly"},
                                           previous=previous, restricted=restricted)
                    _attach_handoff_facts(context, applied_fields=applied, rejected_fields=rejected,
                                          restricted_controls=restricted,
                                          pending_fields=[f.label for f in draft.fields[index:]])
                    return _form_closed_or_readonly_result()
                if (outcome.state == "completed" or attempt
                        or outcome.receipt is None
                        or outcome.receipt.error_code != "stale_observation"):
                    break
                if context.page.execute("observe", {}).state != "completed":
                    return _refused()
            _collect_receipt_outcomes(outcome, item.label, item.option_label or item.value,
                                      prev_val, applied, rejected, previous, restricted)
            if outcome.state != "completed":
                _record_field_outcomes(context, applied=applied, rejected=rejected,
                                       previous=previous, restricted=restricted)
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
        _record_field_outcomes(context, applied=applied, rejected=rejected,
                               previous=previous, restricted=restricted)
        self.last_handoff_facts = _attach_handoff_facts(
            context, applied_fields=applied, rejected_fields=rejected,
            restricted_controls=restricted, pending_fields=())
        return handoff_draft(context, before, expected, form.submit_label)
