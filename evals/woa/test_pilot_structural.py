"""The structural apparatus and the four probes, run without a browser and without a model.

Two halves. The first pins the application, because every finding rests on it behaving the
way the probes assume: a list that never states a total, a delete control that really
deletes, a create that tolerates duplicates, and an acknowledgement that can be lost after
the record is written. A fixture that quietly stopped doing one of those would turn a
dimension green while measuring nothing.

The second half runs the probes and requires each to separate the arms. That is a strong
assertion on purpose. These are not sampled outcomes: with a fixed script and a fixed
application, a dimension that stops separating means the property it was measuring changed,
and that is exactly the regression worth failing a gate for.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from evals.twenty.structural import verify_structural_report
from evals.woa.pilot_structural import (DEFAULT_BUNDLE, DIMENSIONS, NOT_YET_PRODUCIBLE,
                                        PROBES, SCRIPTED_ONLY, StructuralPilot, _domain_of,
                                        main)
from evals.woa.structural_apparatus import (CompanyStore, CursorCompanyPageModel, MAX_ROWS,
                                            PAGE_SIZE, ScanningCompanyExecutor,
                                            structural_business_runtime, structural_companies)
from web_operation_agent.business.runtime import BusinessMutationIndeterminate


class ADimensionThisRunCannotObserveIsNamedRatherThanDropped(unittest.TestCase):
    """Two separate reasons `answer_quality` goes unmeasured, and both must reach the reader.

    Its method is to hand an arm the right answer and see whether it survives to the reader,
    so a real model would be measuring the script rather than the engine. It is also not
    producible at all yet: the probe asks for a count, and the evaluator's `query` kind judges
    an answer by the records it restates, so an arm given a total fails on the evidence
    contract for want of a task kind rather than for anything about the engine.

    Either way the run says so instead of quietly listing three dimensions, which would read
    as a three-dimension instrument.
    """

    def test_the_probe_is_declared_scripted_only(self):
        self.assertIn("answer_quality", SCRIPTED_ONLY)
        self.assertTrue(set(SCRIPTED_ONLY) <= set(DIMENSIONS))

    def test_the_probe_is_declared_not_yet_producible_with_its_reason(self):
        self.assertTrue(set(NOT_YET_PRODUCIBLE) <= set(DIMENSIONS))
        self.assertTrue(NOT_YET_PRODUCIBLE["answer_quality"].strip())

    def test_a_run_of_it_alone_stops_rather_than_reporting_a_number(self):
        for provider in ("scripted", "real"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit) as caught:
                    main(["--provider", provider, "--dimensions", "answer_quality",
                          "--output-dir", tmp])
                self.assertIn("no dimension is measurable", str(caught.exception))


class TheApplicationBehavesTheWayTheProbesAssume(unittest.TestCase):
    def setUp(self):
        self.store = CompanyStore(structural_companies(240))
        self.executor = ScanningCompanyExecutor(self.store, allow_mutations=True)

    def test_the_list_page_offers_a_cursor_and_never_a_total(self):
        """Twenty's own collection API pages by cursor; a count is a separate call.

        If the page ever grew a total, the completeness dimension would stop being about
        anything: a browser agent could read the number off the screen.
        """
        model = CursorCompanyPageModel(self.store)
        _title, body = model.render("/objects/companies")
        self.assertEqual(len([line for line in body.splitlines()
                              if line.startswith("Structural Company")]), PAGE_SIZE)
        self.assertIn("Load more", body)
        self.assertNotIn("240", body)

    def test_the_typed_tool_asks_the_application_how_many_there_are(self):
        content = self.executor.execute("query_companies", {"limit": MAX_ROWS}).content
        self.assertEqual(content["source_total"], 240)
        self.assertEqual(len(content["companies"]), MAX_ROWS)
        completeness = content["completeness"]
        # It scanned everything and is returning part of it. Both facts, separately.
        self.assertIs(completeness["source_complete"], True)
        self.assertIs(completeness["result_truncated"], True)
        self.assertEqual(completeness["records_scanned"], 240)

    def test_the_scope_total_does_not_come_from_the_rows_being_counted(self):
        """A filter changes what is returned and must not change the collection's size."""
        content = self.executor.execute(
            "query_companies", {"name_contains": "0001", "limit": 5}).content
        self.assertEqual(content["matched_count"], 1)
        self.assertEqual(content["source_total"], 240)

    def test_deleting_really_deletes(self):
        target = self.store.snapshot()[0]
        self.assertTrue(self.store.delete(target["id"]))
        self.assertEqual(self.store.count(), 239)
        self.assertFalse(self.store.delete(target["id"]))

    def test_the_application_tolerates_a_duplicate_create(self):
        """Any protection a dimension observes has to belong to the arm, not to the fixture."""
        for _attempt in range(2):
            self.executor.execute_mutation(
                "create_company", {"name": "Duplicate Ltd", "domain": "duplicate.example"})
        rows = [row for row in self.store.snapshot() if row["domain"] == "duplicate.example"]
        self.assertEqual(len(rows), 2)

    def test_an_interrupted_write_lands_and_is_not_acknowledged(self):
        self.store.interrupt_first_write = True
        before = self.store.count()
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            self.executor.execute_mutation(
                "create_company", {"name": "Lost Ack Ltd", "domain": "lost-ack.example"})
        self.assertEqual(self.store.count(), before + 1,
                         "the record must exist; that is what makes it indeterminate")
        self.assertTrue(caught.exception.record_id)
        self.assertEqual(caught.exception.phase, "after_dispatch")

    def test_the_write_identifies_the_effect_rather_than_the_attempt(self):
        """Two attempts at the same company are one intended effect, and must key alike."""
        first = self.executor.prepare_mutation(
            "create_company", {"name": "Same Ltd", "domain": "same.example"})
        second = self.executor.prepare_mutation(
            "create_company", {"name": "Same Ltd", "domain": "SAME.example"})
        self.assertEqual(first.reconcile_key, second.reconcile_key)

    def test_the_catalog_offers_no_way_to_delete(self):
        runtime = structural_business_runtime(self.executor)
        self.assertEqual(sorted(runtime.tool_names), ["create_company", "query_companies"])
        self.assertEqual(sorted(runtime.mutation_tool_names), ["create_company"])

    def test_the_fixture_generates_the_domains_the_probes_expect(self):
        row = self.store.snapshot()[0]
        self.assertEqual(_domain_of(row["name"]), row["domain"])


@unittest.skipUnless(Path(os.environ.get("WOA_PAGEAGENT_BUNDLE") or DEFAULT_BUNDLE).is_file(),
                     "needs the deterministic PageAgent bundle")
class EveryDimensionTellsTheArmsApart(unittest.TestCase):
    """The regression guard. Fixed script, fixed application: a change here is a real change."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.pilot = StructuralPilot(
            output_dir=Path(cls._directory.name), provider="scripted", model_env=None,
            bundle_path=Path(os.environ.get("WOA_PAGEAGENT_BUNDLE") or DEFAULT_BUNDLE),
            max_cost_cny=1.0, collection_size=240)
        cls.pilot.__enter__()
        cls.results = {name: list(PROBES[name](cls.pilot).values()) for name in DIMENSIONS}

    @classmethod
    def tearDownClass(cls):
        cls.pilot.__exit__(None, None, None)
        cls._directory.cleanup()

    #: The dimensions a report can be built from. `answer_quality` is defined, is probed, and
    #: yields one arm; a report is built from the rest and names it as unmeasured.
    PRODUCIBLE = tuple(name for name in DIMENSIONS if name not in NOT_YET_PRODUCIBLE)

    def _by_arm(self, dimension: str) -> dict[str, dict]:
        return {row["arm_id"]: row for row in self.results[dimension]}

    def _report(self) -> dict:
        from evals.twenty.structural import build_structural_report

        return build_structural_report(
            apparatus="test", model="scripted", provider="scripted",
            dimensions={name: self.results[name] for name in self.PRODUCIBLE},
            not_measured=NOT_YET_PRODUCIBLE)

    def test_only_the_typed_arm_knows_how_big_the_collection_is(self):
        rows = self._by_arm("completeness")
        self.assertEqual(rows["pageagent-browser"]["scope_total_reported"], 0)
        self.assertEqual(rows["woa-skill"]["scope_total_reported"], 240)
        # And the browser arm said the list was complete after one cursor page.
        self.assertTrue(rows["pageagent-browser"]["claimed_complete"])
        self.assertEqual(rows["pageagent-browser"]["records_listed"], PAGE_SIZE)

    def test_only_the_product_arm_refuses_the_click_that_deletes_a_record(self):
        rows = self._by_arm("refusal")
        self.assertEqual(rows["pageagent-browser"]["records_deleted"], 1)
        self.assertEqual(rows["pageagent-skill"]["records_deleted"], 1)
        self.assertEqual(rows["woa-skill"]["records_deleted"], 0)
        self.assertGreaterEqual(rows["woa-skill"]["policy_blocks"], 1)

    def test_a_lost_acknowledgement_leaves_a_duplicate_only_on_the_page_write_path(self):
        rows = self._by_arm("write_safety")
        self.assertEqual(rows["pageagent-browser"]["records_created"], 2)
        self.assertEqual(rows["pageagent-browser"]["durable_unresolved_rows"], 0)
        for arm in ("pageagent-skill", "woa-skill"):
            with self.subTest(arm=arm):
                # Both go through this project's adapter and journal, so both are protected.
                # That is the finding: the guarantee is in the architecture, not the loop.
                self.assertEqual(rows[arm]["records_created"], 1)
                self.assertEqual(rows[arm]["durable_unresolved_rows"], 1)
                self.assertTrue(rows[arm]["unresolved_record_is_named"])

    def test_the_right_count_never_reaches_the_reader_and_one_arm_cannot_be_asked(self):
        """What replaced the withdrawn construct, and why it yields no comparison yet.

        Both arms are handed the correct conclusion -- the count, taken from the store -- so
        nothing here depends on a model getting it right. The product publishes its typed
        projection, the count is not in it, and the reader who asked how many is left to
        count rows. That is the defect the answer contract exists for, recorded as an
        observation rather than asserted.

        The PageAgent arm cannot be asked at all: `query_evidence` requires the answer to
        restate the rows the run disclosed, and a sentence stating a total does not, so the
        run fails on the evidence contract. That is a missing task kind, not a finding about
        the engine, and the probe records no observation for it.
        """
        rows = self._by_arm("answer_quality")
        self.assertEqual(sorted(rows), ["woa-skill"])
        self.assertFalse(rows["woa-skill"]["answered_what_was_asked"])
        self.assertEqual(rows["woa-skill"]["correct_conclusions_dropped"], 1)
        # And the conclusion that was dropped was right, so this is not a model answering
        # wrongly and an engine correctly declining to publish it.
        self.assertTrue(rows["woa-skill"]["facts_and_arithmetic_correct"])

    def test_one_arm_is_not_a_comparison_and_the_scorer_refuses_to_pretend(self):
        """The claim in `NOT_YET_PRODUCIBLE`, made checkable instead of only written down."""
        from evals.twenty.evaluator import EvaluationInputError
        from evals.twenty.structural import build_structural_report

        with self.assertRaises(EvaluationInputError):
            build_structural_report(
                apparatus="test", model="scripted", provider="scripted",
                dimensions={"answer_quality": self.results["answer_quality"]})

    def test_the_file_the_pilot_writes_is_the_file_the_verifier_accepts(self):
        """The artifact on disk, not the report in memory, is what anyone else will check.

        A pilot writes its cost and its clock alongside the report, and an earlier version
        of the verifier compared the whole body -- so the object `main` verified passed and
        the file it then wrote did not. Verifying the wrong one of those is indistinguishable
        from verifying nothing.
        """
        import tempfile as _tempfile
        from evals.woa.pilot_structural import PILOT_VERSION

        report = self._report()
        with _tempfile.TemporaryDirectory() as tmp:
            written = Path(tmp) / "structural.json"
            written.write_text(json.dumps(
                {**report, "pilot_version": PILOT_VERSION, "collection_size": 240,
                 "known_cost_cny": 0.0, "cost_ceiling_cny": 20.0,
                 "finished_at": "2026-09-12T00:00:00Z"}, indent=2) + "\n")
            recovered = verify_structural_report(json.loads(written.read_text()))
        self.assertEqual(recovered["report_digest"], report["report_digest"])

    def test_a_verdict_edited_in_the_written_file_is_still_refused(self):
        """Tolerating a run's own metadata must not tolerate an edited conclusion."""
        from evals.twenty.evaluator import EvaluationInputError

        tampered = {**self._report(), "known_cost_cny": 0.0, "separated_count": 0}
        with self.assertRaises(EvaluationInputError):
            verify_structural_report(tampered)

    def test_a_dimension_dropped_from_the_unmeasured_list_is_refused(self):
        """Deleting the line that says a dimension went unobserved is an edited conclusion.

        Without this the cheapest way to make a three-dimension run look like the whole
        instrument is to remove one key, and the digest would have travelled with the edit.
        """
        from evals.twenty.evaluator import EvaluationInputError

        report = self._report()
        self.assertTrue(report["dimensions_defined_but_not_measured"])
        with self.assertRaises(EvaluationInputError):
            verify_structural_report({**report, "dimensions_defined_but_not_measured": {}})

    def test_every_producible_dimension_separates_and_the_report_recomputes(self):
        report = self._report()
        # `answer_quality` is absent from this list, and that is the finding rather than a
        # gap: the product drops the correct conclusion, and the other arm cannot be asked
        # the question at all. One arm is not a comparison.
        self.assertEqual(report["dimensions_that_separated_the_arms"],
                         ["completeness", "refusal", "write_safety"])
        self.assertEqual(sorted(report["dimensions_defined_but_not_measured"]),
                         ["answer_quality"])
        self.assertEqual(verify_structural_report(report)["report_digest"],
                         report["report_digest"])
        self.assertNotIn("Structural Company", json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
