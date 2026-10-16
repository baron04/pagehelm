"""Real candidate/name/prepare composition over a synthetic native API."""
from copy import deepcopy
from dataclasses import replace
import unittest

from web_operation_agent.business.errors import BusinessToolReferenceError
from web_operation_agent.goal.ground import ground
from web_operation_agent.goal.narrow import KnownReference, locate_names
from web_operation_agent.schemas.browser import PageObservation
from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.page_goals import NativePageGoalPort
from web_operation_agent_yudao.ui_source import UiSource
from web_operation_agent_yudao.writes import native_tools
from .test_goal import DOCS, _Client
from .test_native_writes import context


class _Records(_Client):
    def __init__(self, *, name="合成联系人", owners=("客户A", "客户B")):
        super().__init__()
        self.records = [{"id": identifier, "name": name, "customerName": owner,
                         "customerId": 5, "ownerUserId": 9, "contactLastTime": None}
                        for identifier, owner in zip((11, 13), owners)]

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {**super().get(path, **params), "user": {"id": 9}, "roles": []}
        if path == "crm/permission/list":
            return [{"userId": 9, "level": 1}]
        if path == "system/dict-data/simple-list":
            return [{"dictType": "crm_follow_up_type", "label": "电话", "value": "1"}]
        if path == "crm/contract/page":
            rows = [r for r in self.records if not params.get("name") or params["name"] in r["name"]]
            return {"list": deepcopy(rows), "total": len(rows)}
        if path == "crm/contract/get":
            return deepcopy(next((r for r in self.records if r["id"] == int(params["id"])), None))
        return super().get(path, **params)


class NativeTargetReferenceTests(unittest.TestCase):
    def setup_records(self, **kwargs):
        self.client = _Records(**kwargs)
        self.data = YudaoGoalData(self.client, deepcopy(DOCS), ui=UiSource(
            records={"/crm/contract": "/crm/contract/detail/{id}"}))
        self.port = NativePageGoalPort(self.data)
        self.tools = native_tools(self.client, self.data)

    def prepare(self, reference):
        return self.tools.prepare_mutation("crm_follow_up_create", {
            "entity": "crm/contract", "target": reference, "type": "电话",
            "content": "合成跟进正文", "nextTime": "2026-10-12 10:00:00"}, context=context())

    def candidates(self, name):
        located = locate_names(self.port, (name,), entities=("crm/contract",), context=context())
        hints = located or (KnownReference(name, "crm/contract", name),)
        return ground(self.port, entities=("crm/contract",), known=hints, context=context())

    def test_owner_candidates_round_trip_to_the_exact_native_prepare_target(self):
        self.setup_records()
        candidates = self.candidates("合成联系人")
        self.assertEqual([c.display for c in candidates], ["合成联系人（客户A）", "合成联系人（客户B）"])
        self.assertEqual([self.prepare(c.display).args["body"]["bizId"] for c in candidates], [11, 13])

    def test_identical_names_and_owners_keep_id_suffixes_and_original_names(self):
        self.setup_records(owners=("同名客户", "同名客户"))
        candidates = self.candidates("合成联系人")
        self.assertEqual([c.display for c in candidates],
                         ["合成联系人（同名客户；ID=11）", "合成联系人（同名客户；ID=13）"])
        self.assertEqual([self.prepare(c.display).args["body"]["bizId"] for c in candidates], [11, 13])

    def test_parentheses_in_record_and_owner_names_survive_native_name_recheck(self):
        self.setup_records(name="合成（开发）部", owners=("客户（集团）", "客户（集团）"))
        candidates = self.candidates("合成（开发）部")
        self.assertEqual(len(candidates), 2)
        for candidate, identifier in zip(candidates, (11, 13)):
            self.assertEqual(self.data.find("crm/contract", candidate.display)[0].name, "合成（开发）部")
            self.assertEqual(self.prepare(candidate.display).args["body"]["bizId"], identifier)

    def test_bare_ambiguous_name_returns_current_choices_without_preparing_a_write(self):
        self.setup_records()
        with self.assertRaises(BusinessToolReferenceError) as error:
            self.prepare("合成联系人")
        self.assertEqual(error.exception.choices, ("合成联系人（客户A）", "合成联系人（客户B）"))

    def test_current_detail_candidate_round_trips_but_route_or_name_changes_do_not(self):
        self.setup_records()
        path = "/crm/contract/detail/13"
        observation = PageObservation(1, 2, "https://app.test" + path, "", [], [],
            render_state="ready", record_route=path, page_route=path)
        self.port.page_reader = lambda ctx: observation
        candidates = ground(self.port, entities=("crm/contract",), known=(), context=context())
        self.assertEqual(self.prepare(candidates[0].display).args["body"]["bizId"], 13)
        self.client.records[1]["name"] = "已更名"
        with self.assertRaises(BusinessToolReferenceError):
            self.prepare(candidates[0].display)
        self.port.page_reader = lambda ctx: replace(observation, record_route="")
        self.assertEqual(ground(self.port, entities=("crm/contract",), known=(), context=context()), ())


if __name__ == "__main__":
    unittest.main()
