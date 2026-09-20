from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import unittest

from evals.woa.assembler import (
    ENGINE_ANSWER_CEILING, FinalPageSample, TurnEvidence, WoaRunAssemblyError,
    assemble_woa_run, permitted_routes, utc_now,
)
from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import BusinessToolResult
from web_operation_agent.execution.product import AgentTurnExecution, AgentTurnMessage, AgentTurnStatus
from web_operation_agent.execution.session import RunResult


COMPANY = {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "Synthetic One", "domain": "one.synthetic.example",
}
ROUTE = f"/object/company/{COMPANY['id']}"
_ACCOUNTING = {
    "model_calls": 2, "usage_unavailable_calls": 0,
    "provider_reported_models": ["scripted"], "provider_request_ids_digest": "sha256:" + "a" * 64,
    "known_input_tokens": 200, "known_output_tokens": 40, "known_cached_tokens": 0,
    "known_reasoning_tokens": 10, "known_cost_cny": 0.0, "total_cost_cny": 0.0,
    "spend_ledger_slice_digest": "sha256:" + "a" * 64,
}


def _case(kind="query"):
    task = {"artifact_version": 1, "kind": kind}
    if kind == "open":
        task["company_id"] = COMPANY["id"]
    return {
        "artifact_version": 1, "kind": "twenty_execution_case",
        "set_id": "twenty-development", "split": "development", "task_id": "dev-query-one",
        "template_family_digest": "sha256:" + "a" * 64,
        "task_digest": "sha256:" + "b" * 64,
        "prompt": "List every company and domain.", "evaluator_task": task,
    }


def _results():
    return ({
        "companies": [{"name": COMPANY["name"], "domain": COMPANY["domain"],
                       "ui_path": ROUTE}],
        "completeness": {"source_complete": True, "result_truncated": False},
    },)


def _answer():
    return json.dumps({
        "artifact_version": 1, "execution_status": "completed", "complete": True,
        "companies": [{"name": COMPANY["name"], "domain": COMPANY["domain"]}],
    })


def _execution(*, terminal_reason="agent_finished", answer=None,
               page_actions=0) -> AgentTurnExecution:
    structured_query = terminal_reason == "agent_finished" and page_actions == 0
    agent = RunResult(
        run_id="woa-turn", task_ref="woa-turn", arm="U", terminal_reason=terminal_reason,
        steps_used=3, page_actions=page_actions,
        agent_said=answer if answer is not None else _answer(),
        business_answer_projected=structured_query)
    business_result = (BusinessToolResult(
        content=_results()[0], audit={}, allowed_routes=(ROUTE,),
        query_answer=StructuredQueryAnswer(
            collection="companies", fields=("name", "domain"),
            records=((COMPANY["name"], COMPANY["domain"]),),
            source="synthetic typed API", scope="synthetic_workspace",
            observed_at="2026-09-10T00:00:00+00:00",
            source_complete=True, result_truncated=False, matched_count=1,
            source_total=1, records_scanned=1,
        ),
    ) if structured_query else None)
    return AgentTurnExecution(
        schema_version=1, run_id="woa-turn", session_id="woa-session",
        message=AgentTurnMessage(
            message_id="m1", text="List companies", page_url="http://app.test/companies"),
        entry_observation_id="observation-1", entry_observation_digest="sha256:" + "c" * 64,
        status=(AgentTurnStatus.FINISHED if terminal_reason == "agent_finished"
                else AgentTurnStatus.FAILED),
        terminal_reason=terminal_reason,
        user_answer=answer if answer is not None else _answer(),
        agent=agent, business_result=business_result, answer_fallback_used=False,
        started_at=1.0, finished_at=2.0)


def _assemble(evidence: TurnEvidence, *, case=None) -> dict:
    return assemble_woa_run(
        case=case or _case(), run_id="woa-assembly", evidence=evidence,
        capability_profile="skill_api", source_mode="off",
        source_interface="absent",
        started_at=utc_now(), started_monotonic=time.monotonic())


class WoaAssemblerTests(unittest.TestCase):
    def test_a_finished_query_turn_becomes_completed_evidence(self):
        result = _assemble(TurnEvidence(
            execution=_execution(), accounting=_ACCOUNTING,
            business_results=_results(), session_binding_unchanged=True,
            principal_bound=True))

        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["apparatus_code"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["kind"], "woa_twenty_run")
        self.assertEqual(result["engine"]["name"], "web_operation_agent")
        self.assertEqual(result["evidence"]["companies"],
                         [{"name": COMPANY["name"], "domain": COMPANY["domain"]}])
        self.assertEqual(result["trial"]["accounting"], _ACCOUNTING)
        self.assertFalse(result["turn"]["answer_at_engine_ceiling"])
        self.assertRegex(result["run_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_an_answer_at_the_engine_ceiling_is_reported_not_hidden(self):
        padded = _answer() + " " * (ENGINE_ANSWER_CEILING - len(_answer()))
        result = _assemble(TurnEvidence(
            execution=_execution(answer=padded), accounting=_ACCOUNTING,
            business_results=_results(), session_binding_unchanged=True))

        self.assertTrue(result["turn"]["answer_at_engine_ceiling"])
        self.assertEqual(result["turn"]["answer_chars"], ENGINE_ANSWER_CEILING)

    def test_open_evidence_requires_a_permitted_route_and_agreeing_observation(self):
        events = ({"event": "business_route_opened",
                   "url_digest": hashlib.sha256(ROUTE.encode()).hexdigest()},)
        page = FinalPageSample(
            document_url="http://app.test" + ROUTE,
            observation_url="http://app.test" + ROUTE,
            observation_id="observation-2", observation_digest="sha256:" + "d" * 64,
            visible_projection=f"{COMPANY['name']}\nDomain\n{COMPANY['domain']}")
        evidence = TurnEvidence(
            execution=_execution(answer="Opened it.", page_actions=1),
            ledger_events=events, accounting=_ACCOUNTING, business_results=_results(),
            final_page=page, session_binding_unchanged=True)
        result = _assemble(evidence, case=_case("open"))

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["page"], {
            "path": ROUTE, "name": COMPANY["name"], "domain": COMPANY["domain"]})
        self.assertEqual(permitted_routes(events, _results()), [ROUTE])

        # Without the server's own route permit the same page cannot be used as evidence.
        unpermitted = _assemble(
            dataclasses.replace(evidence, ledger_events=()), case=_case("open"))
        self.assertEqual(unpermitted["status"], "failed")
        self.assertEqual(unpermitted["error_code"], "evidence_contract")

        # A document that disagrees with the last observation cannot be used either.
        drifted = dataclasses.replace(
            page, observation_url="http://app.test/objects/companies")
        result = _assemble(
            dataclasses.replace(evidence, final_page=drifted), case=_case("open"))
        self.assertEqual(result["error_code"], "evidence_contract")

    def test_typed_failures_are_classified_without_reading_exception_text(self):
        cases = {
            "provider_outage": (
                "provider_error",
                ({"event": "provider_failed", "provider_error_code": "provider_http"},),
                "provider_http"),
            "transport_contract": (
                "agent_finished",
                ({"event": "business_tool_rejected", "error_code": "business_unavailable"},),
                "business_unavailable"),
            "credential_or_identity": (
                "agent_finished",
                ({"event": "business_tool_rejected", "error_code": "business_identity"},),
                "business_identity"),
        }
        for apparatus, (terminal, events, code) in cases.items():
            with self.subTest(apparatus=apparatus):
                result = _assemble(TurnEvidence(
                    execution=_execution(terminal_reason=terminal), ledger_events=events,
                    accounting=_ACCOUNTING, business_results=_results(),
                    session_binding_unchanged=True))
                self.assertEqual(result["status"], "apparatus_invalid")
                self.assertEqual(result["apparatus_code"], apparatus)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["evidence"]["execution_status"], "failed")

        for terminal in ("agent_refused", "step_budget", "user_cancelled", "approval_denied"):
            with self.subTest(terminal=terminal):
                result = _assemble(TurnEvidence(
                    execution=_execution(terminal_reason=terminal, answer="stopped"),
                    accounting=_ACCOUNTING, business_results=_results(),
                    session_binding_unchanged=True))
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["apparatus_code"])
                self.assertEqual(result["error_code"], terminal)

    def test_indeterminate_write_is_preserved_and_never_scored_as_failure(self):
        result = _assemble(TurnEvidence(
            execution=_execution(terminal_reason="indeterminate_write", answer="uncertain"),
            accounting=_ACCOUNTING, business_results=_results(),
            session_binding_unchanged=True))

        self.assertEqual(result["status"], "indeterminate")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["evidence"], {
            "artifact_version": 1, "execution_status": "indeterminate"})

    def test_a_replaced_session_invalidates_even_a_finished_turn(self):
        result = _assemble(TurnEvidence(
            execution=_execution(), accounting=_ACCOUNTING, business_results=_results(),
            session_binding_unchanged=False))

        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "session_changed")

    def test_a_missing_turn_and_a_pre_run_cancellation_are_distinguished(self):
        missing = _assemble(TurnEvidence(accounting=_ACCOUNTING))
        self.assertEqual(missing["status"], "apparatus_invalid")
        self.assertEqual(missing["error_code"], "turn_missing")

        cancelled = _assemble(TurnEvidence(accounting=_ACCOUNTING, cancelled=True))
        self.assertEqual(cancelled["status"], "failed")
        self.assertIsNone(cancelled["apparatus_code"])
        self.assertEqual(cancelled["error_code"], "user_cancelled")

    def test_the_artifact_is_schema_fixed_and_carries_no_prompt_or_answer_text(self):
        case = _case()
        result = _assemble(TurnEvidence(
            execution=_execution(), accounting=_ACCOUNTING, business_results=_results(),
            session_binding_unchanged=True, principal_bound=True))

        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(case["prompt"], rendered)
        self.assertNotIn("woa-session", rendered)
        self.assertNotIn("observation-1", rendered)
        self.assertEqual(set(result["case"]), {
            "set_id", "split", "task_id", "task_digest", "template_family_digest",
            "evaluator_task"})
        self.assertEqual(set(result["identity"]),
                         {"principal_bound", "session_binding_unchanged"})

    def test_invalid_assembler_inputs_are_refused(self):
        with self.assertRaisesRegex(WoaRunAssemblyError, "capability profile"):
            assemble_woa_run(
                case=_case(), run_id="woa", evidence=TurnEvidence(),
                capability_profile="unknown", source_mode="off",
                source_interface="absent",
                started_at=utc_now(), started_monotonic=time.monotonic())
        with self.assertRaisesRegex(WoaRunAssemblyError, "evidence must be"):
            assemble_woa_run(
                case=_case(), run_id="woa", evidence={},
                capability_profile="skill_api", source_mode="off",
                source_interface="absent",
                started_at=utc_now(), started_monotonic=time.monotonic())
        with self.assertRaisesRegex(WoaRunAssemblyError, "both finish and fail"):
            _assemble(TurnEvidence(
                execution=_execution(), boundary_error_code="stale_message"))


if __name__ == "__main__":
    unittest.main()
