"""Native state transitions, derived receivable plans and OA publication."""
from datetime import datetime, timedelta
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from web_operation_agent.business.effects import RequestedEffect, EffectProjection, canonical
from web_operation_agent.business.errors import (BusinessToolRequestError,
    BusinessToolReferenceError, BusinessToolPermissionError, BusinessMutationRefusedError,
    BusinessMutationIndeterminate)
from web_operation_agent.business.tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition
from web_operation_agent.execution.events import turn_interval
from web_operation_agent.schemas.approval_details import ApprovalDetail
from .write_definitions import object_schema, text, USER_RELATION, RECORD_OPERATIONS
from .write_inputs import encode, epoch, recheck_choices
from .write_relations import resolve_relation, recheck_relation


STATE_FIELDS = {"crm_customer_lock": "lockStatus", "crm_customer_deal_status": "dealStatus"}
STATE_PATHS = {"crm_customer_lock": "lock", "crm_customer_deal_status": "update-deal-status",
               "crm_customer_put_pool": "put-pool", "crm_customer_receive": "receive"}
AUDIENCE = "发布时当前账号负责部门及其子部门的全部用户（排除自己）"
SPECIAL_DICTIONARIES = {"crm_receivable_plan_create": (("returnType", "crm_receivable_return_type"),),
    "oa_announcement_create": (("type", "oa_announcement_type"), ("priority", "oa_priority"))}


def read_pages(client, path, **params):
    """A complete bounded native list; truncated/filtered absence is not proof."""
    rows, total = [], None
    for page in range(1, 9):
        result = client.get(path, **params, pageNo=page, pageSize=100)
        if (not isinstance(result, dict) or not isinstance(result.get("list"), list)
                or type(result.get("total")) is not int or not 0 <= result["total"] <= 800
                or total is not None and total != result["total"]):
            raise BusinessToolRequestError("native list is incomplete or exceeds its bound")
        total = result["total"]
        rows.extend(result["list"])
        if len(rows) >= total:
            break
    if (len(rows) != total or any(not isinstance(r, dict) or type(r.get("id")) is not int for r in rows)
            or len({r["id"] for r in rows}) != len(rows)):
        raise BusinessToolRequestError("native list identities are incomplete")
    return rows


class SpecialEffects:
    def __init__(self, operation): self.operation = operation
    def project_inputs(self, inputs):
        if self.operation in STATE_PATHS:
            values = ({STATE_FIELDS[self.operation]: inputs[STATE_FIELDS[self.operation]]}
                if self.operation in STATE_FIELDS else {"ownerUserId":
                    None if self.operation == "crm_customer_put_pool" else inputs.get("owner", "当前账号")})
            return RequestedEffect("crm/customer", "state_change", "crm/customer", inputs["target"], canonical(values))
        if self.operation == "crm_receivable_plan_create":
            values = {{"contract": "contractId", "owner": "ownerUserId"}.get(k, k): v for k, v in inputs.items()}
            return RequestedEffect("crm/receivable-plan", "create", values_json=canonical(values))
        if self.operation == "oa_announcement_create":
            return RequestedEffect("oa/announcement", "create", values_json=canonical(inputs))
        raise BusinessToolRequestError("special operation is not installed")


class NativeSpecialHandler:
    def __init__(self, base):
        self.base, self.client, self.operation, self.goal_data = base, base.client, base.operation, base.goal_data

    def _grants(self, record_id, biz_type=2):
        grants = self.client.get("crm/permission/list", bizType=biz_type, bizId=record_id)
        if not isinstance(grants, list) or any(not isinstance(g, dict) or "userId" not in g or "level" not in g for g in grants):
            raise BusinessToolRequestError("current data permissions are unavailable")
        return grants

    def _contacts(self, record_id):
        rows = read_pages(self.client, "crm/contact/page-by-customer", customerId=record_id)
        if len(rows) > 80 or any(r.get("customerId") != record_id or "ownerUserId" not in r for r in rows):
            raise BusinessToolRequestError("complete related contact ownership is unavailable")
        return sorted(({k: r[k] for k in ("id", "name", "customerId", "ownerUserId")} for r in rows), key=lambda r: r["id"])

    def _customer(self, display, *, pool):
        # Use the same actor's native list scene. In particular, losing detail
        # permission after put-pool is never interpreted as proof of success.
        told = re.fullmatch(r"(.+)（ID=([0-9]+)）", display)
        name = told.group(1) if told else display
        rows = read_pages(self.client, "crm/customer/page", name=name, pool=pool)
        matches = [r for r in rows if r.get("name") == name and
                   (not told or str(r["id"]) == told.group(2))]
        if len(matches) != 1:
            choices = tuple(f"{r['name']}（ID={r['id']}）" for r in rows if isinstance(r.get("name"), str))[:6]
            raise BusinessToolReferenceError("customer is missing or ambiguous in the requested native scene",
                entity="crm/customer", choices=choices)
        row = matches[0]
        if any(k not in row for k in ("name", "ownerUserId", "lockStatus", "dealStatus")):
            raise BusinessToolRequestError("customer state is incomplete")
        return row

    def _state(self, inputs, actor):
        receive = self.operation == "crm_customer_receive"
        record = self._customer(inputs["target"], pool=receive)
        grants = self._grants(record["id"])
        if not receive:
            levels = {str(g["level"]) for g in grants if str(g["userId"]) == str(actor["id"])}
            allowed = {"1", "3"} if self.operation == "crm_customer_deal_status" else {"1"}
            if not levels & allowed:
                raise BusinessToolPermissionError("customer transition requires the actor's native data permission")
        if self.operation in STATE_FIELDS:
            field = STATE_FIELDS[self.operation]
            if type(record[field]) is not bool or record[field] == inputs[field]:
                raise BusinessToolRequestError("customer already has the requested state or state is unavailable; no write needed")
            expected = {field: inputs[field]}
            body = {"id": record["id"], **expected} if field == "lockStatus" else {}
            params = {} if body else {"id": record["id"], field: inputs[field]}
            contacts = []
        else:
            if record["lockStatus"] is not False or (receive and record["dealStatus"] is not False):
                raise BusinessToolRequestError("current customer state forbids this pool transition")
            if bool(record["ownerUserId"]) == receive:
                raise BusinessToolRequestError("customer is not in the requested owner/pool state")
            if receive and inputs.get("owner", "当前账号") not in {"当前账号", actor.get("nickname"), actor.get("username")}:
                raise BusinessToolRequestError("receive only assigns the customer to the current account")
            contacts = self._contacts(record["id"])
            expected = {"ownerUserId": int(actor["id"]) if receive else None}
            body, params = {}, {"ids": [record["id"]]} if receive else {"id": record["id"]}
        preserved = {k: record[k] for k in RECORD_OPERATIONS["crm_customer_update"].preserved -
            set(expected) - ({"ownerTime"} if receive else set()) if k in record}
        details = [ApprovalDetail("change", k, f"{record[k]} → {v}") for k, v in expected.items()]
        duties = ["record", "unrelated_field_preservation"]
        derived = {}
        if self.operation == "crm_customer_put_pool":
            details += [ApprovalDetail("side_effect", "关联联系人负责人", "全部清空：" +
                        ("、".join(f"{r['name']}（ID={r['id']}）" for r in contacts) or "当前无联系人")),
                        ApprovalDetail("side_effect", "客户权限", "删除客户全部 OWNER 数据权限；其他数据权限保留。应用登记负责人变更历史。")]
            duties += ["related_owner_clear", "owner_permission_removal"]
            derived = {"contacts": contacts, "remove_customer_owner_permissions": True, "owner_history": "application_native"}
        elif receive:
            details += [ApprovalDetail("derived", "领取人", f"当前账号：{actor.get('nickname') or actor['id']}"),
                        ApprovalDetail("side_effect", "客户权限", "为当前账号建立 OWNER 权限和负责人变更历史；不恢复联系人负责人。")]
            duties += ["owner_permission", "related_owner_preservation"]
            # The native response does not expose ownerTime or owner history.
            # Their source rules are not a readback receipt for hidden metadata.
            derived = {"owner_history": "application_native_not_exposed", "contacts": contacts}
        values = dict(expected)
        if receive:
            values["ownerUserId"] = inputs.get("owner", "当前账号")
        return {"entity": "crm/customer", "body": body, "params": params, "expected": expected,
                "snapshot": record, "preserved": preserved, "contacts": contacts, "grants": grants,
                "pool_before": receive, "inputs": inputs}, details, duties, derived, values

    def _plans(self, contract):
        rows = read_pages(self.client, "crm/receivable-plan/page-by-customer",
            customerId=contract["customerId"], contractId=contract["id"])
        if any(r.get("contractId") != contract["id"] or type(r.get("period")) is not int or r["period"] < 1 for r in rows):
            raise BusinessToolRequestError("current contract plan periods are unavailable")
        return sorted(({"id": r["id"], "period": r["period"]} for r in rows), key=lambda r: r["id"])

    def _receivable(self, inputs, actor):
        contract = self.base._target("crm/contract", inputs["contract"])
        if type(contract.get("customerId")) is not int:
            raise BusinessToolRequestError("contract customer relation is unavailable")
        owner = resolve_relation(self.client, USER_RELATION, inputs["owner"])
        body, details, choices = encode(self.client, self.goal_data,
            {k: v for k, v in inputs.items() if k not in {"owner", "contract"}},
            dictionaries=SPECIAL_DICTIONARIES[self.operation])
        body["returnTime"] = epoch(inputs["returnTime"], self.goal_data)
        body.update(contractId=contract["id"], ownerUserId=owner["id"])
        plans = self._plans(contract)
        expected = {"customerId": contract["customerId"], "period": max((r["period"] for r in plans), default=0) + 1}
        if "remindDays" in inputs:
            zone = getattr(self.goal_data, "time_zone", "Asia/Shanghai")
            zone = zone() if callable(zone) else zone
            date = datetime.strptime(inputs["returnTime"], "%Y-%m-%d %H:%M:%S")
            expected["remindTime"] = int((date - timedelta(days=inputs["remindDays"])).replace(tzinfo=ZoneInfo(zone)).timestamp() * 1000)
        details += [ApprovalDetail("derived", "合同", inputs["contract"]),
            ApprovalDetail("derived", "负责人", inputs["owner"]),
            ApprovalDetail("side_effect", "计划派生", f"关联客户 ID={contract['customerId']}；按当前合同计划确定第 {expected['period']} 期；提醒日期按回款时间减提醒天数。"),
            ApprovalDetail("side_effect", "数据权限", "为指定负责人建立回款计划 OWNER 权限；不创建实际回款。")]
        declared = SpecialEffects(self.operation).project_inputs(inputs)
        values = self._decode(body, declared.values, choices)
        values["contractId"], values["ownerUserId"], values["returnTime"] = inputs["contract"], inputs["owner"], inputs["returnTime"]
        return {"entity": "crm/receivable-plan", "body": body, "params": {}, "expected": expected,
            "relations": [owner, {"entity": "crm/contract", "id": contract["id"], "name": contract["name"], "customerId": contract["customerId"]}],
            "contract": contract, "plans": plans, "choices": choices}, details, ["record", "plan_derivation", "owner_permission"], expected, values

    def _audience(self, actor):
        departments = self.client.get("system/dept/list")
        if not isinstance(departments, list) or len(departments) > 20000 or any(
                not isinstance(d, dict) or type(d.get("id")) is not int or "parentId" not in d or "leaderUserId" not in d for d in departments):
            raise BusinessToolRequestError("complete department publication rule is unavailable")
        ids = {d["id"] for d in departments if str(d["leaderUserId"]) == str(actor["id"])}
        for _ in range(16):
            added = {d["id"] for d in departments if d["parentId"] in ids} - ids
            if not added: break
            ids |= added
        else:
            raise BusinessToolRequestError("publication hierarchy exceeds its bound")
        users = read_pages(self.client, "system/user/page")
        return sorted(u["id"] for u in users if u.get("deptId") in ids and str(u["id"]) != str(actor["id"]))

    def _announcement(self, inputs, actor):
        body, details, choices = encode(self.client, self.goal_data,
            {k: v for k, v in inputs.items() if k != "audience"}, dictionaries=SPECIAL_DICTIONARIES[self.operation])
        receivers = self._audience(actor)
        details += [ApprovalDetail("side_effect", "发布受众", AUDIENCE + "；在派发时重新计算，不是固定名单。"),
                    ApprovalDetail("side_effect", "接收关系", "创建公告接收关系，接收人可在公告列表查看；不额外发送站内信，不标记已读。")]
        declared = SpecialEffects(self.operation).project_inputs(inputs)
        values = self._decode(body, declared.values, choices)
        values["audience"] = inputs["audience"]
        return {"entity": "oa/announcement", "body": body, "params": {}, "choices": choices,
            "expected": {}, "receivers": receivers}, details, ["record", "receiver_relations"], {"audience_rule": AUDIENCE}, values

    @staticmethod
    def _decode(body, values, choices):
        decoded = {k: body[k] for k in values if k in body}
        for c in choices:
            if body[c["field"]] != c["code"]:
                raise BusinessToolRequestError("prepared dictionary code differs from its bound choice")
            decoded[c["field"]] = c["label"]
        return decoded

    def prepare(self, inputs, *, context=None):
        actor = self.base._actor(context)
        if self.operation in STATE_PATHS:
            private, details, duties, derived, values = self._state(inputs, actor)
            snapshot = private["snapshot"]
            bindings = [{"entity": "crm/customer", "id": snapshot["id"], "name": snapshot["name"]}]
            targets = [f"{snapshot['name']}（ID={snapshot['id']}）"]
            preserved = private["preserved"]
        else:
            private, details, duties, derived, values = (self._receivable(inputs, actor)
                if self.operation == "crm_receivable_plan_create" else self._announcement(inputs, actor))
            bindings, preserved = private.get("relations", []), {}
            targets = [inputs.get("title", inputs.get("contract", ""))]
        for field, value in inputs.items():
            if field != "target" and not any(d.label == field for d in details):
                details.append(ApprovalDetail("body" if field == "content" else "change", field, str(value)))
        private.update(actor=str(actor["id"]), operation=self.operation)
        declared = SpecialEffects(self.operation).project_inputs(inputs)
        change = RequestedEffect(declared.entity, declared.action, declared.target_entity, declared.target, canonical(values))
        key = hashlib.sha256(canonical(private).encode()).hexdigest()
        return BusinessMutationSpec(private, {
            "crm_customer_put_pool": "客户放入公海并清除关联负责人及 OWNER 权限",
            "crm_customer_receive": "领取公海客户给当前账号并建立 OWNER 权限",
            "crm_customer_lock": "变更客户锁定状态", "crm_customer_deal_status": "变更客户成交状态",
            "crm_receivable_plan_create": "新建合同回款计划及负责人权限",
            "oa_announcement_create": "发布 OA 公告并建立原生下属接收关系"}[self.operation], tuple(targets),
            details=tuple(details), reconcile_key=key, reconciliation={"entity": private["entity"], "operation": self.operation, "body_digest": key},
            effect_projection=EffectProjection(change, canonical(bindings), canonical(preserved), canonical(derived), tuple(duties),
                preserves_unmentioned=self.operation in STATE_PATHS))

    def dispatch(self, args, *, context=None):
        actor = self.base._actor(context)
        if str(actor["id"]) != args["actor"] or args["operation"] != self.operation:
            raise BusinessToolPermissionError("prepared transition has a different actor or operation")
        recheck_choices(self.client, args.get("choices", []))
        if self.operation in STATE_PATHS:
            current = self._customer(args["inputs"]["target"], pool=args["pool_before"])
            if any(current.get(k) != args["snapshot"].get(k) for k in ("id", "name", "ownerUserId", "lockStatus", "dealStatus")):
                raise BusinessMutationRefusedError("customer state changed after approval", reason="precondition_changed")
            if self._grants(current["id"]) != args["grants"] or (self.operation not in STATE_FIELDS and self._contacts(current["id"]) != args["contacts"]):
                raise BusinessMutationRefusedError("customer related effects changed after approval", reason="precondition_changed")
            preserved = {k: current[k] for k in args["preserved"] if k in current}
            record_id = current["id"]
            path, method = "crm/customer/" + STATE_PATHS[self.operation], "PUT"
        else:
            for relation in args.get("relations", []):
                if "list_path" in relation:
                    recheck_relation(self.client, relation)
                else:
                    current = self.client.get(relation["entity"] + "/get", id=relation["id"])
                    if not isinstance(current, dict) or any(current.get(k) != relation[k] for k in ("id", "name", "customerId")):
                        raise BusinessMutationRefusedError("contract relation changed after approval", reason="precondition_changed")
            if self.operation == "crm_receivable_plan_create" and self._plans(args["contract"]) != args["plans"]:
                raise BusinessMutationRefusedError("contract plan periods changed after approval", reason="precondition_changed")
            receivers = self._audience(actor) if self.operation == "oa_announcement_create" else []
            path, method, record_id, preserved = args["entity"] + "/create", "POST", None, {}
        uncertain_dispatch = False
        try:
            result = self.client.write_once(method, path, body=args["body"], params=args["params"],
                                            operation=self.operation)
        except BusinessMutationIndeterminate:
            # State transitions have a bound target ID and the checks below cover
            # both the requested state and their related effects. A create has no
            # attributable ID after an uncertain response.
            if self.operation not in STATE_PATHS:
                raise
            result = record_id
            uncertain_dispatch = True
        record_id = record_id if record_id is not None else result
        with turn_interval("verification"):
            try:
                if self.operation in STATE_PATHS:
                    pool = self.operation == "crm_customer_put_pool"
                    actual = self._customer(f"{args['snapshot']['name']}（ID={record_id}）", pool=pool)
                    expected = args["expected"]
                else:
                    actual = self.client.get(args["entity"] + "/get", id=record_id)
                    expected = {**args["body"], **args["expected"]}
                if not isinstance(actual, dict) or str(actual.get("id")) != str(record_id) or any(actual.get(k) != v for k, v in expected.items()):
                    raise BusinessMutationIndeterminate("native effect does not confirm every approved field", phase="postcondition", reconciliation={"id": record_id})
                effects = [{"effect": "record", "verified": True, "id": record_id}]
                if self.operation in STATE_PATHS:
                    if any(actual.get(k) != v for k, v in preserved.items()):
                        raise BusinessMutationIndeterminate("customer transition changed unrelated business fields", phase="postcondition")
                    effects.append({"effect": "unrelated_field_preservation", "verified": True})
                    if self.operation not in STATE_FIELDS:
                        contacts = self._contacts(record_id)
                        wanted = [{**c, "ownerUserId": None} for c in args["contacts"]] if pool else args["contacts"]
                        if contacts != wanted:
                            raise BusinessMutationIndeterminate("complete related contact ownership is not verified", phase="postcondition")
                        effects.append({"effect": "related_owner_clear" if pool else "related_owner_preservation", "verified": True})
                        grants = self._grants(record_id)
                        if pool:
                            if any(str(g["level"]) == "1" for g in grants) or grants != [g for g in args["grants"] if str(g["level"]) != "1"]:
                                raise BusinessMutationIndeterminate("customer OWNER permission removal is not verified", phase="postcondition")
                            effects.append({"effect": "owner_permission_removal", "verified": True})
                        else:
                            self._verify_owner(grants, actor["id"])
                            effects.append({"effect": "owner_permission", "verified": True})
                elif self.operation == "crm_receivable_plan_create":
                    self._verify_owner(self._grants(record_id, 8), args["body"]["ownerUserId"])
                    if actual.get("receivableId") is not None:
                        raise BusinessMutationIndeterminate("new plan unexpectedly links an actual receivable", phase="postcondition")
                    effects += [{"effect": "plan_derivation", "verified": True}, {"effect": "owner_permission", "verified": True}]
                else:
                    actual_receivers = actual.get("receiverUserIds")
                    if (str(actual.get("publisherUserId")) != str(actor["id"]) or not isinstance(actual_receivers, list)
                            or sorted(actual_receivers) != receivers or len(set(actual_receivers)) != len(actual_receivers)):
                        raise BusinessMutationIndeterminate("announcement receiver relations are not verified", phase="postcondition")
                    effects.append({"effect": "receiver_relations", "verified": True, "count": len(actual_receivers)})
            except BusinessMutationIndeterminate:
                raise
            except Exception as exc:
                raise BusinessMutationIndeterminate("native write acknowledged; same-actor readback failed", phase="postcondition",
                    reconciliation={"id": record_id, "entity": args["entity"]}) from exc
        self.goal_data.invalidate_reads(frozenset({args["entity"], "crm/contact"} if self.operation in STATE_PATHS else {args["entity"]}))
        if uncertain_dispatch:
            from web_operation_agent.business.dispatch_evidence import verified_after_readback
            verified_after_readback()
        name = actual.get("name") or actual.get("title") or "回款计划"
        return BusinessToolResult({"verified": True, "record_id": str(record_id), "entity": args["entity"],
            "reference": f"{name}（ID={record_id}）", "effects": effects}, {"verified": True, "operation": self.operation, "effects": effects})

    @staticmethod
    def _verify_owner(grants, owner):
        if not any(str(g["userId"]) == str(owner) and str(g["level"]) == "1" for g in grants):
            raise BusinessMutationIndeterminate("native OWNER permission is not verified", phase="postcondition")


def special_tools(client, goal_data, handler_factory, *, dictionary_rows=None):
    target = text(500, description="已有 CRM 客户的确切名称；同名使用当前完整候选引用，不填裸ID。")
    definitions = []
    for name in STATE_PATHS:
        fields = {"target": target}
        if name in STATE_FIELDS:
            fields[STATE_FIELDS[name]] = {"type": "boolean", "description": "用户明确要求的期望状态。"}
        if name == "crm_customer_receive":
            fields["owner"] = text(200, description="用户指定领取人时保留名称；只支持当前账号，其他人不能省略后执行。")
        schema = object_schema(fields, ("target", *([STATE_FIELDS[name]] if name in STATE_FIELDS else [])))
        definitions.append(ToolDefinition(name, {
            "crm_customer_put_pool": "客户放入公海：清空客户与全部联系人负责人，删除客户 OWNER 权限。",
            "crm_customer_receive": "单个公海客户领取给当前账号，建立客户 OWNER 权限，不恢复联系人负责人。",
            "crm_customer_lock": "锁定或解锁 CRM 客户；OWNER 权限，锁定上限由应用校验。",
            "crm_customer_deal_status": "变更 CRM 客户成交状态；不创建合同或回款。"}[name], json.dumps(schema), "write", "yudao.write",
            ToolOperation("crm_customer", "update"), NativeSpecialHandler(handler_factory(client, name, goal_data)),
            reference_inputs=("target",), input_fields=tuple((k, "crm/customer", "ownerUserId" if k == "owner" else k) for k in fields if k != "target"),
            effect_contract=SpecialEffects(name), scope_entities=("crm/customer", "crm/contact"),
            preservation_fields=tuple(sorted(RECORD_OPERATIONS["crm_customer_update"].preserved -
                ({STATE_FIELDS[name]} if name in STATE_FIELDS else {"ownerUserId", "ownerTime"}))), preserves_unmentioned=True))
    entries = (
        ("crm_receivable_plan_create", "crm/receivable-plan", {
            "contract": text(500, description="已有 CRM 合同确切名称或核实的完整候选引用。"),
            "owner": text(500, description="计划负责人当前名称或含部门显示名称。"),
            "returnTime": text(19, description="应用时区的计划回款时间 YYYY-MM-DD HH:MM:SS。"),
            "price": {"type": "number", "minimum": 0, "description": "计划回款金额，元。"},
            "returnType": text(100, description="当前回款方式字典标签。"),
            "remindDays": {"type": "integer", "minimum": 0, "maximum": 36500}, "remark": text()},
            ("contract", "owner", "returnTime", "price"), ("contract", "owner"), ("remark",),
            ("crm/receivable-plan", "crm/contract", "crm/customer", "system/user")),
        ("oa_announcement_create", "oa/announcement", {
            "title": text(255), "content": text(20000, description="完整正文原文。"),
            "type": text(100, description="当前 OA 公告类型标签。"), "priority": text(100, description="当前 OA 优先级标签。"),
            "top": {"type": "boolean"}, "url": text(512),
            "audience": {**text(50), "enum": ["原生下属规则"], "description": AUDIENCE + "；不支持固定名单。"}},
            ("title", "content", "type", "priority", "top", "audience"), (), ("title", "content", "url"),
            ("oa/announcement", "system/dept", "system/user")))
    for name, entity, fields, required, references, literals, scopes in entries:
        available = True
        if dictionary_rows is not None:
            for field, kind in SPECIAL_DICTIONARIES[name]:
                labels = list(dict.fromkeys(r["label"] for r in dictionary_rows if isinstance(r, dict)
                    and r.get("dictType") == kind and isinstance(r.get("label"), str) and 0 < len(r["label"]) <= 100))
                if len(labels) > 100:
                    raise BusinessToolRequestError("current special dictionary exceeds its bound")
                if labels: fields[field]["enum"] = labels
                elif field in required: available = False
                else: fields.pop(field)
        if not available: continue
        definitions.append(ToolDefinition(name, "新建合同回款计划及派生期数/提醒/负责人权限，不建立实际回款。" if name.startswith("crm") else
            "发布 OA 公告并建立发布时的原生下属接收关系；不额外发送站内信，不支持固定收件名单。",
            json.dumps(object_schema(fields, required)), "write", "yudao.write", ToolOperation(entity.replace("/", "_").replace("-", "_"), "create"),
            NativeSpecialHandler(handler_factory(client, name, goal_data)), reference_inputs=references,
            literal_inputs=literals, input_fields=tuple((k, entity, {"owner": "ownerUserId", "contract": "contractId"}.get(k, k)) for k in fields),
            effect_contract=SpecialEffects(name), scope_entities=scopes))
    return definitions
