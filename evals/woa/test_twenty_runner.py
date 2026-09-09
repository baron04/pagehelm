"""Drive the real Web Operation Agent turn path with a scripted provider and no browser.

Every case runs the product identity binding, remote session channel, ``AgentTurnExecutor``
and shared Business Skills/runtime, then scores the assembled artifact with the independent
evaluator. Nothing here calls a model or touches a real Twenty instance.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from evals.pageagent.scripted import (
    SyntheticCompanyExecutor, SyntheticTwentyAdapter, company_ui_path, development_case,
    evaluate_run, query_answer, synthetic_business_runtime, synthetic_companies,
    write_storage_state,
)
from evals.woa.assembler import ENGINE_ANSWER_CEILING
from evals.woa.scripted import ScriptedWoaChat, SimulatedRuntime, SyntheticPageModel
from evals.woa.twenty_runner import WoaTwentyConfig, WoaTwentyRunnerError, run_case
from web_operation_agent.business import (
    BusinessToolIdentityError, BusinessToolUnavailableError,
)
from web_operation_agent.schemas import SourceMap


ORIGIN = "http://127.0.0.1:3020"
SKILL_ID = "synthetic.companies-query"


class WoaRunnerScenarioTests(unittest.TestCase):
    def setUp(self):
        self.companies = synthetic_companies(3)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = write_storage_state(self.root / "state.json", origin=ORIGIN)
        self.storage_state = json.loads(self.state.read_text(encoding="utf-8"))
        self.executor = SyntheticCompanyExecutor(self.companies)
        self.runtime_tools = synthetic_business_runtime(self.executor)
        self.adapter = SyntheticTwentyAdapter(origin=ORIGIN, runtime=self.runtime_tools)
        self.page_model = SyntheticPageModel(self.companies)
        self.runtimes: list[SimulatedRuntime] = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.stop()
        self.directory.cleanup()

    def _factory(self, *, replace_cookie_after_start: bool = False,
                 approval_decision: str | None = None):
        def factory(*, channel, grant, registry, origin, entry_path):
            runtime = SimulatedRuntime(
                channel, grant, origin=origin, entry_path=entry_path,
                page_model=self.page_model, storage_state=self.storage_state,
                approval_decision=approval_decision)
            if replace_cookie_after_start:
                runtime.replace_session_cookie()
            self.runtimes.append(runtime)
            return runtime
        return factory

    @staticmethod
    def _sampler(runtime):
        _title, content = runtime.page_model.render(
            runtime.current_path)
        return content

    def _run(self, *, case, chat, adapter=None, cancelled=None,
             replace_cookie_after_start=False, config=None, run_id="woa-offline-run",
             source_map=None, approval_decision=None):
        return run_case(
            case=case, adapter=adapter or self.adapter, client=chat,
            config=config or WoaTwentyConfig(storage_state_path=self.state, wall_time_s=60),
            ledger_path=self.root / "audit.jsonl",
            runtime_factory=self._factory(
                replace_cookie_after_start=replace_cookie_after_start,
                approval_decision=approval_decision),
            page_sampler=self._sampler, run_id=run_id, cancelled=cancelled,
            source_map=source_map)

    def _query_replies(self, answer: str | None = None):
        return [
            ("load_skill", {"skill_id": SKILL_ID}),
            ("query_companies", {"limit": 50}),
            (None, answer if answer is not None else query_answer(self.companies)),
        ]

    def _open_replies(self, target):
        return [
            ("load_skill", {"skill_id": SKILL_ID}),
            ("query_companies", {"name_contains": target["name"], "limit": 20}),
            ("goto", {"url": company_ui_path(target)}),
            ("finish", {"done": True, "why": "The requested company record is open."}),
        ]

    def test_query_success_binds_identity_and_passes_the_evaluator(self):
        case = development_case("dev-query-en-1", self.companies)
        chat = ScriptedWoaChat(self._query_replies())
        result = self._run(case=case, chat=chat)

        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["apparatus_code"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["kind"], "woa_twenty_run")
        self.assertEqual(result["engine"]["name"], "web_operation_agent")
        self.assertTrue(result["identity"]["principal_bound"])
        self.assertTrue(result["identity"]["session_binding_unchanged"])
        self.assertEqual(result["turn"]["terminal_reason"], "agent_finished")
        self.assertEqual(result["trial"]["business_calls"], 1)
        self.assertEqual(result["trial"]["accounting"]["model_calls"], chat.dispatched)
        self.assertEqual(result["trial"]["accounting"]["usage_unavailable_calls"], 0)
        self.assertEqual(result["trial"]["accounting"]["total_cost_cny"], 0.0)
        self.assertEqual(
            sorted(row["name"] for row in result["evidence"]["companies"]),
            sorted(company["name"] for company in self.companies))
        self.assertLess(result["turn"]["answer_chars"], ENGINE_ANSWER_CEILING)
        self.assertFalse(result["turn"]["answer_at_engine_ceiling"])
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_browser_only_query_reads_page_without_business_capabilities(self):
        self.page_model = SyntheticPageModel(
            self.companies, expose_company_list=True)
        case = development_case("dev-query-en-1", self.companies)
        chat = ScriptedWoaChat([
            ("finish", {"done": True, "why": query_answer(self.companies)}),
        ])
        result = self._run(
            case=case, chat=chat,
            config=WoaTwentyConfig(
                storage_state_path=self.state, capability_profile="browser_only",
                wall_time_s=60))

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["trial"]["business_calls"], 0)
        self.assertNotIn("load_skill", chat.requests[0]["tool_names"])
        self.assertNotIn("query_companies", chat.requests[0]["tool_names"])
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_browser_only_open_uses_an_exact_visible_link(self):
        self.page_model = SyntheticPageModel(
            self.companies, expose_company_list=True)
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        route = company_ui_path(target)
        target_index = self.companies.index(target) + 1
        chat = ScriptedWoaChat([
            ("click", {"ref": f"company-{target_index}", "expect_navigation": True}),
            ("finish", {"done": True, "why": "Opened."}),
        ])
        result = self._run(
            case=case, chat=chat,
            config=WoaTwentyConfig(
                storage_state_path=self.state, capability_profile="browser_only",
                wall_time_s=60))

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["trial"]["business_calls"], 0)
        self.assertEqual(result["trial"]["page_actions"], 1)
        self.assertEqual(result["evidence"]["page"]["path"], route)
        self.assertIn(target["name"], result["evidence"]["page"]["visible_projection"])
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_source_pair_exposes_the_same_tool_surface_and_binds_source_mode(self):
        case = development_case("dev-query-en-1", self.companies)
        off_chat = ScriptedWoaChat(self._query_replies())
        on_chat = ScriptedWoaChat(self._query_replies())
        paired = {"capability_profile": "skill_api", "source_interface": "paired",
                  "wall_time_s": 60}
        off = self._run(
            case=case, chat=off_chat, run_id="woa-source-off",
            config=WoaTwentyConfig(
                storage_state_path=self.state, source_mode="off", **paired))
        on = self._run(
            case=case, chat=on_chat, run_id="woa-source-on",
            config=WoaTwentyConfig(
                storage_state_path=self.state, source_mode="on", **paired),
            source_map=SourceMap(1, "synthetic-attestation", "synthetic/source", "v1", []))

        self.assertEqual(off["status"], "completed", off)
        self.assertEqual(on["status"], "completed", on)
        self.assertEqual(off["engine"]["source_interface"], "paired")
        self.assertEqual((off["engine"]["source_mode"], on["engine"]["source_mode"]),
                         ("off", "on"))
        self.assertEqual(off_chat.requests[0]["tool_names"], on_chat.requests[0]["tool_names"])
        self.assertIn("open_source_candidate", on_chat.requests[0]["tool_names"])

        events = [json.loads(line) for line in
                  (self.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        delivered = {
            run_id: [event for event in events
                     if event["run_id"] == run_id
                     and event["event"] == "source_affordances_delivered"]
            for run_id in ("woa-source-off", "woa-source-on")
        }
        self.assertTrue(delivered["woa-source-off"])
        self.assertTrue(delivered["woa-source-on"])
        self.assertTrue(all(
            event["candidates"] == []
            for rows in delivered.values() for event in rows))

    def test_create_requires_a_fresh_exact_effect_approval_and_dispatches_once(self):
        before_companies = [dict(item) for item in self.companies]
        executor = SyntheticCompanyExecutor(before_companies, allow_mutations=True)
        adapter = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=synthetic_business_runtime(executor))
        case = development_case("dev-create-zh-1", before_companies)
        task = case["evaluator_task"]
        chat = ScriptedWoaChat([
            ("load_skill", {"skill_id": "synthetic.companies-create"}),
            ("create_company", {"name": task["name"], "domain": task["domain"]}),
            ("finish", {"done": True, "why": "Created after approval."}),
        ])
        result = self._run(
            case=case, chat=chat, adapter=adapter, approval_decision="approved",
            config=WoaTwentyConfig(
                storage_state_path=self.state, allow_create=True, wall_time_s=60),
            run_id="woa-create-approved")

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(executor.mutation_dispatches, 1)
        self.assertEqual(result["trial"]["approvals_requested"], 1)
        self.assertEqual(result["trial"]["approvals_approved"], 1)
        self.assertEqual(result["trial"]["approvals_denied"], 0)
        self.assertEqual(result["evidence"]["write_receipt"], {
            "dispatch_count": 1, "acknowledged": True, "same_actor_readback": True})
        self.assertEqual(len(self.runtimes[-1].approval_requests), 1)
        request = self.runtimes[-1].approval_requests[0]
        self.assertEqual(request["effect"], "Create one company in Synthetic Twenty")
        self.assertIn(task["name"], request["targets"][0])

        from evals.twenty.evaluator import evaluate, make_state
        from evals.pageagent.scripted import SYNTHETIC_FIXTURE, evaluator_projection
        before = evaluator_projection(before_companies)
        after = evaluator_projection(executor.companies)
        evaluation = evaluate(
            task=task,
            before=make_state(
                fixture=SYNTHETIC_FIXTURE, companies=before["companies"],
                guards=before["guards"], captured_at="2026-09-09T00:00:00Z",
                role="baseline"),
            after=make_state(
                fixture=SYNTHETIC_FIXTURE, companies=after["companies"],
                guards=after["guards"], captured_at="2026-09-09T00:00:01Z",
                role="observation"),
            evidence=result["evidence"])
        self.assertEqual(evaluation["status"], "PASS", evaluation)

    def test_denied_create_is_a_valid_failure_and_never_dispatches(self):
        executor = SyntheticCompanyExecutor(self.companies, allow_mutations=True)
        adapter = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=synthetic_business_runtime(executor))
        case = development_case("dev-create-zh-1", self.companies)
        task = case["evaluator_task"]
        result = self._run(
            case=case, adapter=adapter, approval_decision="denied",
            chat=ScriptedWoaChat([
                ("load_skill", {"skill_id": "synthetic.companies-create"}),
                ("create_company", {"name": task["name"], "domain": task["domain"]}),
            ]),
            config=WoaTwentyConfig(
                storage_state_path=self.state, allow_create=True, wall_time_s=60),
            run_id="woa-create-denied")

        self.assertEqual((result["status"], result["error_code"]),
                         ("failed", "approval_denied"))
        self.assertEqual(executor.mutation_dispatches, 0)
        self.assertEqual(result["trial"]["approvals_requested"], 1)
        self.assertEqual(result["trial"]["approvals_denied"], 1)

    def test_open_success_uses_a_disclosed_route_and_the_final_visible_page(self):
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        result = self._run(case=case, chat=ScriptedWoaChat(self._open_replies(target)))

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["page"], {
            "path": company_ui_path(target), "name": target["name"],
            "domain": target["domain"]})
        self.assertEqual(result["final_page"]["path"], company_ui_path(target))
        self.assertEqual(result["trial"]["page_actions"], 1)
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "PASS")

    def test_open_without_a_disclosed_route_cannot_produce_page_evidence(self):
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        chat = ScriptedWoaChat([
            ("load_skill", {"skill_id": SKILL_ID}),
            ("goto", {"url": company_ui_path(target)}),
            ("finish", {"done": True, "why": "Opened without asking the API first."}),
        ])
        result = self._run(case=case, chat=chat)

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "evidence_contract")
        self.assertEqual(result["turn"]["route_rejections"], 1)
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "FAIL")

    def test_wrong_or_repaired_answers_fail_without_repair(self):
        case = development_case("dev-query-en-1", self.companies)
        for answer in (query_answer(self.companies[:2]),
                       "```json\n" + query_answer(self.companies) + "\n```",
                       query_answer(self.companies, complete=False),
                       "The companies are listed above."):
            with self.subTest(answer=answer[:40]):
                result = self._run(
                    case=case, chat=ScriptedWoaChat(self._query_replies(answer)))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], "evidence_contract")
                self.assertEqual(result["evidence"], {
                    "artifact_version": 1, "execution_status": "failed"})
                self.assertEqual(
                    evaluate_run(case, self.companies, result)["status"], "FAIL")

    def test_invalid_tool_arguments_are_a_scorable_engine_failure(self):
        case = development_case("dev-query-en-1", self.companies)
        chat = ScriptedWoaChat([
            ("load_skill", {"skill_id": SKILL_ID}),
            ("query_companies", {"limit": 999}),
            (None, query_answer(self.companies)),
        ])
        result = self._run(case=case, chat=chat)

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"], result)
        # The rejected call never becomes a business result, so the Agent has nothing to
        # answer from and refuses. That refusal is the engine's own scorable failure.
        self.assertEqual(result["error_code"], "agent_refused")
        self.assertEqual(result["turn"]["business_rejections"], 1)
        self.assertEqual(result["trial"]["business_calls"], 1)
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "FAIL")

    def test_business_api_outage_invalidates_the_trial(self):
        case = development_case("dev-query-en-1", self.companies)
        executor = SyntheticCompanyExecutor(
            self.companies, failure=BusinessToolUnavailableError("synthetic API outage"))
        adapter = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=synthetic_business_runtime(executor))
        result = self._run(
            case=case, chat=ScriptedWoaChat(self._query_replies()), adapter=adapter)

        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "transport_contract")
        self.assertEqual(result["error_code"], "business_unavailable")
        self.assertEqual(result["evidence"]["execution_status"], "failed")

    def test_provider_outage_invalidates_the_trial_with_unknown_cost(self):
        case = development_case("dev-query-en-1", self.companies)
        chat = ScriptedWoaChat(self._query_replies(), failures={1: "provider_http"})
        result = self._run(case=case, chat=chat)

        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "provider_outage")
        self.assertEqual(result["error_code"], "provider_http")
        accounting = result["trial"]["accounting"]
        self.assertEqual(accounting["model_calls"], 1)
        self.assertEqual(accounting["usage_unavailable_calls"], 1)
        self.assertIsNone(accounting["total_cost_cny"])

    def test_identity_binding_failure_never_reaches_the_model(self):
        case = development_case("dev-query-en-1", self.companies)
        adapter = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=self.runtime_tools,
            identity_failure=BusinessToolIdentityError(
                "page session and delegated API differ"))
        chat = ScriptedWoaChat(self._query_replies())
        result = self._run(case=case, chat=chat, adapter=adapter)

        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "identity_binding")
        self.assertEqual(chat.dispatched, 0)
        self.assertFalse(result["identity"]["principal_bound"])

    def test_replaced_browser_session_invalidates_even_a_claimed_success(self):
        case = development_case("dev-query-en-1", self.companies)
        result = self._run(
            case=case, chat=ScriptedWoaChat(self._query_replies()),
            replace_cookie_after_start=True)

        self.assertEqual(result["status"], "apparatus_invalid")
        self.assertEqual(result["apparatus_code"], "credential_or_identity")
        self.assertEqual(result["error_code"], "session_changed")
        self.assertFalse(result["identity"]["session_binding_unchanged"])
        self.assertEqual(result["evidence"]["execution_status"], "failed")

    def test_cancellation_before_and_during_a_turn_is_a_scorable_engine_failure(self):
        case = development_case("dev-query-en-1", self.companies)
        before = threading.Event()
        before.set()
        chat = ScriptedWoaChat(self._query_replies())
        result = self._run(case=case, chat=chat, cancelled=before)

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "user_cancelled")
        self.assertTrue(result["cancelled"])
        self.assertEqual(chat.dispatched, 0)
        self.assertEqual(result["evidence"]["execution_status"], "failed")

        # An in-flight task is stopped the way the panel stops it: by cancelling that exact
        # message through the authenticated channel.
        chat = ScriptedWoaChat(
            self._query_replies(),
            after_call=lambda sequence: (
                self.runtimes[-1].cancel_active() if sequence == 1 else None))
        result = self._run(case=case, chat=chat)

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "user_cancelled")
        self.assertEqual(result["turn"]["terminal_reason"], "user_cancelled")
        self.assertEqual(result["evidence"]["execution_status"], "failed")

    def test_step_budget_is_a_scorable_engine_failure(self):
        case = development_case("dev-query-en-1", self.companies)
        chat = ScriptedWoaChat([("load_skill", {"skill_id": SKILL_ID})] * 6)
        result = self._run(
            case=case, chat=chat,
            config=WoaTwentyConfig(
                storage_state_path=self.state, wall_time_s=60, step_budget=2))

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["apparatus_code"])
        self.assertEqual(result["error_code"], "step_budget")
        self.assertEqual(evaluate_run(case, self.companies, result)["status"], "FAIL")

    def test_artifact_is_schema_fixed_and_free_of_prompt_and_credentials(self):
        case = development_case("dev-open-en-1", self.companies)
        target = next(company for company in self.companies
                      if company["name"] in case["prompt"])
        result = self._run(case=case, chat=ScriptedWoaChat(self._open_replies(target)))

        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(case["prompt"], rendered)
        self.assertNotIn("synthetic-session-1", rendered)
        self.assertNotIn(str(self.state), rendered)
        for company in self.companies:
            if company is not target:
                self.assertNotIn(company["name"], rendered)
        self.assertRegex(result["run_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result["runtime_trace_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(set(result["case"]), {
            "set_id", "split", "task_id", "task_digest", "template_family_digest",
            "evaluator_task"})


class WoaRunnerBoundaryTests(unittest.TestCase):
    def test_config_rejects_unsupported_routes_profiles_and_source_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text("{}", encoding="utf-8")
            for path in ("//other.test/x", "/objects/../admin", "/objects?q=1", "/objects#x"):
                with self.subTest(path=path), self.assertRaisesRegex(
                        WoaTwentyRunnerError, "relative application path"):
                    WoaTwentyConfig(storage_state_path=state, entry_path=path)
            with self.assertRaisesRegex(WoaTwentyRunnerError, "capability_profile"):
                WoaTwentyConfig(storage_state_path=state, capability_profile="unknown")
            self.assertEqual(
                WoaTwentyConfig(storage_state_path=state, capability_profile="browser_only")
                .capability_profile, "browser_only")
            with self.assertRaisesRegex(WoaTwentyRunnerError, "paired source interface"):
                WoaTwentyConfig(storage_state_path=state, source_mode="on")
            paired_on = WoaTwentyConfig(
                storage_state_path=state, source_mode="on", source_interface="paired")
            self.assertEqual(paired_on.source_mode, "on")
            missing = WoaTwentyConfig(storage_state_path=Path(directory) / "absent.json")
            with self.assertRaisesRegex(WoaTwentyRunnerError, "storage state"):
                missing.verify_files()

    def test_create_cases_require_explicit_write_configuration(self):
        companies = synthetic_companies(2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = write_storage_state(root / "state.json", origin=ORIGIN)
            executor = SyntheticCompanyExecutor(companies)
            adapter = SyntheticTwentyAdapter(
                origin=ORIGIN, runtime=synthetic_business_runtime(executor))
            chat = ScriptedWoaChat([])
            with self.assertRaisesRegex(ValueError, "allow_create"):
                run_case(
                    case=development_case("dev-create-en-1", companies), adapter=adapter,
                    client=chat, config=WoaTwentyConfig(storage_state_path=state),
                    ledger_path=root / "audit.jsonl",
                    runtime_factory=lambda **_kwargs: None, run_id="woa-refused")
            self.assertEqual(chat.dispatched, 0)


if __name__ == "__main__":
    unittest.main()
