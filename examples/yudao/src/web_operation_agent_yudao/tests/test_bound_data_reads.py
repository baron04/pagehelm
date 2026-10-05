"""Synthetic API qualification of actor binding and per-request cancellation."""
from datetime import date
import time
import unittest
from unittest.mock import patch

from web_operation_agent.answer.evidence import ComputationEvidence, RecordEvidence
from web_operation_agent.business.errors import BusinessToolIdentityError
from web_operation_agent.goal.read_tools import DataReadTools
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.transport.browser import TurnCancelled
from web_operation_agent_yudao.bound_data_reads import BoundDataReads
from web_operation_agent_yudao.client import YudaoClient
from web_operation_agent_yudao.goal import YudaoGoalData
from .test_goal import _Client, DOCS


class BoundDataReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _Client()
        self.client = YudaoClient.for_token("http://synthetic.invalid", "synthetic-token", "1")
        self.user = {"id": 1, "deptId": 10}
        self.after = lambda path: None
        def request(method, path, **kwargs):
            if method != "GET":
                raise AssertionError("data tools cannot mutate")
            payload = self.fixture.get(path, **(kwargs.get("params") or {}))
            if path == "system/auth/get-permission-info":
                payload = {**payload, "user": self.user}
            self.after(path)
            return 0, {"code": 0, "data": payload}
        self.addCleanup(patch.stopall)
        patch.object(self.client, "_request", side_effect=request).start()
        self.data = YudaoGoalData(self.client, DOCS)
        self.tools = DataReadTools(lambda ctx, check: BoundDataReads(self.data, ctx, check),
                                  "yudao", "yudao.read", today=date(2026, 10, 6))
        now = int(time.time() * 1000)
        principal = PrincipalContext(1, "yudao", "1", "10", "1", "delegation", now-1000,
                                     now+60_000, ("yudao.read",))
        self.context = OperationContext.create(principal, session_id="session", turn_id="turn",
                                               operation_id="read", deadline_ms=now+50_000)

    def call(self, *deliverables, check=lambda: None):
        return self.tools.execute("resolve_data", {"deliverables": list(deliverables)},
            context=self.context, checkpoint=check, user_request="查询合同的客户和数量")

    def test_same_resolver_reads_native_link_and_count_without_source_or_page(self):
        result = self.call({"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同", "fields": ["客户"]},
                          {"kind": "aggregate", "entity": "crm/contract", "metric": {"op": "count"}})
        customer = next(e for e in result.evidence if isinstance(e, RecordEvidence) and e.field_name == "customerId")
        self.assertEqual("crm/contract:11", customer.record_id)
        self.assertIn("云帆", customer.value)
        count = next(e for e in result.evidence if isinstance(e, ComputationEvidence))
        self.assertEqual("2", count.value)
        self.assertEqual((), result.allowed_routes)
        self.assertIsNone(result.form_draft)

    def test_foreign_actor_and_mid_read_credential_renewal_are_refused(self):
        self.user = {"id": 2, "deptId": 10}
        with self.assertRaises(BusinessToolIdentityError):
            self.call({"kind": "aggregate", "entity": "crm/contract"})
        self.assertFalse(any(path == "crm/contract/page" for path, _ in self.fixture.calls))
        self.user = {"id": 1, "deptId": 10}
        self.after = lambda path: self.client.renew("synthetic-renewed") if path == "crm/contract/page" else None
        with self.assertRaises(BusinessToolIdentityError):
            self.call({"kind": "aggregate", "entity": "crm/contract"})
        self.assertEqual(1, sum(path == "crm/contract/page" for path, _ in self.fixture.calls))

    def test_cancellation_stops_before_next_scene_and_clears_request_window(self):
        stopped = [False]
        self.after = lambda path: stopped.__setitem__(0, True) if path == "crm/contract/page" else None
        def check():
            if stopped[0]:
                raise TurnCancelled("stopped")
        with self.assertRaises(TurnCancelled):
            self.call({"kind": "aggregate", "entity": "crm/contract"}, check=check)
        self.assertEqual(1, sum(path == "crm/contract/page" for path, _ in self.fixture.calls))
        # A cancelled context cannot leak into the next independent client read.
        self.assertIn("user", self.client.get("system/auth/get-permission-info"))


if __name__ == "__main__":
    unittest.main()
