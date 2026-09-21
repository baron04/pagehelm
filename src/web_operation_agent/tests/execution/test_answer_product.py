"""The same product entry must deliver computed answers rather than the last query table."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.execution import product, turn
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.schemas import POTENTIALLY_WRITING_ACTIONS
from web_operation_agent.tests.support.business import _Chat, _ProjectedExecutor, _skill
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.answer import _answer


class _EvidenceExecutor(_ProjectedExecutor):
    def execute(self, name, args, *, context=None):
        result = super().execute(name, args, context=context)
        origin = Origin("typed", "company-read", "2026-09-13T00:00:00Z", "current workspace")
        return replace(result, evidence=(
            QueryEvidence("query#1", origin, "companies", ("all visible companies",), 1,
                          records_scanned=1, covers_whole_scope=True),
            RecordEvidence("record#1", origin, "company/1", "amount", "annual amount", True,
                           "12.50", "money", "USD"),
            RecordEvidence("record#2", origin, "company/1", "name", "company name", True,
                           "Acme", "text"),
        ))


class DefaultProductAnswerPath(unittest.TestCase):
    def execute(self, replies, *, renderer=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("load_skill", {"skill_id": "fixture.companies-query"}),
                          ("query_companies", {}), *replies])
            tools = BusinessToolRuntime((_skill(root / "skill.json"),), _EvidenceExecutor())
            kwargs = {"answer_renderer": renderer} if renderer else {}
            executor = AgentTurnExecutor(
                channel=_Channel(), chat=chat, business_tools=tools,
                config=AgentTurnConfig(supported_scope="Read companies.",
                                       ledger_path=root / "run.jsonl",
                                       plain_business_answer_min_page_actions=0,
                                       disabled_actions=(
                                           POTENTIALLY_WRITING_ACTIONS - {"goto"})),
                control=ReadOnlyControl(), run_id_factory=lambda message: "answer-run", **kwargs)
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.run_once({"message_id": "answer-task", "text": "公司金额合计多少？",
                                            "page_url": "http://app.test/companies"})
            return result, chat

    def test_computed_total_reaches_the_default_product_without_a_page_action(self):
        result, chat = self.execute([
            ("compute", {"operation": "sum", "dataset_ref": "call2-query#1", "field": "amount"}),
            ("finish", _answer("aggregate", "calc#3")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("12.50 USD", result.user_answer)
        self.assertNotIn("acme.test", result.user_answer)
        self.assertFalse(result.agent.business_answer_projected)
        self.assertIsNotNone(result.agent.verified_answer)
        self.assertEqual(result.agent.page_actions, 0)
        self.assertIn("compute", chat.tool_names[2])
        self.assertIn("call2-record#1", str(chat.messages[2]))

    def test_plain_content_is_corrected_instead_of_overwriting_or_bypassing_evidence(self):
        result, chat = self.execute([
            (None, {"content": "The total is 900000 dollars."}),
            ("compute", {"operation": "sum", "dataset_ref": "call2-query#1", "field": "amount"}),
            ("finish", _answer("aggregate", "calc#4")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("12.50", result.user_answer)
        self.assertNotIn("900000", result.user_answer)
        self.assertEqual(len(chat.messages), 5)

    def test_forged_reference_can_be_corrected_by_the_same_agent(self):
        result, _ = self.execute([
            ("finish", _answer("field_value", "call1-record#999")),
            ("finish", _answer("field_value", "call2-record#1")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("12.50", result.user_answer)

    def test_repeated_invalid_answer_stops_with_a_short_honest_message(self):
        result, chat = self.execute([(None, {"content": "The total is 900000 dollars."})] * 3)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("不足", result.user_answer)
        self.assertNotIn("900000", result.user_answer)
        self.assertEqual(len(chat.messages), 5)

    def test_a_custom_renderer_cannot_replace_the_verified_answer(self):
        result, _ = self.execute([
            ("finish", _answer("field_value", "call2-record#1"))],
            renderer=lambda context: "900000 dollars")
        self.assertIn("12.50", result.user_answer)
        self.assertNotIn("900000", result.user_answer)

    def test_selection_fields_come_from_the_same_read(self):
        result, _ = self.execute([
            ("finish", _answer("selection", "call2-query#1", fields=["name", "amount"])),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("Acme", result.user_answer)
        self.assertIn("12.50 USD", result.user_answer)

    def test_unable_does_not_fall_back_to_the_last_table(self):
        result, _ = self.execute([("finish", {"outcome": "unable"})])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("acme.test", result.user_answer)
        self.assertIsNone(result.agent.verified_answer)

    def test_navigating_does_not_restore_the_unverified_business_prose_exit(self):
        result, _ = self.execute([
            ("goto", {"url": "/object/company/company-1"}),
            ("finish", {"done": True, "why": "The total is 900000 dollars."}),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("已打开页面", result.user_answer)
        self.assertNotIn("900000", result.user_answer)

    def test_an_action_claim_without_a_receipt_is_refused(self):
        result, _ = self.execute([("finish", {"outcome": "acted"})] * 3)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)

    def test_completed_navigation_can_include_verified_facts(self):
        answer = _answer("selection", "call2-query#1", fields=["name", "amount"])
        answer["outcome"] = "acted"
        result, _ = self.execute([
            ("goto", {"url": "/object/company/company-1"}), ("finish", answer),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("已打开页面", result.user_answer)
        self.assertIn("Acme", result.user_answer)
        self.assertIn("12.50 USD", result.user_answer)

    def test_a_completed_action_does_not_authorize_fabricated_answer_facts(self):
        answer = _answer("field_value", "forged#1")
        answer["outcome"] = "acted"
        result, _ = self.execute([
            ("goto", {"url": "/object/company/company-1"}),
            *[("finish", answer)] * 3,
        ])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIsNone(result.agent.verified_answer)

    def test_selection_fields_survive_query_context_trimming_after_navigation(self):
        result, chat = self.execute([
            ("compute", {"operation": "argmax", "dataset_ref": "call2-query#1", "field": "amount"}),
            ("goto", {"url": "/object/company/company-1"}),
            ("wait_for", {"ms": 0}),
            ("finish", _answer("selection", "calc#3", fields=["name", "amount"])),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("Acme", result.user_answer)
        final_context = chat.messages[-1]
        query = next(x for x in final_context if x.get("role") == "tool" and x.get("name") == "query_companies")
        computed = next(x for x in final_context if x.get("role") == "tool" and x.get("name") == "compute")
        self.assertTrue(query["content"].startswith("(earlier)"))
        self.assertIn("call2-record#2", computed["content"])
        self.assertIn('"field_name":"name"', computed["content"])
        self.assertIn('"outcome":"acted"', str(final_context))


if __name__ == "__main__":
    unittest.main()
