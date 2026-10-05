"""A valid UI reference cannot stand in for an independently requested data answer."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.answer.evidence import TurnEvidence
from web_operation_agent.answer.page_answer import collect
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.execution import turn
from web_operation_agent.execution.request_contract import (
    check_answer_evidence, parse_requirements)
from web_operation_agent.schemas import ElementRef
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.execution.test_page_answer import _PageBrowser, page
from web_operation_agent.tests.support.answer import _answer
from web_operation_agent.tests.support.business import _Chat


def observation(epoch=2):
    obs = page(epoch)
    obs.elements.append(ElementRef("e5", "tab", "Details", "button", epoch,
                                  {"aria-selected": "true"}))
    obs.content += '\n[5]<button role="tab" aria-selected="true">Details />'
    return obs


def declaration(prompt, kind="data"):
    return {"action_request": "", "action_kind": "none",
            "answer_requests": [prompt], "answer_kinds": [kind]}


def bound_answer(ref):
    payload = _answer("page_observation", ref)
    payload["answer"]["answers_for"] = [{"request_ref": "a1", "evidence_refs": [ref]}]
    return payload


class AnswerBindingKinds(unittest.TestCase):
    def test_kind_declaration_is_required_bounded_and_frozen_before_results(self):
        args = declaration("What is the phone number?")
        for kinds in (None, [], [True], ["other"], ["data", "ui"]):
            with self.subTest(kinds=kinds), self.assertRaises(ValueError):
                parse_requirements({**args, "answer_kinds": kinds}, args["answer_requests"][0])
        absent = deepcopy(args)
        del absent["answer_kinds"]
        with self.assertRaises(ValueError):
            parse_requirements(absent, args["answer_requests"][0])
        req = parse_requirements(args, args["answer_requests"][0])
        self.assertEqual(req.answer_kinds, ("data",))
        self.assertIn('"a1": "data"', req.render())

    def test_selected_control_and_its_serialized_text_cannot_settle_data_or_knowledge(self):
        evidence = TurnEvidence()
        evidence.capture(collect(observation(), observed_at="now"), namespace="obs1")
        ui_refs = [ref for ref in evidence.ids() if evidence.get(ref).saw == "ui_state"]
        self.assertEqual(len(ui_refs), 2)
        for kind in ("data", "knowledge"):
            req = parse_requirements(declaration("What is the phone number?", kind),
                                     "What is the phone number?")
            for ref in ui_refs:
                with self.subTest(kind=kind, ref=ref), self.assertRaisesRegex(ValueError, "only UI"):
                    check_answer_evidence(req, [{"request_ref": "a1", "evidence_refs": [ref]}], evidence)
            # Adding a real value does not launder the unrelated reference.
            with self.assertRaisesRegex(ValueError, "only UI"):
                check_answer_evidence(req, [{"request_ref": "a1", "evidence_refs": [
                    "obs1-page#1", ui_refs[0]]}], evidence)

    def test_explicit_ui_question_can_still_report_selected_state(self):
        evidence = TurnEvidence()
        evidence.capture(collect(observation(), observed_at="now"), namespace="obs1")
        req = parse_requirements(declaration("Which tab is selected?", "ui"), "Which tab is selected?")
        check_answer_evidence(req, [{"request_ref": "a1", "evidence_refs": ["obs1-page#5"]}], evidence)

    def test_link_and_button_business_content_is_not_assumed_to_be_ui_only(self):
        obs = observation()
        obs.elements = []
        obs.content = ('[1]<a href=/records/7>Grace Hopper />\n'
                       '[2]<button >Invoice 812.00 USD />\n'
                       '[3]<div role="tab">Details />')
        items = collect(obs, observed_at="now")
        self.assertEqual([item.saw for item in items], ['text', 'text', 'ui_state'])

    def test_real_values_and_document_content_remain_available_but_expired_refs_do_not(self):
        evidence = TurnEvidence()
        evidence.capture(collect(observation(), observed_at="now"), namespace="obs1")
        req = parse_requirements(declaration("What is the phone number?"), "What is the phone number?")
        for ref in ("obs1-page#1", "obs1-page#6"):
            check_answer_evidence(req, [{"request_ref": "a1", "evidence_refs": [ref]}], evidence)
        evidence.discard_pages()
        with self.assertRaisesRegex(ValueError, "expired"):
            check_answer_evidence(req, [{"request_ref": "a1", "evidence_refs": ["obs1-page#1"]}], evidence)


class _SelectedBrowser(_PageBrowser):
    def execute(self, *args, **kwargs):
        receipt = super().execute(*args, **kwargs)
        return replace(receipt, observation=observation(self.epoch))


class AnswerBindingRecovery(unittest.TestCase):
    def execute(self, script, prompt="What is the phone number?", kind="data"):
        with tempfile.TemporaryDirectory() as tmp:
            chat = _Chat([("declare_task", declaration(prompt, kind)), *script])
            executor = AgentTurnExecutor(channel=_Channel(), chat=chat, control=ReadOnlyControl(),
                run_id_factory=lambda message: "answer-kind-run",
                config=AgentTurnConfig(supported_scope="Read current page.", require_task_contract=True,
                                       ledger_path=Path(tmp) / "run.jsonl"))
            with patch.object(turn, "RemoteBrowserClient", _SelectedBrowser):
                result = executor.run_once({"message_id": "kinds", "text": prompt,
                                            "page_url": "http://app.test/companies"})
            return result, chat

    def test_same_agent_can_read_real_value_after_valid_but_irrelevant_state_is_refused(self):
        result, chat = self.execute([
            ("observe", {"answer_evidence": True}),
            ("finish", bound_answer("obs2-page#5")),
            ("observe", {"answer_evidence": True, "answer_query": "Phone"}),
            ("finish", bound_answer("obs4-page#1")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("123-456", result.user_answer)
        self.assertNotIn("selected", result.user_answer)
        self.assertIn("only UI control/state evidence", str(chat.messages))
        self.assertNotIn("goto", chat.tool_names[-1])
        self.assertNotIn("click", chat.tool_names[-1])
        self.assertIn("observe", chat.tool_names[-1])

    def test_repeated_irrelevant_answers_end_with_unanswered_question_not_success(self):
        result, _ = self.execute([
            ("observe", {"answer_evidence": True}),
            *([("finish", bound_answer("obs2-page#5"))] * 3),
        ])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertIn("Unanswered request: What is the phone number?", result.user_answer)
        self.assertNotIn("selected", result.user_answer)

    def test_ui_question_is_answerable_through_the_same_product_path(self):
        result, _ = self.execute([
            ("observe", {"answer_evidence": True}),
            ("finish", bound_answer("obs2-page#5")),
        ], prompt="Which tab is selected?", kind="ui")
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("Details", result.user_answer)
        self.assertIn("selected", result.user_answer)


if __name__ == "__main__":
    unittest.main()
