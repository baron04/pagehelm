"""The paired synthetic pilot drives both real engines through the common schedule driver.

`PairedPilotProtocolTests` needs no browser. `PairedPilotChromiumTests` launches the built
PageAgent bundle in headless Chromium for one scheduled pair and is skipped when the bundle
or Chromium is absent, unless `WOA_REQUIRE_PAGEAGENT_BROWSER=1` makes that absence a failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from evals.pageagent.test_twenty_runner_browser import BUNDLE, _chromium_launches
from evals.twenty.protocol import freeze_protocol
from evals.twenty.report import verify_report
from evals.twenty.taskset import seal_task_set, verify_split_separation
from evals.woa.pilot_paired_synthetic import (
    APPARATUS, PILOT_SEED, PairedPilot, PilotConfigurationError, opaque_task_set,
    pilot_protocol, scripted_pageagent_actions, scripted_woa_replies,
)
from evals.woa.qualify_schedule import DEVELOPMENT_TASKSET


def _artifacts():
    development = json.loads(DEVELOPMENT_TASKSET.read_text(encoding="utf-8"))
    development_seal = seal_task_set(development)
    task_set = opaque_task_set(development, set_id="twenty-companies-paired-pilot-v1")
    test_seal = seal_task_set(task_set)
    split = verify_split_separation(development_seal, test_seal)
    return development_seal, task_set, test_seal, split


class PairedPilotProtocolTests(unittest.TestCase):
    def test_the_pilot_protocol_is_a_development_pilot_bound_to_the_real_model(self):
        development_seal, _task_set, test_seal, split = _artifacts()
        frozen = freeze_protocol(
            pilot_protocol(sorted(test_seal["task_digests"]), seal=test_seal,
                           development_seal=development_seal, split=split,
                           requested_model="qwen3.8-max"),
            development_seal=development_seal, test_seal=test_seal, split_verification=split)

        self.assertEqual(frozen["purpose"], "development_pilot")
        self.assertEqual(frozen["provider"]["requested_model"], "qwen3.8-max")
        self.assertEqual(frozen["randomization_seed"], PILOT_SEED)
        self.assertEqual(len(frozen["schedule"]), 21)
        boundaries = {c["comparison_type"]: c["approval_boundary"]
                      for c in frozen["comparisons"]}
        self.assertEqual(boundaries["skill_api"], "capability_only")

    def test_scripted_replies_follow_each_arm_capability(self):
        companies = [{"id": "11111111-1111-4111-8111-111111111111", "name": "Acme",
                      "domain": "acme.example.com"}]
        open_case = {"evaluator_task": {"kind": "open", "company_id": companies[0]["id"]}}
        create_case = {"evaluator_task": {"kind": "create", "name": "New Co",
                                          "domain": "new.example.com", "require_open": True}}
        browser = {"capability_profile": "browser_only"}
        skill = {"capability_profile": "skill_api"}

        self.assertEqual(scripted_woa_replies(open_case, browser, companies)[0][0], "goto")
        self.assertEqual(scripted_woa_replies(open_case, skill, companies)[0][0], "load_skill")
        self.assertEqual(
            [next(iter(action)) for action in scripted_pageagent_actions(open_case, browser, companies)],
            ["click_element_by_index", "done"])
        self.assertEqual(
            [next(iter(action)) for action in scripted_pageagent_actions(open_case, skill, companies)],
            ["query_companies", "open_allowed_route", "done"])
        self.assertEqual(
            [next(iter(action)) for action in scripted_pageagent_actions(create_case, skill, companies)],
            ["create_company", "open_allowed_route", "done"])

    def test_configuration_errors_are_refused_before_any_browser_or_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.js"
            bundle.write_bytes(b"// placeholder bundle\n")
            with self.assertRaisesRegex(PilotConfigurationError, "provider"):
                PairedPilot(output_dir=Path(directory) / "out", bundle_path=bundle,
                            provider="mystery", model_env=None, max_cost_cny=1.0)
            with self.assertRaisesRegex(PilotConfigurationError, "max-cost-cny"):
                PairedPilot(output_dir=Path(directory) / "out", bundle_path=bundle,
                            provider="scripted", model_env=None, max_cost_cny=0)
            with self.assertRaisesRegex(PilotConfigurationError, "bundle"):
                PairedPilot(output_dir=Path(directory) / "out",
                            bundle_path=Path(directory) / "missing.js",
                            provider="scripted", model_env=None, max_cost_cny=1.0)


class PairedPilotChromiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_PAGEAGENT_BROWSER") == "1"
        if not BUNDLE.is_file():
            if required:
                raise RuntimeError("PageAgent baseline bundle is not built")
            raise unittest.SkipTest("PageAgent baseline bundle is not built")
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is unavailable")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_one_scheduled_pair_runs_both_real_engines_through_the_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot"
            with PairedPilot(output_dir=output, bundle_path=BUNDLE, provider="scripted",
                             model_env=None, max_cost_cny=1.0, wall_time_s=60) as pilot:
                summary = pilot.run(batch_id="paired-pilot-test", max_trials=2)

            self.assertEqual(summary["apparatus"], APPARATUS)
            self.assertEqual(summary["completed_trials"], 2)
            self.assertEqual(summary["stopped_reason"], "trial_limit")
            self.assertEqual(summary["batch_status"], "OPEN")
            self.assertEqual(summary["outcomes"]["PASS"], 2)
            self.assertEqual(summary["known_cost_cny"], 0.0)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(verify_report(report)["status"], "READY")
            completed = [row for row in report["comparisons"] if row["pairs_completed"]]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["pairs_both_valid"], 1)
            # The one finished pair used exactly its two preregistered arms, each once.
            used = {arm["arm_id"]: arm["trials"] for arm in report["arms"] if arm["trials"]}
            self.assertEqual(used, {completed[0]["left_arm"]: 1, completed[0]["right_arm"]: 1})
            self.assertFalse((output / "spend.jsonl").exists())
            self.assertEqual(
                "sha256:" + hashlib.sha256(BUNDLE.read_bytes()).hexdigest(),
                summary["bundle_digest"])


if __name__ == "__main__":
    unittest.main()
