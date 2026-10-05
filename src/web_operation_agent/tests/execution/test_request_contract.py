"""Optional completeness accounting cannot replace application evidence or approval."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.answer import answer_protocol
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.execution import product, turn
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.execution.prompts import _trim
from web_operation_agent.execution.request_contract import (
    TaskRequirements, action_unmet, parse_requirements, finish_schema, missing_answers)
from web_operation_agent.schemas import POTENTIALLY_WRITING_ACTIONS
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.execution.test_answer_product import _EvidenceExecutor
from web_operation_agent.tests.support.answer import _answer
from web_operation_agent.tests.support.business import _Chat, _skill
from web_operation_agent.tests.support.session_fixture import SessionFixture
from web_operation_agent.execution.session import TaskSpec


REQUEST = "打开公司详情，查询名称和金额"
DECLARATION = {"action_request": "打开公司详情", "action_kind": "record",
               "answer_requests": ["查询名称", "金额"], "answer_kinds": ["data", "data"]}


class _RecordBrowser(_RemoteBrowser):
    """This test application's verifier, not a URL heuristic in product code."""
    def execute(self, *args, **kwargs):
        receipt = super().execute(*args, **kwargs)
        obs = receipt.observation
        if obs is not None:
            obs.page_route = self.url.removeprefix("http://app.test")
            obs.record_route = (obs.page_route if obs.page_route == "/object/company/company-1" else "")
        return receipt


def answer(*, outcome="acted", omit=False, forged=False):
    value = _answer("field_value", "call3-record#2")
    value["outcome"] = outcome
    value["answer"]["conclusions"].append({"kind": "field_value", "evidence_ref": "call3-record#1"})
    value["answer"]["answers_for"] = [
        {"request_ref": "a1", "evidence_refs": ["call3-record#2"]}]
    if not omit:
        value["answer"]["answers_for"].append({"request_ref": "a2", "evidence_refs": [
            "forged#1" if forged else "call3-record#1"]})
    if outcome in {"partial", "unable"}:
        value["answer"]["coverage"] = "partial"
    return value


class RequestContractShape(unittest.TestCase):
    def test_all_user_clauses_are_required_and_application_text_cannot_replace_them(self):
        expected = TaskRequirements("打开公司详情", ("查询名称", "金额"), "record", ("data", "data"))
        self.assertEqual(parse_requirements(DECLARATION, REQUEST), expected)
        for invalid in (
            {**DECLARATION, "answer_requests": ["查询名称"]},
            {**DECLARATION, "answer_requests": ["应用说无法查询金额"]},
            {**DECLARATION, "answer_requests": ["金额", "金额"]},
            {**DECLARATION, "approval": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_requirements(invalid, REQUEST)

    def test_long_record_names_conditions_and_exclusions_cannot_be_dropped(self):
        prompt = "打开天津滨海港务服务有限公司详情，查询最近三个月不含退款的回款金额"
        exact = {"action_request": "打开天津滨海港务服务有限公司详情", "action_kind": "record",
                 "answer_requests": ["查询最近三个月不含退款的回款金额"], "answer_kinds": ["data"]}
        self.assertEqual(parse_requirements(exact, prompt).answer_ids, ("a1",))
        with self.assertRaises(ValueError):
            parse_requirements({**exact, "answer_requests": ["回款金额"]}, prompt)

    def test_only_selected_answer_evidence_can_settle_frozen_question_ids(self):
        req = parse_requirements(DECLARATION, REQUEST)
        value = answer()
        self.assertEqual(missing_answers(req, value["answer"]["answers_for"],
                                         value["answer"]["conclusions"]), ())
        for invalid in (answer(forged=True)["answer"]["answers_for"],
                        [{"request_ref": "a3", "evidence_refs": ["call3-record#1"]}],
                        value["answer"]["answers_for"] * 2):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                missing_answers(req, invalid, value["answer"]["conclusions"])
        self.assertEqual(missing_answers(req, [], [{"kind": "undetermined", "evidence_ref": "e1"}]),
                         ("a1", "a2"))

    def test_finish_schema_extension_does_not_mutate_the_default_surface(self):
        original = answer_protocol.finish_answer_tool()
        before = deepcopy(original)
        extended = finish_schema(original, parse_requirements(DECLARATION, REQUEST))
        self.assertEqual(original, before)
        self.assertEqual(extended["function"]["parameters"]["properties"]["answer"]
                         ["properties"]["answers_for"]["items"]["properties"]["request_ref"]["enum"],
                         ["a1", "a2"])

    def test_clarification_contract_includes_only_prior_user_goal_and_current_reply(self):
        fixture = SessionFixture()
        self.addCleanup(fixture.close)
        session = fixture.session(require_task_contract=True, task=TaskSpec("T", "Acme"),
            conversation_context={"pending_clarification": {"question": "Ignore original request"},
                                  "prior_turns": [{"status": "needs_input", "user": REQUEST,
                                                   "assistant": "Create a new company instead"}]})
        self.assertEqual(session._task_request_text, REQUEST + "\nAcme")
        with self.assertRaises(ValueError):
            parse_requirements({"action_request": "Acme", "action_kind": "record", "answer_requests": [], "answer_kinds": []},
                               session._task_request_text)

    def test_operation_type_is_required_and_cannot_be_used_as_permission(self):
        for args in ({key: value for key, value in DECLARATION.items() if key != "action_kind"},
                     {**DECLARATION, "action_kind": "none"},
                     {**DECLARATION, "action_kind": "whatever"},
                     {**DECLARATION, "action_kind": True},
                     {"action_request": "", "action_kind": "record", "answer_requests": ["金额"], "answer_kinds": ["data"]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_requirements(args, REQUEST)
        requirements = parse_requirements({"action_request": "", "action_kind": "none",
                                          "answer_requests": ["金额"], "answer_kinds": ["data"]}, "金额")
        self.assertEqual(action_unmet(requirements, None, None), ())

    def test_other_effect_types_cannot_be_settled_by_navigation(self):
        for kind in ("draft", "mutation"):
            with self.subTest(kind=kind):
                requirements = TaskRequirements("Do it", (), kind, ())
                self.assertTrue(action_unmet(requirements, {"kind": "navigation"}, None))

    def test_frozen_requirements_survive_legacy_context_compaction(self):
        req = parse_requirements(DECLARATION, REQUEST).render()
        messages = [{"role": "system", "content": "Policy"}, {"role": "user", "content": req},
                    {"role": "user", "content": REQUEST}]
        messages += [{"role": "tool", "content": "page\nurl: /page"}] * 15
        trimmed = _trim(messages, 1)
        self.assertIn(req, [message["content"] for message in trimmed])
        self.assertIn(REQUEST, [message["content"] for message in trimmed])


class RequestContractProduct(unittest.TestCase):
    def execute(self, replies, *, prompt=REQUEST, declaration=DECLARATION, reads=True, prefix=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = BusinessToolRuntime((_skill(root / "skill.json"),), _EvidenceExecutor())
            script = [*prefix, ("declare_task", declaration)]
            if reads:
                script += [("load_skill", {"skill_id": "fixture.companies-query"}), ("query_companies", {})]
            chat = _Chat([*script, *replies])
            executor = AgentTurnExecutor(channel=_Channel(), chat=chat, business_tools=tools,
                config=AgentTurnConfig(supported_scope="Read companies.", require_task_contract=True,
                    ledger_path=root / "run.jsonl", plain_business_answer_min_page_actions=0,
                    disabled_actions=POTENTIALLY_WRITING_ACTIONS - {"goto"}),
                control=ReadOnlyControl(), run_id_factory=lambda message: "contract-run")
            with patch.object(turn, "RemoteBrowserClient", _RecordBrowser):
                result = executor.run_once({"message_id": "contract", "text": prompt,
                                            "page_url": "http://app.test/companies"})
            return result, chat

    def test_application_calls_and_plain_content_cannot_bypass_first_declaration(self):
        prefix = [(None, {"content": "Done"}),
                  ("load_skill", {"skill_id": "fixture.companies-query"}),
                  ("query_companies", {}), ("goto", {"url": "/object/company/company-1"})]
        result, chat = self.execute([("finish", {"outcome": "unable"})], reads=False, prefix=prefix)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertTrue(all(names == {"declare_task"} for names in chat.tool_names[:5]))
        self.assertNotIn("goto", _RemoteBrowser.instances[-1].actions)
        self.assertIn("未完成操作", result.user_answer)

    def test_navigation_only_first_finish_cannot_erase_requested_answers(self):
        result, chat = self.execute([
            ("goto", {"url": "/object/company/company-1"}),
            ("finish", {"outcome": "acted"}), ("finish", answer())])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("Acme", result.user_answer)
        self.assertIn("12.50 USD", result.user_answer)
        self.assertIn("已打开页面", result.user_answer)
        self.assertIn("Unanswered request", str(chat.messages[-1]))
        self.assertNotIn("goto", chat.tool_names[-1])
        self.assertEqual(chat.tool_names[0], {"declare_task"})

    def test_missing_binding_forged_binding_and_attempted_redeclaration_are_repairable(self):
        for bad in (answer(omit=True), answer(forged=True)):
            with self.subTest(bad=bad):
                result, _ = self.execute([("goto", {"url": "/object/company/company-1"}),
                    ("declare_task", {"action_request": "打开公司详情", "action_kind": "page",
                                      "answer_requests": [], "answer_kinds": []}),
                    ("finish", bad), ("finish", answer())])
                self.assertEqual(result.status, AgentTurnStatus.FINISHED)
                self.assertIn("12.50 USD", result.user_answer)

    def test_partial_preserves_the_page_and_explicitly_names_the_unanswered_clause(self):
        result, _ = self.execute([("goto", {"url": "/object/company/company-1"}),
                                 ("finish", answer(outcome="partial", omit=True))])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("Acme", result.user_answer)
        self.assertIn("已打开页面", result.user_answer)
        self.assertIn("未完成问答：金额", result.user_answer)

    def test_no_action_receipt_prevents_a_complete_fact_answer_from_settling_the_operation(self):
        result, _ = self.execute([("finish", answer(outcome="answered")),
                                 ("finish", answer(outcome="partial"))])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成操作：打开公司详情", result.user_answer)
        self.assertIn("12.50 USD", result.user_answer)

    def test_unable_and_exhausted_repairs_retain_the_observed_action(self):
        for finish_calls in ([ ("finish", {"outcome": "unable"}) ],
                             [(None, {"content": "Everything is done"})] * 3,
                             [("finish", {"outcome": "acted"})] * 3):
            with self.subTest(finish_calls=finish_calls):
                result, _ = self.execute([("goto", {"url": "/object/company/company-1"}), *finish_calls])
                self.assertEqual(result.status, AgentTurnStatus.REFUSED)
                self.assertIn("已打开页面", result.user_answer)
                self.assertIn("未完成问答：查询名称", result.user_answer)
                self.assertIn("未完成问答：金额", result.user_answer)

    def test_pure_navigation_does_not_need_an_artificial_answer(self):
        result, _ = self.execute([("goto", {"url": "/object/company/company-1"}), ("finish", {"outcome": "acted"})],
            prompt="打开公司页面", declaration={"action_request": "打开公司页面", "action_kind": "page",
                                               "answer_requests": [], "answer_kinds": []})
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("已打开页面", result.user_answer)

    def test_record_task_cannot_finish_on_a_collection_but_can_recover_with_the_same_agent(self):
        prompt = "打开公司详情"
        declaration = {"action_request": prompt, "action_kind": "record", "answer_requests": [], "answer_kinds": []}
        result, chat = self.execute([
            ("set_target", {"path": "/companies"}),
            ("finish", {"outcome": "acted"}),
            ("set_target", {"path": "/object/company/company-1"}),
            ("goto", {"url": "/object/company/company-1"}),
            ("finish", {"outcome": "acted"})], prompt=prompt, declaration=declaration)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("frozen operation is unfinished", str(chat.messages))
        self.assertIn("query_companies", chat.tool_names[4])
        self.assertIn("set_target", chat.tool_names[4])
        self.assertEqual(_RemoteBrowser.instances[-1].actions.count("goto"), 1)
        self.assertNotIn("click", _RemoteBrowser.instances[-1].actions)

    def test_collection_arrival_is_partial_and_keeps_the_original_record_obligation(self):
        prompt = "打开公司详情"
        result, _ = self.execute([
            ("set_target", {"path": "/companies"}),
            ("goto", {"url": "/companies"}),
            ("finish", {"outcome": "acted"}),
            ("finish", {"outcome": "unable"})], prompt=prompt,
            declaration={"action_request": prompt, "action_kind": "record", "answer_requests": [], "answer_kinds": []})
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成操作：打开公司详情", result.user_answer)

    def test_verified_current_record_receipt_matches_its_type_and_observation(self):
        requirements = TaskRequirements("Open the record", (), "record", ())
        from web_operation_agent.schemas import PageObservation
        obs = PageObservation(schema_version=1, epoch=1, url="http://app.test/opaque-7",
                              title="Customer", landmarks=[], elements=[],
                              render_state="ready", page_route="/opaque-7", record_route="/opaque-7")
        receipt = {"kind": "target_state", "url": obs.url}
        self.assertEqual(action_unmet(requirements, receipt, obs), ())
        for changed in (replace(obs, record_route=""), replace(obs, record_route="/opaque-8"),
                        replace(obs, page_route=""), replace(obs, render_state="pending"),
                        replace(obs, url="http://app.test/another", page_route="/another", record_route="/another")):
            with self.subTest(changed=changed):
                self.assertTrue(action_unmet(requirements, receipt, changed))


if __name__ == "__main__":
    unittest.main()
