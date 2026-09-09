"""End-to-end no-model qualification of the common schedule driver, ledger and report.

Both engines run through one driver, one evaluator, one projector and one append-only ledger
over a frozen protocol built from the public development task set. The provider is scripted
and the fixture is in memory, so the qualification spends no model tokens, touches no Twenty
instance and reads no private held-out content.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from evals.pageagent.scripted import (
    SyntheticCompanyExecutor, SyntheticTwentyAdapter, company_ui_path,
    synthetic_business_runtime, synthetic_companies, write_storage_state,
)
from evals.twenty.driver import ScheduleDriverError, run_schedule
from evals.twenty.evaluator import EvaluationInputError
from evals.twenty.protocol import freeze_protocol
from evals.twenty.records import append_trial, initialize_batch, verify_batch
from evals.twenty.report import build_report, verify_report
from evals.twenty.taskset import seal_task_set, verify_split_separation
from evals.twenty.trial import project_trial_record
from evals.woa.assembler import utc_now
from evals.woa.qualify_schedule import (
    DEVELOPMENT_TASKSET, READ_ONLY_PREFIX_PAIRS, InMemoryFixture, opaque_task_set,
    qualification_protocol, read_only_prefix,
)
from evals.woa.scripted import ScriptedWoaChat, SimulatedRuntime, SyntheticPageModel
from evals.woa.twenty_runner import WoaTwentyConfig
from evals.woa import twenty_runner as woa_runner


ORIGIN = "http://127.0.0.1:3020"
BATCH_ID = "driver-qualification-v1"
SKILL_ID = "synthetic.companies-query"


def _development() -> dict:
    return json.loads(DEVELOPMENT_TASKSET.read_text(encoding="utf-8"))


class _Harness:
    """Build both engine runner adapters over one synthetic company set."""

    def __init__(self, root: Path, companies: list[dict]):
        self.root = root
        self.companies = companies
        self.state = write_storage_state(root / "state.json", origin=ORIGIN)
        self.storage_state = json.loads(self.state.read_text(encoding="utf-8"))
        self.executor = SyntheticCompanyExecutor(companies)
        self.adapter = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=synthetic_business_runtime(self.executor))
        self.page_model = SyntheticPageModel(companies)
        self.runtimes: list[SimulatedRuntime] = []
        self.calls: list[tuple[int, str, str]] = []

    def close(self):
        for runtime in self.runtimes:
            runtime.stop()

    def _factory(self):
        def factory(*, channel, grant, registry, origin, entry_path):
            runtime = SimulatedRuntime(
                channel, grant, origin=origin, entry_path=entry_path,
                page_model=self.page_model, storage_state=self.storage_state)
            self.runtimes.append(runtime)
            return runtime
        return factory

    def _sampler(self, runtime):
        return self.page_model.render(runtime.current_path)[1]

    def _target(self, case):
        company_id = case["evaluator_task"].get("company_id")
        return next(item for item in self.companies if item["id"] == company_id)

    def _woa_replies(self, case):
        from evals.pageagent.scripted import query_answer

        if case["evaluator_task"]["kind"] == "query":
            return [("load_skill", {"skill_id": SKILL_ID}),
                    ("query_companies", {"limit": 50}),
                    (None, query_answer(self.companies))]
        target = self._target(case)
        return [("load_skill", {"skill_id": SKILL_ID}),
                ("query_companies", {"name_contains": target["name"], "limit": 20}),
                ("goto", {"url": company_ui_path(target)}),
                ("finish", {"done": True, "why": "The requested record is open."})]

    def woa(self, **overrides):
        def adapter(*, case, arm_id, task_id, schedule_position, run_id):
            self.calls.append((schedule_position, arm_id, task_id))
            chat = ScriptedWoaChat(
                overrides.get("replies", self._woa_replies)(case),
                failures=overrides.get("failures"))
            return woa_runner.run_case(
                case=case, adapter=overrides.get("adapter", self.adapter), client=chat,
                config=WoaTwentyConfig(storage_state_path=self.state, wall_time_s=60),
                ledger_path=self.root / "audit.jsonl", runtime_factory=self._factory(),
                page_sampler=self._sampler, run_id=run_id)
        return adapter

    def pageagent(self):
        """A PageAgent artifact assembled from the same synthetic run facts.

        The official bundle is qualified separately in real Chromium. Here the schedule
        driver only needs a second engine that emits the shared projection, so this adapter
        reuses the PageAgent runner's own evidence contract without launching a browser.
        """
        from evals.pageagent.scripted import query_answer
        from evals.twenty.evidence import open_evidence, query_evidence

        def adapter(*, case, arm_id, task_id, schedule_position, run_id):
            self.calls.append((schedule_position, arm_id, task_id))
            started_at = utc_now()
            started = time.monotonic()
            results = [{
                "companies": [{"name": item["name"], "domain": item["domain"],
                               "ui_path": company_ui_path(item)} for item in self.companies],
                "completeness": {"source_complete": True, "result_truncated": False},
            }]
            if case["evaluator_task"]["kind"] == "query":
                evidence = query_evidence(query_answer(self.companies), results)
                page_actions = 0
            else:
                target = self._target(case)
                route = company_ui_path(target)
                evidence = open_evidence(
                    path=route,
                    visible_projection=self.page_model.render(route)[1],
                    business_results=results, permitted_routes=[route])
                page_actions = 1
            digest = "sha256:" + "e" * 64
            return {
                "artifact_version": 1, "kind": "pageagent_twenty_run", "status": "completed",
                "apparatus_code": None, "error_code": None, "run_id": run_id,
                "case": {**{key: case[key] for key in (
                    "set_id", "split", "task_id", "task_digest", "template_family_digest")},
                    "evaluator_task": {"kind": case["evaluator_task"]["kind"]}},
                "evidence": evidence,
                "started_at": started_at, "finished_at": utc_now(),
                "wall_ms": int((time.monotonic() - started) * 1_000),
                "trial": {
                    "accounting": {
                        "model_calls": 2, "usage_unavailable_calls": 0,
                        "provider_reported_models": ["pageagent-scripted-model"],
                        "provider_request_ids_digest": digest,
                        "known_input_tokens": 200, "known_output_tokens": 40,
                        "known_cached_tokens": 0, "known_reasoning_tokens": 10,
                        "known_cost_cny": 0.0, "total_cost_cny": 0.0,
                        "spend_ledger_slice_digest": digest,
                    },
                    "page_actions": page_actions, "business_calls": 1,
                    "approvals_requested": 0, "approvals_approved": 0, "approvals_denied": 0,
                    "automatic_retries": 0, "user_interventions": 0,
                    "safety": {"unauthorized_writes": 0, "duplicate_writes": 0,
                               "forbidden_state_changes": 0, "cross_identity_actions": 0},
                    "session_ledger_digest": digest, "public_answer_digest": digest,
                    "runtime_trace_digest": digest,
                },
                "run_digest": digest,
            }
        return adapter


class ScheduleDriverQualificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.companies = synthetic_companies(3)
        development = _development()
        self.development_seal = seal_task_set(development)
        self.qualification_set = opaque_task_set(
            development, set_id="twenty-companies-qualification-v1")
        self.test_seal = seal_task_set(self.qualification_set)
        self.split = verify_split_separation(self.development_seal, self.test_seal)
        self.protocol = freeze_protocol(
            qualification_protocol(
                sorted(self.test_seal["task_digests"]), seal=self.test_seal,
                development_seal=self.development_seal, split=self.split),
            development_seal=self.development_seal, test_seal=self.test_seal,
            split_verification=self.split)
        self.assertEqual(
            read_only_prefix(self.protocol, self.qualification_set),
            READ_ONLY_PREFIX_PAIRS,
            "the qualification seed must keep its read-only schedule prefix")
        self.fixture = InMemoryFixture(self.companies)
        self.harness = _Harness(self.root, self.companies)
        self.ledger = self.root / "batch.jsonl"
        self.kwargs = {
            "protocol": self.protocol, "development_seal": self.development_seal,
            "test_seal": self.test_seal, "split_verification": self.split,
        }
        initialize_batch(
            self.ledger, batch_id=BATCH_ID, created_at="2026-09-08T23:59:59+00:00",
            **self.kwargs)

    def tearDown(self):
        self.harness.close()
        self.directory.cleanup()

    def _runners(self, **overrides):
        woa = self.harness.woa(**overrides)
        pageagent = self.harness.pageagent()
        return {
            arm["arm_id"]: (woa if arm["engine"] == "web_operation_agent" else pageagent)
            for arm in self.protocol["arms"]
        }

    def _drive(self, *, max_trials=None, runners=None, start_index=0):
        return run_schedule(
            protocol=self.protocol, development_seal=self.development_seal,
            test_seal=self.test_seal, split_verification=self.split,
            task_set=self.qualification_set, fixture=self.fixture,
            runners=runners or self._runners(), ledger_path=self.ledger,
            batch_id=BATCH_ID, start_index=start_index, max_trials=max_trials)

    def test_both_engines_pass_through_one_driver_evaluator_ledger_and_report(self):
        outcomes = self._drive(max_trials=6)

        self.assertEqual(len(outcomes), 6)
        self.assertEqual([outcome.evaluation_status for outcome in outcomes], ["PASS"] * 6)
        self.assertEqual([outcome.trial_status for outcome in outcomes], ["valid"] * 6)
        engines = {
            outcome.arm_id: next(
                arm["engine"] for arm in self.protocol["arms"]
                if arm["arm_id"] == outcome.arm_id)
            for outcome in outcomes
        }
        self.assertIn("web_operation_agent", engines.values())
        self.assertTrue({"pageagent", "pageagent_core"} & set(engines.values()))
        kinds = {
            next(task["kind"] for task in self.qualification_set["tasks"]
                 if task["task_id"] == outcome.task_id)
            for outcome in outcomes
        }
        self.assertEqual(kinds, {"query", "open"})

        status = verify_batch(self.ledger, **self.kwargs)
        self.assertEqual(status["completed_trials"], 6)
        self.assertEqual(status["outcomes"]["PASS"], 6)

        report = build_report(ledger_path=self.ledger, **self.kwargs)
        self.assertEqual(verify_report(report)["status"], "READY")
        self.assertEqual(report["purpose"], "apparatus_qualification")
        self.assertFalse(report["comparative_claim_supported"])
        self.assertEqual(report["outcomes"]["PASS"], 6)
        self.assertEqual(report["stop_reason"], "batch_open")
        self.assertEqual(report["total_cost_cny"], 0.0)
        rendered = json.dumps(report, ensure_ascii=False)
        for company in self.companies:
            self.assertNotIn(company["name"], rendered)
            self.assertNotIn(company["id"], rendered)
        for task in self.qualification_set["tasks"]:
            self.assertNotIn(task["prompt_template"][:20], rendered)
        self.assertNotIn(BATCH_ID + "-0001", rendered)

    def test_driver_follows_the_frozen_order_and_refuses_to_skip_or_repeat(self):
        self._drive(max_trials=2)
        expected = [
            (self.protocol["schedule"][0]["position"], self.protocol["schedule"][0]["task_id"]),
        ] * 2
        self.assertEqual([(call[0], call[2]) for call in self.harness.calls], expected)

        with self.assertRaisesRegex(ScheduleDriverError, "start index"):
            self._drive(max_trials=1, start_index=0)
        with self.assertRaisesRegex(ScheduleDriverError, "start index"):
            self._drive(max_trials=1, start_index=5)

        self._drive(max_trials=2, start_index=2)
        self.assertEqual(verify_batch(self.ledger, **self.kwargs)["completed_trials"], 4)

    def test_a_runner_bound_to_the_wrong_engine_is_rejected_before_append(self):
        pageagent = self.harness.pageagent()
        runners = {arm["arm_id"]: pageagent for arm in self.protocol["arms"]}
        with self.assertRaisesRegex(EvaluationInputError, "scheduled engine"):
            self._drive(max_trials=2, runners=runners)
        self.assertEqual(verify_batch(self.ledger, **self.kwargs)["completed_trials"], 0)

        with self.assertRaisesRegex(ScheduleDriverError, "outside the frozen protocol"):
            self._drive(max_trials=1, runners={"invented-arm": pageagent})

    def test_evaluation_bound_to_another_fixture_state_fails_before_append(self):
        other = InMemoryFixture(synthetic_companies(2))
        before = other.capture(role="baseline")
        after = self.fixture.capture(role="observation")
        arm_id = self.protocol["schedule"][0]["first_arm"]
        run = self._runners()[arm_id](
            case=_instantiated(self.qualification_set, self.protocol, self.fixture),
            arm_id=arm_id, task_id=self.protocol["schedule"][0]["task_id"],
            schedule_position=1, run_id=f"{BATCH_ID}-0001")
        evaluation = {
            "artifact_version": 1, "kind": "twenty_evaluation", "status": "PASS",
            "task_kind": run["case"]["evaluator_task"]["kind"], "checks": {"passed": True},
            "before_digest": before["digest"], "after_digest": after["digest"],
        }
        with self.assertRaisesRegex(EvaluationInputError, "different baseline"):
            project_trial_record(
                protocol=self.protocol, test_seal=self.test_seal, batch_id=BATCH_ID,
                schedule_index=0, arm_id=arm_id, run=run, evaluation=evaluation,
                before=self.fixture.capture(role="baseline"), after=after)
        self.assertEqual(verify_batch(self.ledger, **self.kwargs)["completed_trials"], 0)

    def test_provider_outage_seals_the_batch_and_keeps_unknown_cost_unknown(self):
        outcomes = self._drive(max_trials=2, runners=self._runners(failures={1: "provider_http"}))

        outage = [item for item in outcomes if item.trial_status == "apparatus_invalid"]
        self.assertTrue(outage, outcomes)
        self.assertEqual(outage[0].apparatus_code, "provider_outage")
        self.assertEqual(outage[0].evaluation_status, "INVALID")
        self.assertEqual(outage[-1].batch_status, "SEALED_INVALID")

        report = build_report(ledger_path=self.ledger, **self.kwargs)
        self.assertEqual(report["stop_reason"], "apparatus_invalid")
        self.assertEqual(report["batch_status"], "SEALED_INVALID")
        self.assertEqual(report["apparatus_codes"]["provider_outage"], 1)
        self.assertGreaterEqual(report["usage_unavailable_calls"], 1)
        self.assertIsNone(report["total_cost_cny"])

        with self.assertRaisesRegex(ScheduleDriverError, "terminal"):
            self._drive(max_trials=1, start_index=len(outcomes))

    def test_identity_failure_seals_the_batch_as_a_credential_fault(self):
        from web_operation_agent.business import BusinessToolIdentityError

        broken = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=synthetic_business_runtime(self.harness.executor),
            identity_failure=BusinessToolIdentityError("page and delegated API differ"))
        outcomes = self._drive(max_trials=2, runners=self._runners(adapter=broken))

        invalid = [item for item in outcomes if item.trial_status == "apparatus_invalid"]
        self.assertTrue(invalid, outcomes)
        self.assertEqual(invalid[0].apparatus_code, "credential_or_identity")
        self.assertEqual(invalid[-1].batch_status, "SEALED_INVALID")

    def test_a_read_only_trial_that_changes_state_stops_the_driver(self):
        class DriftingFixture(InMemoryFixture):
            def __init__(self, companies):
                super().__init__(companies)
                self.drifted = False

            def capture(self, *, role: str):
                # Drop a company just before the trial's after-state is observed, so the
                # driver sees a read-only trial that did not leave the state it started from.
                if role == "observation" and not self.drifted:
                    self.drifted = True
                    self.projection["companies"] = self.projection["companies"][:1]
                return super().capture(role=role)

        self.fixture = DriftingFixture(self.companies)
        with self.assertRaisesRegex(ScheduleDriverError, "changed the fixture state"):
            self._drive(max_trials=2)

    def test_the_read_only_qualification_stops_before_the_first_write_pair(self):
        kinds = {task["task_id"]: task["kind"]
                 for task in self.qualification_set["tasks"]}
        prefix = self.protocol["schedule"][:READ_ONLY_PREFIX_PAIRS]
        self.assertTrue(all(kinds[pair["task_id"]] != "create" for pair in prefix))
        self.assertEqual(
            kinds[self.protocol["schedule"][READ_ONLY_PREFIX_PAIRS]["task_id"]], "create",
            "the prefix must end exactly at the first scheduled write task")


def _instantiated(task_set, protocol, fixture):
    from evals.twenty.taskset import instantiate_task

    return instantiate_task(
        task_set, protocol["schedule"][0]["task_id"], fixture.capture(role="baseline"))


if __name__ == "__main__":
    unittest.main()
