"""Qualify Twenty breadth cases through the real WOA product turn path without a model."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evals.pageagent.scripted import (SYNTHETIC_FIXTURE, SyntheticCompanyExecutor,
                                      SyntheticTwentyAdapter, synthetic_business_runtime,
                                      write_storage_state)
from evals.twenty.breadth import evaluate_breadth, make_breadth_state
from evals.twenty.breadth_taskset import instantiate_breadth_task
from evals.woa.breadth_scripted import (SyntheticBreadthExecutor, SyntheticBreadthPageModel,
                                       breadth_projection, synthetic_breadth_records,
                                       synthetic_breadth_runtime)
from evals.woa.scripted import ScriptedWoaChat, SimulatedRuntime
from evals.woa.twenty_runner import WoaTwentyConfig, run_breadth_case


ORIGIN = "http://127.0.0.1:3020"
TASKSET = Path(__file__).resolve().parents[1] / "twenty" / "tasksets" / \
    "twenty-breadth-development-v2.json"


class WoaBreadthRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state_path = write_storage_state(self.root / "state.json", origin=ORIGIN)
        self.storage_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.records = synthetic_breadth_records()
        self.executor = SyntheticBreadthExecutor(self.records)
        runtime = synthetic_breadth_runtime(self.executor)
        self.adapter = SyntheticTwentyAdapter(origin=ORIGIN, runtime=runtime)
        self.page_model = SyntheticBreadthPageModel(self.records)
        self.runtimes: list[SimulatedRuntime] = []
        self.task_set = json.loads(TASKSET.read_text(encoding="utf-8"))
        projection = breadth_projection(self.records)
        self.before = make_breadth_state(
            fixture=SYNTHETIC_FIXTURE, projection=projection,
            captured_at="2026-09-10T00:00:00Z", role="baseline")
        self.after = make_breadth_state(
            fixture=SYNTHETIC_FIXTURE, projection=projection,
            captured_at="2026-09-10T00:01:00Z", role="observation")

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.stop()
        self.directory.cleanup()

    def _factory(self, **kwargs):
        runtime = SimulatedRuntime(
            kwargs["channel"], kwargs["grant"], origin=kwargs["origin"],
            entry_path=kwargs["entry_path"], page_model=self.page_model,
            storage_state=self.storage_state)
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def _sampler(runtime):
        return runtime.page_model.render(runtime.current_path)[1]

    def _case(self, task_id):
        return instantiate_breadth_task(self.task_set, task_id, self.before)

    def _run(self, task_id, replies):
        case = self._case(task_id)
        result = run_breadth_case(
            case=case, adapter=self.adapter, client=ScriptedWoaChat(replies),
            config=WoaTwentyConfig(storage_state_path=self.state_path, wall_time_s=60),
            ledger_path=self.root / "audit.jsonl", runtime_factory=self._factory,
            page_sampler=self._sampler, run_id="breadth-" + task_id)
        return case, result

    def _evaluate(self, case, result):
        return evaluate_breadth(
            task=case["evaluator_task"], before=self.before, after=self.after,
            evidence=result["evidence"])

    def test_enumeration_and_filter_use_matching_typed_query_projections(self):
        cases = (
            ("dev-enumerate-people-en", "synthetic.people-query", "query_people", {"limit": 50}),
            ("dev-filter-opportunities-en", "synthetic.opportunities-query",
             "query_opportunities", None),
        )
        for task_id, skill, tool, arguments in cases:
            with self.subTest(task_id=task_id):
                case = self._case(task_id)
                if arguments is None:
                    arguments = {"name_contains": case["evaluator_task"]["name_contains"],
                                 "limit": 20}
                case, result = self._run(task_id, [
                    ("load_skill", {"skill_id": skill}), (tool, arguments),
                    (None, "Here are the current records."),
                ])
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["kind"], "woa_twenty_breadth_run")
                self.assertEqual(self._evaluate(case, result)["status"], "PASS")
                self.assertTrue(result["identity"]["session_binding_unchanged"])

    def test_record_open_requires_typed_query_route_and_visible_destination(self):
        case = self._case("dev-open-people-en")
        target_id = case["evaluator_task"]["record_id"]
        target = next(row for row in self.records["people"] if row["id"] == target_id)
        case, result = self._run("dev-open-people-en", [
            ("load_skill", {"skill_id": "synthetic.people-query"}),
            ("query_people", {"name_contains": target["name"], "limit": 20}),
            ("goto", {"url": f"/object/person/{target_id}"}),
            ("finish", {"done": True, "why": "The contact is visible."}),
        ])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self._evaluate(case, result)["status"], "PASS")

    def test_collection_navigation_is_observed_without_a_business_write(self):
        case, result = self._run("dev-module-opportunities-zh", [
            ("load_skill", {"skill_id": "synthetic.opportunities-query"}),
            ("query_opportunities", {"limit": 50}),
            ("goto", {"url": "/objects/opportunities"}),
            ("finish", {"done": True, "why": "商机列表已打开。"}),
        ])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["trial"]["business_calls"], 1)
        self.assertEqual(self._evaluate(case, result)["status"], "PASS")

    def test_unsupported_mutation_is_a_scored_refusal_with_zero_dispatch(self):
        case, result = self._run("dev-refuse-person-delete-en", [
            ("finish", {"done": False,
                        "why": "Deleting contacts is not a supported operation."}),
        ])
        self.assertEqual((result["status"], result["error_code"]),
                         ("failed", "unsupported_operation"))
        self.assertEqual(result["evidence"]["dispatch_count"], 0)
        self.assertEqual(self._evaluate(case, result)["status"], "PASS")

    def test_create_case_reuses_the_existing_confirmed_write_spine(self):
        case = self._case("dev-create-company-en-b")
        executor = SyntheticCompanyExecutor(self.records["companies"], allow_mutations=True)
        adapter = SyntheticTwentyAdapter(
            origin=ORIGIN, runtime=synthetic_business_runtime(executor))

        def factory(**kwargs):
            runtime = SimulatedRuntime(
                kwargs["channel"], kwargs["grant"], origin=kwargs["origin"],
                entry_path=kwargs["entry_path"], page_model=self.page_model,
                storage_state=self.storage_state, approval_decision="approved")
            self.runtimes.append(runtime)
            return runtime

        task = case["evaluator_task"]
        result = run_breadth_case(
            case=case, adapter=adapter, client=ScriptedWoaChat([
                ("load_skill", {"skill_id": "synthetic.companies-create"}),
                ("create_company", {"name": task["name"], "domain": task["domain"]}),
                ("finish", {"done": True, "why": "Created after approval."}),
            ]),
            config=WoaTwentyConfig(
                storage_state_path=self.state_path, allow_create=True, wall_time_s=60),
            ledger_path=self.root / "audit.jsonl", runtime_factory=factory,
            page_sampler=self._sampler, run_id="breadth-create-company")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["evidence"]["write_receipt"]["dispatch_count"], 1)
        self.assertEqual(result["trial"]["approvals_approved"], 1)
        self.assertEqual(executor.mutation_dispatches, 1)

    def test_wrong_collection_projection_fails_the_runner_evidence_contract(self):
        _case, result = self._run("dev-enumerate-people-en", [
            ("load_skill", {"skill_id": "synthetic.companies-query"}),
            ("query_companies", {"limit": 50}),
            (None, "These are the records."),
        ])
        self.assertEqual(result["status"], "failed")
        self.assertIn(result["error_code"], {"agent_refused", "evidence_contract"})


if __name__ == "__main__":
    unittest.main()
