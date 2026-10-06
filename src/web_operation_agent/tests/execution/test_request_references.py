"""Reference planning removes protocol work without removing settlement checks."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.answer import answer_protocol
from web_operation_agent.answer.evidence import (Origin, PageEvidence, RecordEvidence,
    SourceEvidence, TurnEvidence)
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.execution import product, turn
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.execution.request_contract import missing_answers, revise_requirements
from web_operation_agent.execution.request_references import compile_finish, parse_plan, request_references
from web_operation_agent.schemas import POTENTIALLY_WRITING_ACTIONS
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.execution.test_answer_product import _EvidenceExecutor
from web_operation_agent.tests.execution.test_request_contract import _RecordBrowser
from web_operation_agent.tests.support.business import _Chat, _skill


def plan(*, action=(), kind="none", answers=(("r0",),), answer_kind="data"):
    return {"action_refs": list(action), "action_kind": kind,
            "answers": [{"refs": list(refs), "kind": answer_kind} for refs in answers]}


class ReferenceContract(unittest.TestCase):
    def test_implicit_subject_is_preserved_without_retyping(self):
        prompt = "查晨岚机电电话，再把联系人页签打开给我看"
        refs = request_references(prompt)
        req = parse_plan(plan(action=("r2",), kind="record", answers=(("r1",),)), prompt)
        self.assertEqual("查晨岚机电电话", req.answer_requests[0])
        self.assertEqual("再把联系人页签打开给我看", req.action_request)
        self.assertEqual(prompt, refs["r0"])
        self.assertEqual(("a1",), req.answer_ids)

    def test_incomplete_unknown_or_duplicate_assignments_cannot_drop_work(self):
        prompt = "打开客户，查电话，解释权限"
        for invalid in (
            plan(action=("r1",), kind="record", answers=(("r2",),)),
            plan(action=("r1",), kind="record", answers=(("r1", "r2", "r3"),)),
            plan(answers=(("r0", "r1"),)), plan(answers=(("r999",),)),
            {**plan(), "action_kind": []}, {**plan(), "authority": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_plan(invalid, prompt)
        req = parse_plan({"action_refs": ["r1"], "action_kind": "record", "answers": [
            {"refs": ["r2"], "kind": "data"}, {"refs": ["r3"], "kind": "knowledge"}]}, prompt)
        with self.assertRaises(ValueError):
            revise_requirements(req, replace(req, action_kind="page"), committed=True)

    def test_whole_question_and_sequential_mixed_request_keep_all_text(self):
        question = "未审核订单有几张，含税总额多少？"
        self.assertEqual((question,), parse_plan(plan(), question).answer_requests)
        prompt = "查客户电话并打开详情"
        self.assertEqual({"r0": prompt, "r1": "查客户电话", "r2": "并打开详情"}, request_references(prompt))
        req = parse_plan(plan(action=("r2",), kind="record", answers=(("r1",),)), prompt)
        self.assertEqual(("查客户电话",), req.answer_requests)

    def evidence(self):
        evidence = TurnEvidence()
        typed = Origin("typed", "customer", "2026-10-06T00:00:00Z", "actor-visible customer")
        evidence.record(RecordEvidence("record#1", typed, "c/1", "phone", "Phone", True, "123", "text"))
        evidence.record(RecordEvidence("record#2", typed, "c/1", "fax", "Fax", False))
        evidence.record(PageEvidence("page#1", Origin("page", "page", typed.observed_at, "page"),
                                     "/customer/1", "ui_state", "tab", "Contacts"))
        evidence.record(SourceEvidence("source#1", Origin("source", "code", typed.observed_at, "release"),
            "refund.py", 1, 1, "v1", "a" * 64, "refund = received - returned"))
        return evidence

    def test_server_supplies_answer_shape_but_still_verifies_support_and_coverage(self):
        req = parse_plan(plan(answer_kind="knowledge"), "退款金额怎么算？")
        evidence = self.evidence()
        flat = {"outcome": "answered", "conclusions": [{"evidence_ref": "source#1", "request_refs": ["a1"],
            "statement": "退款金额是已收金额减去已退金额。", "support": "refund = received - returned"}]}
        compiled = compile_finish(flat, req, evidence)
        self.assertEqual((), missing_answers(req, compiled["answer"]["answers_for"],
                                            compiled["answer"]["conclusions"]))
        verified = answer_protocol.submit(evidence, {"outcome": "answered", "answer": {
            k: v for k,v in compiled["answer"].items() if k != "answers_for"}})
        self.assertEqual("complete", verified["coverage"])
        self.assertNotIn("kind", flat["conclusions"][0])
        with self.assertRaises(ValueError):
            compile_finish({**flat, "answer": {}}, req, evidence)
        bad = {**flat, "conclusions": [{**flat["conclusions"][0], "request_refs": ["r2-a1"]}]}
        with self.assertRaises(ValueError):
            compile_finish(bad, req, evidence)

    def test_unread_or_ui_state_cannot_settle_dynamic_fields_and_static_is_not_dynamic(self):
        evidence = self.evidence()
        req = parse_plan(plan(), "客户电话是什么？")
        compiled = compile_finish({"outcome": "partial", "conclusions": [
            {"evidence_ref": "record#2", "request_refs": ["a1"]}]}, req, evidence)
        self.assertEqual(("a1",), missing_answers(req, compiled["answer"]["answers_for"],
                                                 compiled["answer"]["conclusions"]))
        with self.assertRaises(ValueError):
            compile_finish({"outcome": "answered", "conclusions": [
                {"evidence_ref": "source#1", "request_refs": ["a1"]}]}, req, evidence)
        with self.assertRaises(ValueError):
            compile_finish({"outcome": "answered", "conclusions": [
                {"evidence_ref": "record#1", "request_refs": ["a1"]},
                {"evidence_ref": "source#1", "request_refs": [], "statement": "Invented business claim",
                 "support": "refund = received - returned"}]}, req, evidence)
        req = parse_plan(plan(answer_kind="knowledge"), "退款代码怎么实现？")
        with self.assertRaises(ValueError):
            compile_finish({"outcome": "answered", "conclusions": [
                {"evidence_ref": "record#1", "request_refs": ["a1"]}]}, req, evidence)


class ReferenceProduct(unittest.TestCase):
    def execute(self, script, *, prompt="公司名称和金额是什么？", read_navigation=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            class Reads(_EvidenceExecutor):
                def execute(self, *args, **kwargs):
                    result = super().execute(*args, **kwargs)
                    return replace(result, navigation_target="/object/company/company-1") if read_navigation else result
            tools = BusinessToolRuntime((_skill(root / "skill.json"),), Reads())
            chat = _Chat(script)
            executor = AgentTurnExecutor(channel=_Channel(), chat=chat, business_tools=tools,
                config=AgentTurnConfig(supported_scope="Read companies.", require_task_contract=True,
                    request_protocol="references", ledger_path=root / "run.jsonl", step_budget=16,
                    deterministic_read_navigation=read_navigation,
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=POTENTIALLY_WRITING_ACTIONS - {"goto"}),
                control=ReadOnlyControl(), run_id_factory=lambda message: "ref-run")
            with patch.object(turn, "RemoteBrowserClient", _RecordBrowser):
                result = executor.run_once({"message_id": "refs", "text": prompt,
                                            "page_url": "http://app.test/companies"})
            return result, chat

    def test_read_before_plan_then_flat_completion_in_the_same_product_agent(self):
        result, chat = self.execute([
            ("load_skill", {"skill_id": "fixture.companies-query"}), ("query_companies", {}),
            ("plan_task", plan()), ("finish", {"outcome": "answered", "conclusions": [
                {"evidence_ref": "call2-record#1", "request_refs": ["a1"]},
                {"evidence_ref": "call2-record#2", "request_refs": ["a1"]}]})])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("12.50", result.user_answer)
        self.assertIn("Acme", result.user_answer)
        self.assertIn("plan_task", chat.tool_names[0])
        self.assertNotIn("goto", chat.tool_names[0])
        self.assertNotIn("declare_task", chat.tool_names[0])

    def test_page_effect_cannot_precede_plan_and_missing_answer_preserves_unmet_work(self):
        result, chat = self.execute([
            ("goto", {"url": "/object/company/company-1"}),
            ("plan_task", plan(action=("r1",), kind="record", answers=(("r2",),))),
            ("load_skill", {"skill_id": "fixture.companies-query"}), ("query_companies", {}),
            ("goto", {"url": "/object/company/company-1"}),
            ("finish", {"outcome": "acted"}), ("finish", {"outcome": "partial"})],
            prompt="打开公司详情，查金额")
        self.assertNotEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("查金额", result.user_answer)
        self.assertEqual(1, _RemoteBrowser.instances[-1].actions.count("goto"))
        self.assertIn("navigation", str(result.agent.verified_answer))

    def test_old_finish_wrapper_does_not_consume_unbounded_repairs(self):
        result, chat = self.execute([("plan_task", plan()), *[("finish", {
            "outcome": "answered", "answer": {"conclusions": []}})] * 3])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertEqual(4, len(chat.messages))
        self.assertIn("no answer wrapper", str(chat.messages[-1]))

    def test_reference_protocol_requires_explicit_completeness_policy(self):
        with self.assertRaises(ValueError):
            AgentTurnConfig(supported_scope="Read companies", ledger_path=Path("unused"),
                            request_protocol="references")

    def test_malformed_outcome_is_refused_in_the_bounded_repair_path(self):
        result, chat = self.execute([("plan_task", plan(action=("r0",), kind="record", answers=())),
            *[("finish", {"outcome": []})] * 3], prompt="打开公司 Acme")
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertEqual(4, len(chat.messages))

    def test_read_navigation_shortcut_cannot_bypass_plan_or_finish(self):
        result, chat = self.execute([
            ("load_skill", {"skill_id": "fixture.companies-query"}), ("query_companies", {}),
            ("plan_task", plan(action=("r0",), kind="record", answers=())),
            ("finish", {"outcome": "partial"})], prompt="打开公司 Acme", read_navigation=True)
        self.assertEqual(4, len(chat.messages))
        self.assertNotIn("goto", _RemoteBrowser.instances[-1].actions)
        self.assertNotEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("未完成操作", result.user_answer)


if __name__ == "__main__":
    unittest.main()
