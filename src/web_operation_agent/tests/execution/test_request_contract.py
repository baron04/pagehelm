"""Optional completeness accounting cannot replace application evidence or approval."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
    DECLARE_TASK_TOOL, MANUAL_SUBMIT_TASK_SYSTEM, TaskRequirements, action_unmet,
    declaration_schema, declaration_system, parse_requirements, revise_requirements,
    finish_schema, missing_answers)
from web_operation_agent.schemas import POTENTIALLY_WRITING_ACTIONS, ElementRef
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser, _principal
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
    def __init__(self, channel, **kwargs):
        super().__init__(channel, **kwargs)
        self.url = channel.latest_observation_envelope.observation.url

    def observe(self):
        obs = super().observe()
        obs.elements.append(ElementRef("companies-link", "link", "Companies", "a",
            obs.epoch, {"href": "/companies"}))
        return obs

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
    def test_draft_only_form_mode_is_present_before_declaration_without_changing_global_schema(self):
        session = SimpleNamespace(allow_form_prefill=True, _application_form_tools=object(),
            _approvals=SimpleNamespace(mutations_enabled=False))
        original = deepcopy(DECLARE_TASK_TOOL)
        scoped = declaration_schema(session)
        self.assertIn('DEFAULT FORM EXECUTION MODE', scoped['function']['description'])
        self.assertNotIn(MANUAL_SUBMIT_TASK_SYSTEM.strip(), scoped['function']['description'])
        self.assertIn(MANUAL_SUBMIT_TASK_SYSTEM, declaration_system(session))
        self.assertIn('action_kind=draft', scoped['function']['description'])
        self.assertIn('Explicit persistence remains mutation',
                      scoped['function']['description'])
        self.assertEqual(original['function']['parameters'], scoped['function']['parameters'])
        self.assertEqual(original, DECLARE_TASK_TOOL)

    def test_disabling_writes_alone_does_not_reclassify_persistence_as_draft(self):
        for prefill, forms, writes in ((False, object(), False), (True, None, False),
                                      (True, object(), True)):
            session = SimpleNamespace(allow_form_prefill=prefill, _application_form_tools=forms,
                _approvals=SimpleNamespace(mutations_enabled=writes))
            with self.subTest(prefill=prefill, forms=bool(forms), writes=writes):
                scoped = declaration_schema(session)
                self.assertEqual(DECLARE_TASK_TOOL['function']['parameters'],
                                 scoped['function']['parameters'])
                self.assertNotIn('action_kind=draft', scoped['function']['description'])
                self.assertNotIn(MANUAL_SUBMIT_TASK_SYSTEM, declaration_system(session))

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

    def test_operation_cannot_be_redeclared_as_an_independent_answer(self):
        prompt = "查看公司详情"
        for answers in ([prompt], ["公司详情"]):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                parse_requirements({"action_request": prompt, "action_kind": "record",
                    "answer_requests": answers, "answer_kinds": ["data"]}, prompt)
        self.assertEqual(parse_requirements({"action_request": prompt, "action_kind": "record",
            "answer_requests": [], "answer_kinds": []}, prompt).answer_ids, ())

    def test_inserted_subject_reports_bad_quotation_before_overlap_without_dropping_the_question(self):
        prompt = "先告诉我青岚有几个联系人，再打开联系人页面"
        declaration = {"action_request": "打开青岚的联系人页面", "action_kind": "record",
            "answer_requests": ["先告诉我青岚有几个联系人"], "answer_kinds": ["data"]}
        with self.assertRaisesRegex(ValueError, "quote exact original") as caught:
            parse_requirements(declaration, prompt)
        self.assertNotIn("additional answer", str(caught.exception))
        corrected = parse_requirements({**declaration, "action_request": "打开联系人页面"}, prompt)
        self.assertEqual(corrected.action_request, "打开联系人页面")
        self.assertEqual(corrected.answer_requests, ("先告诉我青岚有几个联系人",))
        with self.assertRaisesRegex(ValueError, "omitted original"):
            parse_requirements({**declaration, "action_request": "打开联系人页面",
                                "answer_requests": [], "answer_kinds": []}, prompt)

    def test_inspection_purpose_belongs_to_the_operation_but_real_questions_remain_required(self):
        prompt = "打开合同详情和产品清单，让我核对一下"
        declaration = {"action_request": prompt, "action_kind": "record",
                       "answer_requests": [], "answer_kinds": []}
        self.assertEqual(parse_requirements(declaration, prompt).answer_ids, ())
        mixed = prompt + "，产品有几种"
        with self.assertRaisesRegex(ValueError, "omitted original"):
            parse_requirements(declaration, mixed)
        corrected = parse_requirements({**declaration, "answer_requests": ["产品有几种"],
                                       "answer_kinds": ["data"]}, mixed)
        self.assertEqual(corrected.answer_ids, ("a1",))

    def test_separate_repeated_words_and_actual_mixed_clauses_are_preserved(self):
        prompt = "打开公司详情，并回答打开公司详情需要什么权限"
        declaration = {"action_request": "打开公司详情", "action_kind": "record",
            "answer_requests": ["回答打开公司详情需要什么权限"], "answer_kinds": ["knowledge"]}
        self.assertEqual(parse_requirements(declaration, prompt).answer_ids, ("a1",))
        # The first occurrence of the longer action overlaps the only answer; its
        # later occurrence is a valid independent quotation.
        self.assertEqual(parse_requirements({"action_request": "ABCDE", "action_kind": "page",
            "answer_requests": ["DEF"], "answer_kinds": ["knowledge"]},
            "ABCDEF, ABCDE").answer_ids, ("a1",))
        with self.assertRaises(ValueError):
            parse_requirements({**declaration, "action_request": prompt}, prompt)

    def test_question_clauses_can_share_the_original_subject_without_adding_text(self):
        prompt = "销售订单今天有几张，含税金额多少？"
        whole = {"action_request":"", "action_kind":"none", "answer_requests":[prompt],
                 "answer_kinds":["data"]}
        self.assertEqual(parse_requirements(whole, prompt).answer_requests, (prompt,))
        shared = {**whole, "answer_requests":["销售订单今天有几张", prompt],
                  "answer_kinds":["data", "data"]}
        self.assertEqual(parse_requirements(shared, prompt).answer_ids, ("a1", "a2"))
        with self.assertRaises(ValueError):
            parse_requirements({**whole, "answer_requests":["销售订单今天有几张", "销售订单含税金额多少？"],
                               "answer_kinds":["data", "data"]}, prompt)

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

    def test_questions_only_error_names_the_fields_to_correct(self):
        prompt = "How many open invoices are there?"
        duplicated = {"action_request": prompt, "action_kind": "none",
                      "answer_requests": [prompt], "answer_kinds": ["data"]}
        with self.assertRaises(ValueError) as caught:
            parse_requirements(duplicated, prompt)
        self.assertIn('action_request=""', str(caught.exception))
        self.assertIn('action_kind="none"', str(caught.exception))
        corrected = parse_requirements({**duplicated, "action_request": ""}, prompt)
        self.assertEqual(corrected.answer_requests, (prompt,))
        self.assertEqual(corrected.action_request, "")

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

    def test_after_results_correction_cannot_drop_a_clause_or_weaken_its_outcome(self):
        original = parse_requirements(DECLARATION, REQUEST)
        for proposed in (
            replace(original, action_kind="page"),
            replace(original, answer_kinds=("ui", "data")),
            replace(original, answer_requests=("金额", "查询名称")),
            replace(original, answer_requests=("查询名称",), answer_kinds=("data",)),
        ):
            with self.subTest(proposed=proposed), self.assertRaises(ValueError):
                revise_requirements(original, proposed, committed=True)

    def test_accepted_corrections_are_bounded_and_do_not_reset_on_a_noop(self):
        original = parse_requirements(DECLARATION, REQUEST)
        second = revise_requirements(original,
            replace(original, answer_kinds=("knowledge", "data")), committed=True)
        with self.assertRaises(ValueError):
            revise_requirements(second, second, committed=True)
        third = revise_requirements(second,
            replace(second, answer_kinds=("data", "knowledge")), committed=True)
        self.assertEqual(("r3-a1", "r3-a2"), third.answer_ids)
        with self.assertRaises(ValueError):
            revise_requirements(third, original, committed=False)


class RequestContractProduct(unittest.TestCase):
    def execute(self, replies, *, prompt=REQUEST, declaration=DECLARATION, reads=True,
                prefix=(), channel=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = BusinessToolRuntime((_skill(root / "skill.json"),), _EvidenceExecutor())
            script = [*prefix, ("declare_task", declaration)]
            if reads:
                script += [("load_skill", {"skill_id": "fixture.companies-query"}), ("query_companies", {})]
            chat = _Chat([*script, *replies])
            channel = channel or _Channel()
            obs = channel.latest_observation_envelope.observation
            obs.elements.append(ElementRef("companies-link", "link", "Companies", "a",
                obs.epoch, {"href": "/companies"}))
            executor = AgentTurnExecutor(channel=channel, chat=chat, business_tools=tools,
                config=AgentTurnConfig(supported_scope="Read companies.", require_task_contract=True,
                    ledger_path=root / "run.jsonl", plain_business_answer_min_page_actions=0,
                    step_budget=16,
                    disabled_actions=POTENTIALLY_WRITING_ACTIONS - {"goto"}),
                control=ReadOnlyControl(), run_id_factory=lambda message: "contract-run")
            with patch.object(turn, "RemoteBrowserClient", _RecordBrowser):
                result = executor.run_once({"message_id": "contract", "text": prompt,
                    "page_url": channel.latest_observation_envelope.observation.url})
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

    def test_declaration_has_only_its_contract_then_execution_restores_full_instructions(self):
        from web_operation_agent.execution.prompts import AGENT_SYSTEM, TARGET_SYSTEM
        from web_operation_agent.execution.request_contract import TASK_SYSTEM
        result, chat = self.execute([("finish", {"outcome": "unable"})], reads=False)
        first = chat.messages[0][0]["content"]
        execution = chat.messages[1][0]["content"]
        self.assertIn(TASK_SYSTEM, first)
        self.assertNotIn(AGENT_SYSTEM, first)
        self.assertIn(AGENT_SYSTEM, execution)
        self.assertIn(TARGET_SYSTEM, execution)
        self.assertIn(TASK_SYSTEM, execution)
        self.assertLess(len(first), len(execution))
        self.assertEqual(chat.tool_names[0], {"declare_task"})
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

    def test_unavailable_operation_retains_an_independent_answer_as_partial(self):
        prompt = "公司的金额是多少？保存公司"
        final = _answer("field_value", "call3-record#1")
        final["outcome"] = "partial"
        final["answer"]["coverage"] = "partial"
        final["answer"]["conclusions"].append({"kind": "capability_gap", "evidence_ref": "capability#1"})
        final["answer"]["answers_for"] = [{"request_ref": "a1", "evidence_refs": ["call3-record#1"]}]
        result, _ = self.execute([("check_capability", {}), ("finish", final)],
            channel=_Channel(principal=_principal()),
            prompt=prompt, declaration={"action_request": "保存公司", "action_kind": "mutation",
                "answer_requests": ["公司的金额是多少？"], "answer_kinds": ["data"]})
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertEqual(result.agent.verified_answer["coverage"], "partial")
        self.assertIn("12.50 USD", result.user_answer)
        self.assertIn("未完成操作：保存公司", result.user_answer)
        self.assertNotIn("未完成问答", result.user_answer)

    def test_binding_repair_names_only_eligible_selected_conclusions(self):
        invalid = answer(outcome="partial")
        invalid["answer"]["answers_for"][0]["evidence_refs"].append("call3-query#1")
        result, chat = self.execute([("finish", invalid), ("finish", answer(outcome="partial"))])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        feedback = chat.messages[-1][-1]["content"]
        self.assertIn("Eligible selected refs: call3-record#2, call3-record#1", feedback)
        self.assertNotIn("Eligible selected refs: call3-query", feedback)

    def test_no_action_receipt_prevents_a_complete_fact_answer_from_settling_the_operation(self):
        result, _ = self.execute([("finish", answer(outcome="answered")),
                                 ("finish", answer(outcome="partial"))])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成操作：打开公司详情", result.user_answer)
        self.assertIn("12.50 USD", result.user_answer)

    def test_explicit_partial_retains_facts_even_if_model_declares_complete_coverage(self):
        partial = answer(outcome="partial")
        partial["answer"]["coverage"] = "complete"
        result, _ = self.execute([("finish", partial)])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成操作：打开公司详情", result.user_answer)
        self.assertIn("12.50 USD", result.user_answer)

    def test_read_facts_do_not_freeze_a_question_misclassified_as_record_navigation(self):
        prompt = "公司的金额是多少"
        declaration = {"action_request":prompt, "action_kind":"record", "answer_requests":[],
                       "answer_kinds":[]}
        corrected = {"action_request":"", "action_kind":"none", "answer_requests":[prompt],
                     "answer_kinds":["data"]}
        final = _answer("field_value", "call3-record#1")
        final["answer"]["answers_for"] = [{"request_ref":"r2-a1", "evidence_refs":["call3-record#1"]}]
        result, _ = self.execute([("declare_task", corrected), ("finish", final)],
                                 prompt=prompt, declaration=declaration)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("12.50 USD", result.user_answer)
        self.assertNotIn("goto", _RemoteBrowser.instances[-1].actions)

    def test_highlighting_a_list_cannot_complete_or_hand_over_a_record_task(self):
        prompt = "打开公司详情"
        result, chat = self.execute([("highlight", {"ref":"companies-link"}),
                                    ("finish", {"outcome":"unable"})], reads=False,
            prompt=prompt, declaration={"action_request":prompt, "action_kind":"record",
                                        "answer_requests":[], "answer_kinds":[]})
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("highlight", _RemoteBrowser.instances[-1].actions)
        self.assertIn("No control was highlighted", str(chat.messages))

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

    def test_overlapping_declaration_repairs_before_any_application_tool(self):
        prompt = "查看公司详情"
        declaration = {"action_request": prompt, "action_kind": "record",
                       "answer_requests": [], "answer_kinds": []}
        result, chat = self.execute([
            ("goto", {"url": "/object/company/company-1"}), ("finish", {"outcome": "acted"})],
            prompt=prompt, declaration=declaration,
            prefix=(("declare_task", {**declaration, "answer_requests": [prompt], "answer_kinds": ["data"]}),))
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(chat.tool_names[:2], [{"declare_task"}, {"declare_task"}])
        self.assertIn("an operation is not an additional answer", str(chat.messages))
        self.assertEqual(_RemoteBrowser.instances[-1].actions.count("goto"), 1)

    def test_misquoted_operation_can_repair_once_and_deliver_both_the_record_and_fact(self):
        prompt = "打开公司详情，并查询金额"
        declaration = {"action_request": "打开公司详情", "action_kind": "record",
                       "answer_requests": ["查询金额"], "answer_kinds": ["data"]}
        final = _answer("field_value", "call4-record#1")
        final["outcome"] = "acted"
        final["answer"]["answers_for"] = [{"request_ref": "a1", "evidence_refs": ["call4-record#1"]}]
        result, chat = self.execute([
            ("goto", {"url": "/object/company/company-1"}), ("finish", final)],
            prompt=prompt, declaration=declaration,
            prefix=(("declare_task", {**declaration, "action_request": "打开公司的详情"}),))
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("12.50 USD", result.user_answer)
        self.assertIn("已打开页面", result.user_answer)
        self.assertIn("quote exact original", str(chat.messages))
        self.assertNotIn("an operation is not an additional answer", str(chat.messages))
        self.assertEqual(chat.tool_names[:2], [{"declare_task"}, {"declare_task"}])
        self.assertEqual(_RemoteBrowser.instances[-1].actions.count("goto"), 1)

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

    def test_wrong_initial_action_kind_can_be_corrected_before_application_facts(self):
        prompt = "打开公司列表"
        declaration = {"action_request": prompt, "action_kind": "record",
                       "answer_requests": [], "answer_kinds": []}
        result, chat = self.execute([
            ("declare_task", {**declaration, "action_kind": "page"}),
            ("set_target", {"path": "/companies"}),
            ("goto", {"url": "/companies"}),
            ("finish", {"outcome": "acted"})],
            prompt=prompt, declaration=declaration, reads=False)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("declare_task", chat.tool_names[1])
        frozen = [m for m in chat.messages[-1] if m["role"] == "user"
                  and m.get("content", "").startswith("FROZEN USER REQUIREMENTS")]
        self.assertEqual(1, len(frozen))
        self.assertIn('"revision": 2', frozen[0]["content"])
        self.assertNotIn('"action_kind": "record"', frozen[0]["content"])

    def test_stricter_correction_invalidates_arrival_until_the_current_record_is_rebound(self):
        prompt = "打开公司详情"
        declaration = {"action_request": prompt, "action_kind": "page",
                       "answer_requests": [], "answer_kinds": []}
        channel = _Channel(page_url="http://app.test/start")
        result, chat = self.execute([
            ("set_target", {"path": "/object/company/company-1"}),
            ("goto", {"url": "/object/company/company-1"}),
            ("declare_task", {**declaration, "action_kind": "record"}),
            ("finish", {"outcome": "acted"}),
            ("set_target", {"path": "/object/company/company-1"}),
            ("finish", {"outcome": "acted"})],
            prompt=prompt, declaration=declaration, channel=channel)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED,
                         (result.terminal_reason, str(chat.messages[-1][-4:])))
        self.assertIn("frozen operation is unfinished", str(chat.messages))
        self.assertEqual(1, len(channel.approvals))
        self.assertEqual(1, _RemoteBrowser.instances[-1].actions.count("goto"))

    def test_partial_after_correction_preserves_actual_action_but_not_old_completion_proof(self):
        prompt = "打开公司详情"
        declaration = {"action_request": prompt, "action_kind": "page",
                       "answer_requests": [], "answer_kinds": []}
        result, _ = self.execute([
            ("set_target", {"path": "/object/company/company-1"}),
            ("goto", {"url": "/object/company/company-1"}),
            ("declare_task", {**declaration, "action_kind": "record"}),
            ("finish", {"outcome": "unable"})], prompt=prompt, declaration=declaration)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("未完成操作：打开公司详情", result.user_answer)
        self.assertIn("http://app.test/object/company/company-1", result.user_answer)
        self.assertIn("已到达请求的页面状态", result.user_answer)
        self.assertEqual(1, _RemoteBrowser.instances[-1].actions.count("goto"))

    def test_after_facts_old_question_bindings_are_rejected_until_explicitly_rebound(self):
        rebound = answer()
        for binding in rebound["answer"]["answers_for"]:
            binding["request_ref"] = "r2-" + binding["request_ref"]
        result, chat = self.execute([
            ("declare_task", {**DECLARATION, "answer_kinds": ["knowledge", "data"]}),
            ("goto", {"url": "/object/company/company-1"}),
            ("finish", answer()), ("finish", rebound)])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("12.50 USD", result.user_answer)
        self.assertIn("r2-a1", str(chat.messages[-1]))
        self.assertEqual(1, _RemoteBrowser.instances[-1].actions.count("goto"))

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
