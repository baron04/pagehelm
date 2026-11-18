"""Current-page grounding uses rendered identity and actor reads, never URL alone."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.ground import bind, ground
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.schemas.browser import PageObservation
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.goal_compat.page_goals import NativePageGoalPort
from web_operation_agent_yudao.rules.ui_source import ListView, UiSource
from .test_goal import DOCS, CONTRACTS, _Client


class CurrentRecordValuesTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.data = YudaoGoalData(self.client, deepcopy(DOCS), ui=UiSource(
            views={"crm/contract/index": ListView(columns=("name", "totalPrice"))},
            records={"/crm/contract": "/crm/contract/detail/{id}"}))
        self.port = NativePageGoalPort(self.data)
        self.path = "/crm/contract/detail/11"
        self.observation = PageObservation(1, 2, "https://app.test" + self.path, "Contract", [], [],
            render_state="ready", record_route=self.path, page_route=self.path)
        self.port.page_reader = lambda context: deepcopy(self.observation)

    def offered(self):
        return ground(self.port, entities=("crm/contract",), known=(), context=object())

    def test_current_record_is_exactly_bound_for_page_and_facts_without_list_read(self):
        offered = self.offered()
        self.assertEqual("云帆企业版合同（ID=11）", offered[0].display)
        self.assertIn("Currently rendered record", offered[0].detail)
        spec = GoalSpec.from_json({"deliverables": [
            {"kind": "page", "entity": "crm/contract", "record": "v1"},
            {"kind": "fact", "entity": "crm/contract", "record": "v1", "fields": ["合同金额"]}]})
        spec, count, unknown = bind(spec, offered)
        self.assertEqual((2, ()), (count, unknown))
        result = self.port.resolve(spec)
        self.assertEqual(self.path, result.deliverables[0].page.url)
        self.assertIn("29800", result.deliverables[1].facts[0].value.replace(",", ""))
        self.assertFalse(any(path == "crm/contract/page" for path, _params in self.client.calls))

    def test_duplicate_names_do_not_select_the_other_record(self):
        other = {**CONTRACTS[1][0], "id": 99, "totalPrice": 1.0}
        with patch.object(self.data, "_fetch", return_value=[CONTRACTS[1][0], other]):
            offered = self.offered()
            spec, _count, _unknown = bind(GoalSpec.from_json({"deliverables": [
                {"kind": "page", "entity": "crm/contract", "record": "v1"}]}), offered)
            self.assertEqual(self.path, self.port.resolve(spec).deliverables[0].page.url)

    def test_url_title_or_global_controls_do_not_establish_current_record(self):
        for changes in ({"record_route": ""}, {"page_route": ""},
                        {"record_route": "/crm/contract/detail/12"},
                        {"page_route": "/crm/contract"}, {"render_state": "pending"},
                        {"render_state": "blocked"}, {"render_state": "unknown"}):
            with self.subTest(changes=changes):
                self.port.page_reader = lambda context: replace(self.observation, **changes)
                with patch.object(self.data, "read", side_effect=AssertionError("unqualified page")):
                    self.assertEqual((), self.offered())

    def test_page_change_during_read_invalidates_current_candidate(self):
        for changes in ({"epoch": 3}, {"content": "New content"},
                        {"page_route": ""}, {"render_state": "pending"},
                        {"url": "https://app.test/crm/contract/detail/12"}):
            with self.subTest(changes=changes):
                snapshots = iter((deepcopy(self.observation), replace(self.observation, **changes)))
                self.port.page_reader = lambda context: next(snapshots)
                self.assertEqual((), self.offered())

    def test_in_place_snapshot_change_also_invalidates_candidate(self):
        self.port.page_reader = lambda context: self.observation
        read = self.data.read
        def changed(*args):
            row = read(*args)
            self.observation.content = "Another rendering"
            return row
        with patch.object(self.data, "read", changed):
            self.assertEqual((), self.offered())

    def test_undeclared_entity_or_revoked_page_cannot_offer_current_record(self):
        with patch.object(self.data, "read", side_effect=AssertionError("not an actor page")):
            self.assertEqual((), self.port.candidates(entities=("erp/supplier",)))
            index = self.port.index()
            with patch.object(self.port, "index", return_value=replace(index, pages=())):
                self.assertEqual((), self.offered())

    def test_read_failure_and_expired_document_do_not_become_current_hint(self):
        failure = DataRefused("Actor read refused", read_failed=True)
        with patch.object(self.data, "read", side_effect=failure):
            with self.assertRaises(DataRefused):
                self.offered()
        self.port.page_reader = lambda context: (_ for _ in ()).throw(failure)
        with self.assertRaises(DataRefused):
            self.offered()

    def test_exact_reference_requires_matching_name_and_id_from_fresh_actor_read(self):
        self.assertEqual((), self.data.find("crm/contract", "Other contract（ID=11）"))
        row = self.data.read("crm/contract", "11", ())
        with patch.object(self.data, "read", return_value=replace(row, id="12")):
            self.assertEqual((), self.data.find("crm/contract", "云帆企业版合同（ID=11）"))
            self.assertEqual((), self.offered())
        with patch.object(self.data, "read", side_effect=DataRefused("Read revoked", read_failed=True)):
            with self.assertRaises(DataRefused):
                self.data.find("crm/contract", "云帆企业版合同（ID=11）")
