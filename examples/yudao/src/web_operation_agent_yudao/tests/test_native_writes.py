"""Native writes bind actor, full effects, conflict check and partial outcomes."""
import copy
import time
import unittest

from web_operation_agent.business.errors import (BusinessMutationIndeterminate,
    BusinessMutationRefusedError, BusinessToolRequestError)
from web_operation_agent.state.identity import PrincipalContext, OperationContext
from web_operation_agent.goal.data import Ref
from web_operation_agent_yudao.writes import native_tools
from web_operation_agent.business.effects import canonical
from dataclasses import replace


def context():
    now = int(time.time() * 1000)
    actor = PrincipalContext(1, "yudao", "1", "7", "9", "synthetic", now-1000, now+60000,
                             ("yudao.read", "yudao.write"))
    return OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o", deadline_ms=now+30000)


class Client:
    tenant = "1"
    def __init__(self):
        self.records = {("crm/contact", 10): {"id": 10, "name": "林舟", "customerId": 20,
            "ownerUserId": 9, "mobile": "13800000001", "email": "lin@example.test", "remark": "保留备注",
            "contactLastTime": None, "contactNextTime": None, "contactLastContent": None},
            ("crm/customer", 20): {"id": 20, "name": "合成星河", "ownerUserId": 9,
                                  "contactLastTime": None, "contactNextTime": None,
                                  "contactLastContent": "之前", "followUpStatus": False}}
        self.writes = []
        self.partial = False
        self.dictionary = [{"dictType": "crm_follow_up_type", "label": "电话", "value": "1"}]
        self.areas = [{"id": 100, "name": "合成省", "children": [{"id": 101, "name": "合成市"}]}]
    def get(self, path, **params):
        if path == "system/dict-data/simple-list": return copy.deepcopy(self.dictionary)
        if path == "system/area/tree": return copy.deepcopy(self.areas)
        if path == "system/auth/get-permission-info": return {"user": {"id": 9, "nickname": "合成员工"}}
        if path == "crm/permission/list": return [{"userId": 9, "level": 1}]
        if path == "crm/follow-up-record/page":
            return {"list": [copy.deepcopy(row) for (entity, _), row in self.records.items()
                             if entity == "crm/follow-up-record" and row["bizId"] == params["bizId"]]}
        return copy.deepcopy(self.records.get((path.removesuffix("/get"), int(params["id"]))))
    def write_once(self, method, path, *, body):
        self.writes.append((method, path, copy.deepcopy(body)))
        if path.endswith("/update"):
            self.records[(path.removesuffix("/update"), body["id"])].update(body)
            return True
        entity, record_id = path.removesuffix("/create"), 30
        self.records[(entity, record_id)] = {"id": record_id, **body}
        if entity == "crm/follow-up-record":
            if self.partial:
                raise BusinessMutationIndeterminate("synthetic failure after insert", phase="dispatch")
            target = {1: "crm/clue", 2: "crm/customer", 3: "crm/contact", 4: "crm/business", 5: "crm/contract"}[body["bizType"]]
            # Model the native services independently of the product contract.
            changes = {"contactLastTime": int(time.time() * 1000)}
            if target != "crm/contract":
                changes["contactNextTime"] = body["nextTime"]
            if target in {"crm/customer", "crm/clue", "crm/contact"}:
                changes["contactLastContent"] = body["content"]
            if target in {"crm/customer", "crm/clue", "crm/business"}:
                changes["followUpStatus"] = True
            self.records[(target, body["bizId"])].update(changes)
            for key, entity in (("contactIds", "crm/contact"), ("businessIds", "crm/business")):
                for related_id in body[key]:
                    self.records[(entity, related_id)]["contactNextTime"] = body["nextTime"]
        return record_id


class Data:
    def __init__(self, client): self.client, self.invalidated = client, []
    def find(self, entity, name, *, limit):
        return tuple(Ref(entity, str(id), row["name"], row["name"]) for (kind, id), row in self.client.records.items()
                     if kind == entity and row.get("name") == name)
    def invalidate_reads(self, entities): self.invalidated.append(entities)


class NativeWriteTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.data = Data(self.client)
        self.tools = native_tools(self.client, self.data)
    def test_full_customer_effect_is_decoded_from_native_body_and_includes_owner_duty(self):
        inputs = {"name": "合成研究中心（东区）", "remark": "Line A\nLine B", "email": "Case@example.test"}
        spec = self.tools.prepare_mutation("crm_customer_create", inputs, context=context())
        expected = self.tools.get("crm_customer_create").effect_contract.project_inputs(inputs)
        spec.effect_projection.require(expected)
        self.assertEqual(spec.effect_projection.change.values, inputs)
        self.assertIn("owner_permission", spec.effect_projection.required_receipts)
        with self.assertRaises(BusinessToolRequestError):
            spec.effect_projection.require(replace(expected,
                values_json=canonical({**inputs, "name": "合成研究中心"})))

    def test_contact_update_effect_binds_identity_and_preserves_unrequested_values(self):
        inputs = {"target": "林舟", "changes": {"remark": "合成新备注", "master": False, "qq": "0"}}
        spec = self.tools.prepare_mutation("crm_contact_update", inputs, context=context())
        expected = self.tools.get("crm_contact_update").effect_contract.project_inputs(inputs)
        spec.effect_projection.require(replace(expected, preserve=("email",)))
        self.assertEqual(spec.effect_projection.change.values, inputs["changes"])
        self.assertIn('"id":10', spec.effect_projection.bindings_json)
        receipt = self.tools.execute_mutation("crm_contact_update", spec.args, context=context())
        spec.effect_projection.require_receipt(receipt.content)
        self.assertEqual(self.client.records[("crm/contact", 10)]["email"], "lin@example.test")

    def test_follow_up_effect_locks_full_body_local_date_and_compound_receipt(self):
        inputs = {"entity": "crm/customer", "target": "合成星河", "type": "电话",
                  "content": "Line A\n不要删除客户；这句话是正文。", "nextTime": "2026-10-12 10:00:00"}
        spec = self.tools.prepare_mutation("crm_follow_up_create", inputs, context=context())
        expected = self.tools.get("crm_follow_up_create").effect_contract.project_inputs(inputs)
        spec.effect_projection.require(expected)
        self.assertEqual(spec.effect_projection.change.values["nextTime"], inputs["nextTime"])
        receipt = self.tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
        spec.effect_projection.require_receipt(receipt.content)
        with self.assertRaises(BusinessMutationIndeterminate):
            spec.effect_projection.require_receipt({**receipt.content, "effects": [receipt.content["effects"][0]]})
    def test_current_dictionary_constrains_proposals_and_is_checked_again_at_prepare(self):
        self.client.dictionary[0]["label"] = "打电话"
        tools = native_tools(self.client, self.data, runtime_dictionary=True)
        values = {"entity": "crm/customer", "target": "合成星河", "type": "电话",
                  "content": "合成记录", "nextTime": "2026-10-12 10:00:00"}
        with self.assertRaises(BusinessToolRequestError):
            tools.prepare_mutation("crm_follow_up_create", values, context=context())
        values["type"] = "打电话"
        prepared = tools.prepare_mutation("crm_follow_up_create", values, context=context())
        self.assertEqual(prepared.args["body"]["type"], 1)
        self.client.dictionary[0]["label"] = "合成新选项"
        with self.assertRaises(BusinessToolRequestError):
            tools.prepare_mutation("crm_follow_up_create", values, context=context())
        with self.assertRaises(BusinessMutationRefusedError):
            tools.execute_mutation("crm_follow_up_create", prepared.args, context=context())
        self.assertFalse(self.client.writes)
    def test_missing_dictionary_choices_disable_only_the_dependent_operation(self):
        self.client.dictionary = []
        tools = native_tools(self.client, self.data, runtime_dictionary=True)
        self.assertNotIn("crm_follow_up_create", {t.name for t in tools.definitions})
        self.assertNotIn("system_notice_create", {t.name for t in tools.definitions})
        self.assertIsNotNone(tools.get("erp_supplier_create"))
        self.assertNotIn("industryId", tools.get("crm_customer_create").schema["function"]["parameters"]["properties"])
    def test_create_binds_owner_to_actor_and_checks_record(self):
        prepared = self.tools.prepare_mutation("crm_customer_create", {"name": "合成海风"}, context=context())
        self.assertEqual(prepared.args["body"], {"name": "合成海风", "ownerUserId": 9})
        self.assertFalse(self.client.writes)
        result = self.tools.execute_mutation("crm_customer_create", prepared.args, context=context())
        self.assertTrue(result.content["verified"])
        self.assertEqual(result.content["reference"], "合成海风（ID=30）")
        self.assertEqual(len(self.client.writes), 1)

    def test_customer_full_fields_are_encoded_and_dictionary_drift_blocks_dispatch(self):
        self.client.dictionary += [{"dictType": "crm_customer_industry", "label": "合成制造", "value": "5"},
            {"dictType": "crm_customer_level", "label": "合成重点", "value": "1"},
            {"dictType": "crm_customer_source", "label": "合成官网", "value": "2"}]
        inputs = {"name": "合成新档案", "industryId": "合成制造", "level": "合成重点",
            "source": "合成官网", "qq": "001234", "areaId": "合成省/合成市",
            "contactNextTime": "2026-10-12 10:00:00"}
        prepared = self.tools.prepare_mutation("crm_customer_create", inputs, context=context())
        self.assertEqual({k: prepared.args["body"][k] for k in inputs if k != "name"},
            {"industryId": 5, "level": 1, "source": 2, "qq": "001234", "areaId": 101,
             "contactNextTime": 1791770400000})
        labels = {d.label: d.text for d in prepared.details}
        self.assertEqual(labels["industryId"], "合成制造")
        self.assertEqual(labels["areaId"], "合成省/合成市")
        self.client.dictionary[-1]["value"] = "99"
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("crm_customer_create", prepared.args, context=context())
        self.assertFalse(self.client.writes)

    def test_contact_creation_binds_customer_and_checks_owner_permission(self):
        prepared = self.tools.prepare_mutation("crm_contact_create", {"name": "合成新联系人",
            "customer": "合成星河", "qq": "12345", "master": False}, context=context())
        self.assertEqual(prepared.args["body"], {"name": "合成新联系人", "customerId": 20,
            "ownerUserId": 9, "qq": 12345, "master": False})
        result = self.tools.execute_mutation("crm_contact_create", prepared.args, context=context())
        self.assertTrue(result.content["verified"])
        self.assertEqual(len(result.content["effects"]), 2)
        original = self.client.get
        self.client.get = lambda path, **kw: [] if path == "crm/permission/list" else original(path, **kw)
        with self.assertRaises(BusinessMutationIndeterminate):
            self.tools.execute_mutation("crm_contact_create", prepared.args, context=context())
        self.assertEqual(len(self.client.writes), 2)

    def test_region_ambiguity_and_relation_drift_fail_before_dispatch(self):
        self.client.areas.append({"id": 200, "name": "合成市"})
        with self.assertRaises(BusinessToolRequestError):
            self.tools.prepare_mutation("crm_customer_create", {"name": "合成档案", "areaId": "合成市"}, context=context())
        prepared = self.tools.prepare_mutation("crm_customer_create",
            {"name": "合成档案", "areaId": "合成省/合成市"}, context=context())
        self.client.areas[0]["children"][0]["id"] = 102
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("crm_customer_create", prepared.args, context=context())
        prepared = self.tools.prepare_mutation("crm_contact_create",
            {"name": "合成联系人", "customer": "合成星河"}, context=context())
        self.client.records[("crm/customer", 20)]["name"] = "合成变化"
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("crm_contact_create", prepared.args, context=context())
        self.assertFalse(self.client.writes)

    def test_customer_update_uses_minimal_fields_and_rejects_unreviewed_effects(self):
        prepared = self.tools.prepare_mutation("crm_customer_update", {"target": "合成星河",
            "changes": {"name": "合成星河新名"}}, context=context())
        self.assertEqual(prepared.args["body"], {"id": 20, "name": "合成星河新名", "ownerUserId": 9})
        self.assertTrue(self.tools.execute_mutation("crm_customer_update", prepared.args, context=context()).content["verified"])
        for operation, changes in (("crm_customer_update", {"ownerUserId": 8}),
                                   ("crm_contact_update", {"businessId": 100}),
                                   ("erp_warehouse_update", {"defaultStatus": True})):
            with self.subTest(operation=operation), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare_mutation(operation, {"target": "合成星河", "changes": changes}, context=context())

    def test_erp_create_update_and_preservation_share_reviewed_contract(self):
        for prefix, inputs, changes in (("erp_supplier", {"name": "合成供应商", "sort": 3,
                "status": "开启", "bankAccount": "0012345", "taxPercent": 13.25}, {"status": "关闭"}),
            ("erp_customer", {"name": "合成销售客户", "sort": 8, "status": "开启",
                "contact": "合成客户联络员", "bankAccount": "000987", "remark": "保留客户备注"},
                {"contact": "合成新客户联络员", "taxPercent": 0}),
            ("erp_warehouse", {"name": "合成仓库", "sort": 4, "status": "开启", "principal": "合成人员",
                "warehousePrice": 0.25, "truckagePrice": 2.5}, {"warehousePrice": 0})):
            with self.subTest(prefix=prefix):
                prepared = self.tools.prepare_mutation(prefix + "_create", inputs, context=context())
                self.assertEqual(prepared.args["body"]["status"], 0)
                self.tools.execute_mutation(prefix + "_create", prepared.args, context=context())
                entity = prefix.replace("_", "/")
                before = copy.deepcopy(self.client.records[(entity, 30)])
                prepared = self.tools.prepare_mutation(prefix + "_update",
                    {"target": inputs["name"], "changes": changes}, context=context())
                self.assertEqual(set(prepared.args["body"]), {"id", "name", "sort", "status", *changes})
                self.tools.execute_mutation(prefix + "_update", prepared.args, context=context())
                after = self.client.records[(entity, 30)]
                self.assertEqual({k: after[k] for k in before if k not in changes},
                                 {k: before[k] for k in before if k not in changes})

    def test_invalid_contact_qq_and_missing_erp_requirements_do_not_prepare(self):
        for qq in ("00123", "9223372036854775808", "-1"):
            with self.subTest(qq=qq), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare_mutation("crm_contact_update", {"target": "林舟", "changes": {"qq": qq}}, context=context())
        for prefix in ("erp_supplier", "erp_warehouse", "erp_customer"):
            with self.subTest(prefix=prefix), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare_mutation(prefix + "_create", {"name": "合成档案"}, context=context())
        self.assertFalse(self.client.writes)
    def test_definition_creates_have_no_membership_or_workflow_effect(self):
        for prefix, entity, inputs in (
            ("system_post", "system/post", {"name": "合成岗位", "code": "Synthetic.Post",
                "sort": 0, "status": "关闭", "remark": "完整备注\n保留大小写"}),
            ("member_tag", "member/tag", {"name": "合成标签"}),
            ("member_group", "member/group", {"name": "合成分组", "status": "开启"}),
            ("bpm_category", "bpm/category", {"name": "合成分类", "code": "Synthetic.Category",
                "status": "开启", "sort": 0, "description": "完整分类描述"})):
            with self.subTest(prefix=prefix):
                prepared = self.tools.prepare_mutation(prefix + "_create", inputs, context=context())
                expected = {**inputs}
                if "status" in expected:
                    expected["status"] = {"开启": 0, "关闭": 1}[expected["status"]]
                self.assertEqual(prepared.args["body"], expected)
                before = len(self.client.writes)
                result = self.tools.execute_mutation(prefix + "_create", prepared.args, context=context())
                self.assertTrue(result.content["verified"])
                self.assertEqual(self.client.writes[before:], [("POST", entity + "/create", expected)])
                self.assertNotIn(prefix + "_update", {t.name for t in self.tools.definitions})
                self.assertNotIn(prefix + "_delete", {t.name for t in self.tools.definitions})

    def test_definition_missing_values_lengths_and_extra_effects_fail_before_write(self):
        post = {"name": "合成岗位", "code": "SYNTHETIC", "sort": 0, "status": "开启"}
        category = {"name": "合成分类", "code": "SYNTHETIC", "sort": 0, "status": "开启"}
        invalid = [("system_post_create", {**post, "name": "长" * 51}),
                   ("system_post_create", {**post, "code": "A" * 65}),
                   ("system_post_create", {**post, "userIds": [9]}),
                   ("member_tag_create", {"name": "合成标签", "memberIds": [1]}),
                   ("member_group_create", {"name": "合成分组"}),
                   ("member_group_create", {"name": "合成分组", "status": "开启", "memberIds": [1]}),
                   ("bpm_category_create", {**category, "processDefinitionId": "synthetic"})]
        for operation, complete in (("system_post_create", post), ("bpm_category_create", category)):
            invalid.extend((operation, {k: v for k, v in complete.items() if k != missing})
                           for missing in complete)
        for operation, inputs in invalid:
            with self.subTest(operation=operation, inputs=inputs), self.assertRaises(BusinessToolRequestError):
                self.tools.prepare_mutation(operation, inputs, context=context())
        self.assertFalse(self.client.writes)
    def test_named_owner_is_a_request_constraint_and_never_silently_discarded(self):
        with self.assertRaisesRegex(BusinessToolRequestError, "current account"):
            self.tools.prepare_mutation("crm_customer_create",
                {"name": "合成海风", "owner": "其他合成人员"}, context=context())
        self.assertFalse(self.client.writes)
        prepared = self.tools.prepare_mutation("crm_customer_create",
            {"name": "合成海风", "owner": "合成员工"}, context=context())
        self.assertNotIn("owner", prepared.args["body"])
        result = self.tools.execute_mutation("crm_customer_create", prepared.args, context=context())
        self.assertTrue(result.content["verified"])
    def test_phone_update_is_minimal_and_current_required_fields_are_visible(self):
        prepared = self.tools.prepare_mutation("crm_contact_update",
            {"target": "林舟", "changes": {"mobile": "13800000002"}}, context=context())
        body = prepared.args["body"]
        self.assertEqual(set(body), {"id", "name", "customerId", "ownerUserId", "mobile"})
        self.assertEqual({d.label for d in prepared.details if d.kind == "preserved"}, {"name", "customerId", "ownerUserId"})
        self.tools.execute_mutation("crm_contact_update", prepared.args, context=context())
        self.assertEqual(self.client.records[("crm/contact", 10)]["remark"], "保留备注")
        self.assertEqual(self.client.records[("crm/contact", 10)]["email"], "lin@example.test")
    def test_changed_required_field_refuses_before_native_dispatch(self):
        prepared = self.tools.prepare_mutation("crm_contact_update",
            {"target": "林舟", "changes": {"mobile": "13800000002"}}, context=context())
        self.client.records[("crm/contact", 10)]["customerId"] = 21
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("crm_contact_update", prepared.args, context=context())
        self.assertFalse(self.client.writes)
    def test_acknowledged_update_cannot_verify_if_an_unrequested_value_changes(self):
        prepared = self.tools.prepare_mutation("crm_contact_update",
            {"target": "林舟", "changes": {"mobile": "13800000002"}}, context=context())
        dispatch = self.client.write_once
        def altered(method, path, *, body):
            result = dispatch(method, path, body=body)
            self.client.records[("crm/contact", 10)]["remark"] = "未批准的合成变化"
            return result
        self.client.write_once = altered
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            self.tools.execute_mutation("crm_contact_update", prepared.args, context=context())
        self.assertEqual(caught.exception.phase, "postcondition")
        self.assertEqual(caught.exception.reconciliation["unverified_effect"], "unrelated_field_preservation")
        self.assertEqual(len(self.client.writes), 1)
    def test_unrelated_change_during_approval_is_not_written_back(self):
        prepared = self.tools.prepare_mutation("crm_contact_update",
            {"target": "林舟", "changes": {"mobile": "13800000002"}}, context=context())
        self.client.records[("crm/contact", 10)]["remark"] = "审批期间的合成新值"
        result = self.tools.execute_mutation("crm_contact_update", prepared.args, context=context())
        self.assertTrue(result.content["verified"])
        self.assertEqual(self.client.records[("crm/contact", 10)]["remark"], "审批期间的合成新值")
    def test_follow_up_verifies_two_effects_and_never_reinserts_partial_failure(self):
        values = {"entity": "crm/customer", "target": "合成星河", "type": "电话",
                  "content": "完整合成跟进内容", "nextTime": "2026-10-12 10:00:00"}
        prepared = self.tools.prepare_mutation("crm_follow_up_create", values, context=context())
        self.assertEqual(prepared.args["body"]["nextTime"], 1791770400000)
        self.assertEqual(prepared.args["body"]["businessIds"], [])
        self.assertIn("2026-10-12 10:00:00 (Asia/Shanghai)", [d.text for d in prepared.details])
        result = self.tools.execute_mutation("crm_follow_up_create", prepared.args, context=context())
        self.assertEqual(len(result.content["effects"]), 2)
        self.client.partial = True
        with self.assertRaises(BusinessMutationIndeterminate):
            self.tools.execute_mutation("crm_follow_up_create", prepared.args, context=context())
        self.assertEqual(len(self.client.writes), 2)
        self.assertIn(("crm/follow-up-record", 30), self.client.records)
    def test_readback_mismatch_retains_id_for_settlement(self):
        prepared = self.tools.prepare_mutation("crm_customer_create", {"name": "合成海风"}, context=context())
        original = self.client.get
        self.client.get = lambda path, **params: {} if path == "crm/customer/get" else original(path, **params)
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            self.tools.execute_mutation("crm_customer_create", prepared.args, context=context())
        self.assertEqual(caught.exception.reconciliation["id"], 30)
