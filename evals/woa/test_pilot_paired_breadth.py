"""The paired breadth pilot: scripted replies, concordance arithmetic and one real pair.

`PairedBreadthPilotTests` needs no browser. `PairedBreadthPilotChromiumTests` drives the built
PageAgent bundle in headless Chromium and the Web Operation Agent simulated runtime for one
pair of each paired task kind, and is skipped when the bundle or Chromium is absent unless
`WOA_REQUIRE_PAGEAGENT_BROWSER=1` makes that absence a failure.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from evals.pageagent.test_twenty_runner_browser import BUNDLE, _chromium_launches
from evals.woa.pilot_paired_breadth_synthetic import (
    ARMS, PAIRED_KINDS, PairedBreadthPilot, PilotConfigurationError, scripted_pageagent_actions,
    scripted_woa_replies, summarize_pairs,
)


def _pair(kind: str, left: str, right: str) -> dict:
    return {"task_kind": kind, "arms": {ARMS[0]: {"evaluation_status": left},
                                        ARMS[1]: {"evaluation_status": right}}}


class PairedBreadthPilotTests(unittest.TestCase):
    def test_concordance_counts_disagreement_without_direction_and_skips_invalid_pairs(self):
        summary = summarize_pairs([
            _pair("enumerate", "PASS", "PASS"),
            _pair("enumerate", "PASS", "FAIL"),
            _pair("filter", "FAIL", "PASS"),
            _pair("refuse", "PASS", "INVALID"),
        ])

        self.assertEqual(summary["overall"], {
            "pairs_completed": 4, "pairs_both_valid": 3, "concordant_pairs": 1,
            "discordant_pairs": 2, "discordance_rate": round(2 / 3, 6)})
        self.assertEqual(summary["by_kind"]["enumerate"]["discordant_pairs"], 1)
        self.assertEqual(summary["by_kind"]["filter"]["discordance_rate"], 1.0)
        self.assertIsNone(summary["by_kind"]["refuse"]["discordance_rate"])
        rendered = str(summary)
        for word in ("left", "right", "winner", "pageagent", "woa"):
            self.assertNotIn(word, rendered)

    def test_scripted_replies_cover_every_paired_kind_on_both_engines(self):
        records = {"people": [{"id": "11111111-1111-4111-8111-111111111111",
                               "name": "Synthetic Person 01", "email": "p@example"}]}
        cases = {
            "enumerate": {"kind": "enumerate", "entity": "people"},
            "filter": {"kind": "filter", "entity": "people", "name_contains": "Person 01"},
            "open_record": {"kind": "open_record", "entity": "people",
                            "record_id": records["people"][0]["id"]},
            "open_collection": {"kind": "open_collection", "entity": "people"},
            "refuse": {"kind": "refuse", "entity": "people", "operation": "delete"},
        }
        for kind in PAIRED_KINDS:
            case = {"evaluator_task": cases[kind]}
            woa = scripted_woa_replies(case, records)
            pageagent = scripted_pageagent_actions(case, records)
            self.assertTrue(woa and pageagent, kind)
            if kind == "refuse":
                self.assertEqual(woa, [("finish", {"done": False, "why": woa[0][1]["why"]})])
                self.assertFalse(pageagent[-1]["done"]["success"])
            else:
                self.assertEqual(woa[0][0], "load_skill")
                self.assertIn("query_people", {name for name, _ in woa} | {next(iter(a)) for a in pageagent})
                self.assertTrue(pageagent[-1]["done"]["success"])

    def test_configuration_is_checked_before_any_browser_or_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.js"
            bundle.write_bytes(b"// placeholder\n")
            with self.assertRaisesRegex(PilotConfigurationError, "kinds"):
                PairedBreadthPilot(output_dir=Path(directory) / "out", bundle_path=bundle,
                                   provider="scripted", model_env=None, max_cost_cny=1.0,
                                   kinds=("create_company",))
            with self.assertRaisesRegex(PilotConfigurationError, "bundle"):
                PairedBreadthPilot(output_dir=Path(directory) / "out",
                                   bundle_path=Path(directory) / "missing.js",
                                   provider="scripted", model_env=None, max_cost_cny=1.0)


class PairedBreadthPilotChromiumTests(unittest.TestCase):
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

    def test_one_pair_per_kind_runs_both_engines_and_agrees_with_scripted_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot"
            seen: dict[str, dict] = {}
            for kind in PAIRED_KINDS:
                with PairedBreadthPilot(
                        output_dir=output / kind, bundle_path=BUNDLE, provider="scripted",
                        model_env=None, max_cost_cny=1.0, wall_time_s=60,
                        kinds=(kind,)) as pilot:
                    seen[kind] = pilot.run(max_pairs=1)

            for kind, summary in seen.items():
                self.assertEqual(summary["completed_pairs"], 1, kind)
                self.assertEqual(summary["stopped_reason"], "pair_limit", kind)
                pair = summary["pairs"][0]
                self.assertEqual(set(pair["arms"]), set(ARMS), kind)
                for arm_id, row in pair["arms"].items():
                    self.assertEqual(row["evaluation_status"], "PASS", (kind, arm_id, row))
                    self.assertTrue(row["state_unchanged"], (kind, arm_id))
                    self.assertEqual(row["known_cost_cny"], 0.0, (kind, arm_id))
                self.assertEqual(summary["concordance"]["overall"]["discordant_pairs"], 0, kind)
                self.assertEqual(summary["approval_boundary"], "capability_only")
                self.assertEqual(summary["purpose"], "development_diagnostic_not_benchmark")

    def test_a_collection_that_is_already_open_needs_no_typed_call_on_either_engine(self):
        """The paid pilot's only discordance was this apparatus asymmetry, not a product one."""
        def finish_only_pageagent(case, records):
            if case["evaluator_task"]["kind"] == "open_collection" and \
                    case["evaluator_task"]["entity"] == "companies":
                return [{"done": {"text": "The companies module is already open.",
                                  "success": True}}]
            return scripted_pageagent_actions(case, records)

        def finish_only_woa(case, records):
            if case["evaluator_task"]["kind"] == "open_collection" and \
                    case["evaluator_task"]["entity"] == "companies":
                return [("finish", {"done": True, "why": "The companies module is already open."})]
            return scripted_woa_replies(case, records)

        with tempfile.TemporaryDirectory() as directory:
            with PairedBreadthPilot(
                    output_dir=Path(directory) / "pilot", bundle_path=BUNDLE,
                    provider="scripted", model_env=None, max_cost_cny=1.0, wall_time_s=60,
                    kinds=("open_collection",)) as pilot:
                pilot.pageagent_actions = finish_only_pageagent
                pilot.woa_replies = finish_only_woa
                summary = pilot.run()

        companies = [pair for pair in summary["pairs"] if pair["entity"] == "companies"]
        self.assertTrue(companies)
        for pair in companies:
            for arm_id, row in pair["arms"].items():
                self.assertEqual(row["evaluation_status"], "PASS", (arm_id, row))
                self.assertEqual(row["business_calls"], 0, (arm_id, row))
        self.assertEqual(summary["concordance"]["overall"]["discordant_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
