"""Native target differences reach approval, preparation and postcondition together."""
import copy
import json
import time
import unittest
from unittest.mock import patch

from web_operation_agent.business.errors import BusinessMutationIndeterminate, BusinessToolContractError
from web_operation_agent_yudao.tests.test_native_writes import Client, Data, context
from web_operation_agent_yudao.writes.writes import native_tools


# Independent native shapes; contract intentionally has no next time or content.
TARGET_FIELDS = {
    "crm/customer": ("contactLastTime", "contactNextTime", "contactLastContent", "followUpStatus"),
    "crm/clue": ("contactLastTime", "contactNextTime", "contactLastContent", "followUpStatus"),
    "crm/contact": ("contactLastTime", "contactNextTime", "contactLastContent"),
    "crm/business": ("contactLastTime", "contactNextTime", "followUpStatus"),
    "crm/contract": ("contactLastTime",),
}


class FollowUpTargetTests(unittest.TestCase):
    def target(self, entity):
        client = Client()
        client.records[(entity, 70)] = {"id": 70, "name": "合成主对象", **{
            field: False if field == "followUpStatus" else None for field in TARGET_FIELDS[entity]}}
        tools = native_tools(client, Data(client))
        values = {"entity": entity, "target": "合成主对象", "type": "电话",
                  "content": "完整正文\n第二行", "nextTime": "2026-10-20 10:00:00"}
        return client, tools, values

    def test_all_five_native_shapes_use_exact_approved_and_verified_effects(self):
        for entity, fields in TARGET_FIELDS.items():
            with self.subTest(entity=entity):
                client, tools, values = self.target(entity)
                spec = tools.prepare_mutation("crm_follow_up_create", values, context=context())
                main = json.loads(spec.effect_projection.derived_json)["main_follow_up"]
                self.assertEqual(set(main), set(fields))
                self.assertEqual(main["contactLastTime"], "application_execution_time")
                if "contactNextTime" in fields:
                    self.assertEqual(main["contactNextTime"], spec.args["body"]["nextTime"])
                if "contactLastContent" in fields:
                    self.assertEqual(main["contactLastContent"], values["content"])
                text = next(d.text for d in spec.details if d.label == "主对象跟进信息")
                self.assertEqual("标记已跟进" in text, "followUpStatus" in fields)
                self.assertEqual("最近跟进内容" in text, "contactLastContent" in fields)
                self.assertEqual("下次联系时间" in text, "contactNextTime" in fields)
                receipt = tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
                spec.effect_projection.require_receipt(receipt.content)
                self.assertEqual(len(client.writes), 1)
                self.assertEqual(set(client.records[(entity, 70)]), {"id", "name", *fields})

    def test_unobservable_required_effect_refuses_before_approval_or_dispatch(self):
        for entity, fields in TARGET_FIELDS.items():
            for field in fields:
                with self.subTest(entity=entity, field=field):
                    client, tools, values = self.target(entity)
                    del client.records[(entity, 70)][field]
                    with self.assertRaises(BusinessToolContractError):
                        tools.prepare_mutation("crm_follow_up_create", values, context=context())
                    self.assertEqual(client.writes, [])

    def test_loss_of_readable_effect_after_approval_still_prevents_dispatch(self):
        client, tools, values = self.target("crm/contact")
        spec = tools.prepare_mutation("crm_follow_up_create", values, context=context())
        del client.records[("crm/contact", 70)]["contactLastContent"]
        with self.assertRaises(BusinessToolContractError):
            tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
        self.assertEqual(client.writes, [])

    def test_missing_written_effect_or_stale_timestamp_remains_indeterminate(self):
        for entity, fields in TARGET_FIELDS.items():
            for field in fields:
                with self.subTest(entity=entity, field=field):
                    client, tools, values = self.target(entity)
                    spec = tools.prepare_mutation("crm_follow_up_create", values, context=context())
                    original = client.write_once
                    def incomplete(*args, **kwargs):
                        record = original(*args, **kwargs)
                        client.records[(entity, 70)][field] = (
                            int(time.time() * 1000) - 60000 if field == "contactLastTime" else None)
                        return record
                    with patch.object(client, "write_once", side_effect=incomplete):
                        with self.assertRaises(BusinessMutationIndeterminate):
                            tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
                    self.assertEqual(len(client.writes), 1)

    def test_boolean_status_is_not_confirmed_by_an_integer(self):
        client, tools, values = self.target("crm/customer")
        spec = tools.prepare_mutation("crm_follow_up_create", values, context=context())
        original = client.get
        def wrong_type(path, **params):
            row = copy.deepcopy(original(path, **params))
            if path == "crm/customer/get":
                row["followUpStatus"] = 1
            return row
        with patch.object(client, "get", side_effect=wrong_type):
            with self.assertRaises(BusinessMutationIndeterminate):
                tools.execute_mutation("crm_follow_up_create", spec.args, context=context())
        self.assertEqual(len(client.writes), 1)
