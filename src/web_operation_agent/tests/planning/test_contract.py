"""Retrieval and contract validation, with a stubbed model.

Retrieval must be deterministic and must follow only chains the framework declares.
Validation must reject a contract that cites something it was never shown -- the whole
value of the layer is that a claim can be checked. A move the deployment does not have is
handled differently: it is pruned into `unknowns` so it never reaches the agent, while the
checkable part of the contract survives.
"""
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.planning.contract import ContractPlanner, PlanningFailed, validate_contract
from web_operation_agent.planning.retrieval import EvidenceBundle, SourceRetriever
from web_operation_agent.schemas import OperationContract
from web_operation_agent.tests.support.planning import SEGMENT, TASK, _map, _StubModel


def _key_supporting(source_map, bundle, identity: str) -> str:
    """One bundle citation the support index attributes to `identity`."""
    from web_operation_agent.planning.contract import SupportIndex
    index = SupportIndex(source_map, segment=SEGMENT)
    return next(key for key in sorted(bundle.citation_keys)
                if identity in index.about(key))


class Retrieval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.r = SourceRetriever(self.map)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_follows_the_declared_chain_from_the_task_words(self):
        bundle = self.r.retrieve("T", TASK)
        text = bundle.render()
        self.assertIn("sproutSeedling", text, "the request whose name matches the task is missing")
        self.assertIn("SproutSeedlingForm", text, "the form that submits it is missing")
        self.assertIn("trayId", text, "the fields that form posts are missing")
        self.assertIn("Seed Trays", text, "the visible navigation label is missing")

    def test_a_matched_menu_target_handler_precedes_lexical_request_candidates(self):
        bundle = self.r.retrieve("T", "seed trays sprout cultivar", max_requests=1)
        text = bundle.render()
        self.assertIn("navigation: the menu item shown as \"Seed Trays\"", text)
        self.assertIn("request `glasshouse/findTrays` is handled by view-only", text)

    def test_menu_parameter_sources_are_rendered_without_resolving_them(self):
        text = self.r.retrieve("T", "seed trays", max_requests=1).render()
        self.assertIn("`trayId` from field `selectedTrayId`", text)
        self.assertIn("`viewMode` from literal `summary`", text)
        self.assertIn("`gardenId` from the same-named field (implicit)", text)

    def test_a_view_chain_reaches_the_included_forms_conditional_create_target(self):
        bundle = self.r.retrieve("T", "edit lamp schedule", webapps={"beacon"},
                                 max_requests=1, max_forms=0)
        text = bundle.render()
        self.assertIn("request `beacon/LampSchedule/Edit` is handled by view-only", text)
        self.assertIn("view `LampSchedule/Edit` renders screen", text)
        self.assertIn("includes form", text)
        self.assertIn("included form `LampEditor` uses alternate request "
                      "`LampSchedule/create` when `schedule==null`", text)

    def test_every_line_carries_a_citation_that_resolves(self):
        bundle = self.r.retrieve("T", TASK)
        self.assertTrue(bundle.lines)
        index = self.map.evidence_index()
        for key in bundle.citation_keys:
            self.assertIn(key, index, f"bundle cites {key}, which is not in the map")
        for line in bundle.lines:
            self.assertTrue(line.startswith("["), f"uncited line: {line}")

    def test_it_is_deterministic(self):
        a = self.r.retrieve("T", TASK).render()
        b = SourceRetriever(self.map).retrieve("T", TASK).render()
        self.assertEqual(a, b)

    def test_identical_chain_facts_are_not_repeated(self):
        bundle = self.r.retrieve("T", "edit lamp schedule", webapps={"beacon"})
        self.assertEqual(len(bundle.lines), len(set(bundle.lines)))

    def test_an_unrelated_task_gets_a_small_or_empty_bundle(self):
        bundle = self.r.retrieve("T", "Reconcile the quarterly amortisation schedule.")
        self.assertNotIn("sproutSeedling", bundle.render())

    def test_the_bundle_is_bounded(self):
        bundle = self.r.retrieve("T", TASK, max_lines=3)
        self.assertLessEqual(len(bundle.lines), 3)


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.good_key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def _contract(self, **over) -> OperationContract:
        base = {"schema_version": 1, "task_ref": "T", "summary": "s",
                "citations": [self.good_key],
                "mutation": {"webapp": "glasshouse", "request": "sproutSeedling",
                             "required_params": ["trayId"], "citations": [self.good_key]}}
        base.update(over)
        return OperationContract.from_json(base)

    def test_a_valid_contract_passes(self):
        self.assertEqual(validate_contract(self._contract(), self.bundle, self.map), [])

    def test_a_fabricated_citation_is_rejected(self):
        c = self._contract(citations=["applications/invented/never/seen.xml:1-9"])
        problems = validate_contract(c, self.bundle, self.map)
        self.assertTrue(any("was not among the facts" in p for p in problems), problems)

    def test_a_request_that_does_not_exist_is_not_fatal_but_is_pruned(self):
        """It cannot be checked, so it must not reach the agent -- but the rest of the
        contract still can. See `Pruning`."""
        from web_operation_agent.planning.contract import prune_contract
        c = self._contract(mutation={"webapp": "glasshouse", "request": "harvestEverything",
                                     "citations": [self.good_key]})
        self.assertEqual(validate_contract(c, self.bundle, self.map), [])
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertIsNone(pruned.mutation)
        self.assertTrue(dropped)

    def test_a_contract_with_no_citations_at_all_is_rejected(self):
        c = OperationContract.from_json({"schema_version": 1, "task_ref": "T", "summary": "s"})
        problems = validate_contract(c, self.bundle, self.map)
        self.assertTrue(any("cites nothing" in p for p in problems), problems)


class Planning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def _json(self, citation):
        return ('{"task_ref":"T","summary":"sprout it","entry_candidates":[],'
                '"preconditions":[],"steps":[],'
                '"mutation":{"webapp":"glasshouse","request":"sproutSeedling",'
                f'"required_params":["trayId"],"citations":["{citation}"]}},'
                f'"unknowns":["which bay maps to which tray"],"citations":["{citation}"]}}')

    def test_a_good_answer_is_accepted_in_one_call(self):
        model = _StubModel([self._json(self.key)])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(contract.mutation.request, "sproutSeedling")
        self.assertEqual(contract.unknowns, ["which bay maps to which tray"])

    def test_a_bad_answer_is_returned_to_the_model_with_the_reason(self):
        model = _StubModel([self._json("made/up.xml:1-2"), self._json(self.key)])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(attempts[0]["problems"])
        repair = model.calls[1][-1]["content"]
        self.assertIn("was not among the facts", repair)
        self.assertTrue(contract.cited_keys() <= self.bundle.citation_keys)

    def test_two_bad_answers_are_a_product_failure(self):
        model = _StubModel([self._json("made/up.xml:1-2"), self._json("also/made/up.xml:3-4")])
        with self.assertRaises(PlanningFailed):
            ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")

    def test_prose_around_the_json_is_tolerated_but_nonsense_is_not(self):
        model = _StubModel(["```json\n" + self._json(self.key) + "\n```"])
        contract, _ = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(contract.task_ref, "T")
        model = _StubModel(["I would rather not.", "Still no."])
        with self.assertRaises(PlanningFailed):
            ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")

    def test_the_planner_is_shown_the_task_and_the_bundle_and_nothing_else(self):
        model = _StubModel([self._json(self.key)])
        ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        blob = "\n".join(m["content"] for m in model.calls[0])
        self.assertIn(TASK, blob)
        for forbidden in ("PASS", "postcondition", "evaluator", "ControlPlan", "permit"):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} reached the planner's input")


if __name__ == "__main__":
    unittest.main()


class Normalisation(unittest.TestCase):
    def test_the_servlet_prefix_is_stripped(self):
        from web_operation_agent.planning.contract import normalise_request
        for spelling in ("/accounting/do/findPayments",
                         "accounting/do/findPayments",
                         "http://localhost:18080/accounting/do/findPayments",
                         "'/accounting/do/findPayments'",
                         "findPayments?paymentId=8003",
                         "findPayments/"):
            self.assertEqual(normalise_request("accounting", spelling, segment=SEGMENT), "findPayments",
                             f"{spelling!r} should normalise to the bare request name")

    def test_a_multi_segment_request_name_survives(self):
        from web_operation_agent.planning.contract import normalise_request
        self.assertEqual(
            normalise_request("workeffort", "/workeffort/do/WorkEffortPartyAssigns/create", segment=SEGMENT),
            "WorkEffortPartyAssigns/create")

    def test_a_bare_name_is_left_alone(self):
        from web_operation_agent.planning.contract import normalise_request
        self.assertEqual(normalise_request("accounting", "createPaymentApplication", segment=SEGMENT),
                         "createPaymentApplication")


class Pruning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def _contract(self, steps, mutation=None):
        return OperationContract.from_json(
            {"schema_version": 1, "task_ref": "T", "summary": "s", "steps": steps,
             "mutation": mutation, "citations": [self.key]})

    def test_a_move_the_deployment_does_not_have_becomes_an_unknown(self):
        from web_operation_agent.planning.contract import prune_contract
        c = self._contract([
            {"intent": "real", "webapp": "glasshouse", "request": "sproutSeedling",
             "citations": [self.key]},
            {"intent": "invented", "webapp": "glasshouse", "request": "harvestEverything",
             "citations": [self.key]}])
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertEqual([s.intent for s in pruned.steps], ["real"])
        self.assertEqual(len(dropped), 1)
        self.assertIn("harvestEverything", pruned.unknowns[0])

    def test_an_invented_write_endpoint_is_removed_not_offered(self):
        from web_operation_agent.planning.contract import prune_contract
        c = self._contract([], {"webapp": "glasshouse", "request": "nope",
                                "citations": [self.key]})
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertIsNone(pruned.mutation)
        self.assertTrue(dropped)

    def test_a_form_target_with_no_controller_entry_is_still_a_real_move(self):
        """OFBiz builds some request names at runtime; a declared form target proves it exists.

        The citation must still be about it -- existing and being cited for are two questions.
        """
        from web_operation_agent.planning.contract import SupportIndex, prune_contract
        index = SupportIndex(self.map, segment=SEGMENT)
        key = next(k for k in self.map.evidence_index()
                   if "beacon/lampschedule/create" in index.about(k))
        c = self._contract([{"intent": "post the form", "webapp": "beacon",
                             "request": "LampSchedule/create", "citations": [key]}])
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertEqual(dropped, [])
        self.assertEqual(len(pruned.steps), 1)

    def test_a_fabricated_citation_is_still_fatal(self):
        c = OperationContract.from_json({"schema_version": 1, "task_ref": "T", "summary": "s",
                                         "citations": ["invented/file.xml:1-2"]})
        self.assertTrue(validate_contract(c, self.bundle, self.map))

    def test_a_contract_whose_every_move_is_invented_is_a_planning_failure(self):
        model = _StubModel([
            '{"task_ref":"T","summary":"s","steps":[{"intent":"x","webapp":"glasshouse",'
            f'"request":"nope","citations":["{self.key}"]}}],"citations":["{self.key}"]}}',
            '{"task_ref":"T","summary":"s","steps":[{"intent":"x","webapp":"glasshouse",'
            f'"request":"alsonope","citations":["{self.key}"]}}],"citations":["{self.key}"]}}'])
        with self.assertRaises(PlanningFailed):
            ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")

    def test_a_contract_written_with_servlet_paths_is_accepted(self):
        model = _StubModel([
            '{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
            f'"request":"/glasshouse/do/sproutSeedling","citations":["{self.key}"]}}],'
            f'"citations":["{self.key}"]}}'])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(contract.steps[0].request, "sproutSeedling")


class PlannerBudget(unittest.TestCase):
    """A truncated contract measures the ceiling, not the source."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_planner_asks_for_more_room_than_one_tool_call(self):
        from web_operation_agent.planning.contract import PLANNER_START_TOKENS
        model = _StubModel([
            '{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
            f'"request":"sproutSeedling","citations":["{self.key}"]}}],"citations":["{self.key}"]}}'])
        ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(model.budgets, [PLANNER_START_TOKENS])
        self.assertGreater(PLANNER_START_TOKENS, 2048)

    def test_the_webapp_named_twice_is_still_one_request(self):
        from web_operation_agent.planning.contract import normalise_request
        self.assertEqual(normalise_request("accounting", "accounting/createPaymentApplication", segment=SEGMENT),
                         "createPaymentApplication")
        self.assertEqual(normalise_request("workeffort", "workeffort/WorkEffortPartyAssigns/create", segment=SEGMENT),
                         "WorkEffortPartyAssigns/create")


class CitationNormalisation(unittest.TestCase):
    """A key copied out of our own rendering must not cost a run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_brackets_the_facts_are_printed_in_are_stripped(self):
        from web_operation_agent.planning.contract import normalise_citation
        for spelling in (f"[{self.key}]", f" [{self.key}] ", f"`{self.key}`", self.key):
            self.assertEqual(normalise_citation(spelling), self.key)

    def test_a_contract_citing_bracketed_keys_is_accepted(self):
        model = _StubModel([
            '{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
            f'"request":"sproutSeedling","citations":["[{self.key}]"]}}],'
            f'"citations":["[{self.key}]"]}}'])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(contract.cited_keys(), {self.key})

    def test_a_genuinely_invented_key_is_still_rejected(self):
        model = _StubModel(['{"task_ref":"T","summary":"s","citations":["[made/up.xml:1-2]"]}',
                            '{"task_ref":"T","summary":"s","citations":["[also/made/up.xml:1-2]"]}'])
        with self.assertRaises(PlanningFailed):
            ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")


class ReasoningLeakedIntoTheAnswer(unittest.TestCase):
    """The provider sometimes puts a <think> block in `content`. A brace inside it is found
    before the answer's own unless reasoning is removed before JSON parsing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def _json(self):
        return ('{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
                f'"request":"sproutSeedling","citations":["{self.key}"]}}],"citations":["{self.key}"]}}')

    def test_a_closed_think_block_is_ignored(self):
        model = _StubModel(["<think>\nWe need {careful} JSON.\n</think>\n" + self._json()])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(contract.steps[0].request, "sproutSeedling")

    def test_a_think_block_after_the_answer_is_ignored_too(self):
        model = _StubModel([self._json() + "\n<think>done {ok}</think>"])
        contract, _ = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(contract.task_ref, "T")

    def test_thinking_with_no_answer_eventually_fails_after_growing(self):
        """It is given more room first -- that is the fix -- but the room is bounded."""
        model = _StubModel(["<think>\nI am not sure {what} to do.\n</think>"] * 8)
        with self.assertRaises(PlanningFailed):
            ContractPlanner(model, self.map, max_growths=2, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(model.budgets, [4096, 8192, 16384, 16384],
                         "grow twice, then one repair at the last budget, then give up")


class ReasoningEatsTheBudget(unittest.TestCase):
    """A reasoning model that thinks for its whole allowance and says nothing looks, from the
    outside, like a malformed answer. It is not, and it must not be told it was."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        # Pick a citation that actually supports the move these tests assert about,
        # rather than whichever key happens to sort first: "cited" and "supported by
        # that citation" are two different questions, and only the second is tested here.
        self.key = _key_supporting(self.map, self.bundle, "glasshouse/sproutseedling")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reasoning_and_answer_are_told_apart(self):
        from web_operation_agent.planning.contract import split_reasoning
        t, a = split_reasoning("<think>hmm {x}</think>\n{\"a\": 1}")
        self.assertEqual(t, "<think>hmm {x}</think>")
        self.assertEqual(a, '{"a": 1}')
        t, a = split_reasoning("<think>never closed, and it goes on")
        self.assertTrue(t.startswith("never closed"))
        self.assertEqual(a, "")

    def test_all_reasoning_buys_more_room_not_a_scolding(self):
        good = ('{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
                f'"request":"sproutSeedling","citations":["{self.key}"]}}],"citations":["{self.key}"]}}')
        model = _StubModel(["<think>\n" + "deliberating {a lot} " * 200 + "\n</think>\n", good])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(len(attempts), 2)
        self.assertIn("reasoning", attempts[0]["problems"][0])
        self.assertGreater(attempts[0]["reasoning_chars"], 0)
        self.assertEqual(attempts[0]["answer_chars"], 0)
        self.assertEqual(model.budgets, [4096, 8192], "starvation buys room, not a repair")
        asked = model.calls[1][-1]["content"]
        self.assertIn("no answer", asked)
        self.assertNotIn("rejected", asked, "there was no contract to reject")
        self.assertNotIn("deliberating", "".join(m["content"] for m in model.calls[1]),
                         "its own reasoning must not be fed back to it")
        self.assertEqual(contract.steps[0].request, "sproutSeedling")

    def test_a_truncated_answer_buys_more_room_too(self):
        """The provider saying it stopped on length is a budget fact, not an answer fact."""
        good = ('{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
                f'"request":"sproutSeedling","citations":["{self.key}"]}}],"citations":["{self.key}"]}}')
        model = _StubModel([{"content": '{"task_ref":"T","summ',
                             "finish_reason": "length"}, good])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertIn("cut off", attempts[0]["problems"][0])
        self.assertEqual(attempts[0]["finish_reason"], "length")
        self.assertEqual(model.budgets, [4096, 8192])
        self.assertEqual(contract.steps[0].request, "sproutSeedling")

    def test_growth_is_bounded_and_the_repair_allowance_is_separate(self):
        """Budget growth must not silently consume the one chance to fix a wrong contract."""
        wrong = ('{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
                 '"request":"harvestEverything","citations":["made/up.xml:1-2"]}],'
                 '"citations":["made/up.xml:1-2"]}')
        good = ('{"task_ref":"T","summary":"s","steps":[{"intent":"go","webapp":"glasshouse",'
                f'"request":"sproutSeedling","citations":["{self.key}"]}}],"citations":["{self.key}"]}}')
        model = _StubModel([{"content": "", "finish_reason": "length"}, wrong, good])
        contract, attempts = ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual([a["problems"] != [] for a in attempts], [True, True, False])
        self.assertEqual(model.budgets, [4096, 8192, 8192],
                         "one growth, then the repair reuses the grown budget")
        self.assertEqual(contract.steps[0].request, "sproutSeedling")


class ReasoningIsRecognisedWhoeverProducedIt(unittest.TestCase):
    """Vendors disagree about how to return chain-of-thought. None of them should need a
    code path of their own here."""

    def test_every_inline_tag_convention_is_stripped(self):
        from web_operation_agent.planning.contract import REASONING_TAGS, split_reasoning
        for tag in REASONING_TAGS:
            t, a = split_reasoning(f"<{tag}>weighing {{options}}</{tag}>\n{{\"a\": 1}}")
            self.assertIn("weighing", t, tag)
            self.assertEqual(a, '{"a": 1}', tag)

    def test_tag_attributes_and_casing_do_not_defeat_it(self):
        from web_operation_agent.planning.contract import split_reasoning
        t, a = split_reasoning('<Think depth="3">hmm {x}</Think>\n{"a": 1}')
        self.assertIn("hmm", t)
        self.assertEqual(a, '{"a": 1}')

    def test_reasoning_returned_out_of_band_is_counted_as_reasoning(self):
        """Some endpoints put it in its own field and leave content clean."""
        from web_operation_agent.planning.contract import split_reasoning
        t, a = split_reasoning('{"a": 1}', out_of_band="deliberating at length")
        self.assertIn("deliberating", t)
        self.assertEqual(a, '{"a": 1}')

    def test_an_out_of_band_thought_with_no_content_is_starvation(self):
        from web_operation_agent.planning.contract import split_reasoning
        t, a = split_reasoning("", out_of_band="thought hard, said nothing")
        self.assertTrue(t)
        self.assertEqual(a, "")

    def test_an_answer_with_no_reasoning_is_left_alone(self):
        from web_operation_agent.planning.contract import split_reasoning
        t, a = split_reasoning('{"a": 1}')
        self.assertEqual(t, "")
        self.assertEqual(a, '{"a": 1}')


class CitationsMustBeAboutWhatTheyBack(unittest.TestCase):
    """A citation key that exists does not necessarily support the attached claim.

    The evidence can say Red while the contract says Blue, and every earlier check passed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        from web_operation_agent.planning.contract import SupportIndex
        self.index = SupportIndex(self.map, segment=SEGMENT)
        # The index is keyed by the full `webapp/request`: two applications can both declare
        # `edit`, and a bare name cannot tell their evidence apart.
        sprout, trays = "glasshouse/sproutseedling", "glasshouse/findtrays"
        self.about_sprout = next(k for k in self.map.evidence_index()
                                 if sprout in self.index.about(k))
        self.about_other = next(k for k in self.map.evidence_index()
                                if trays in self.index.about(k)
                                and sprout not in self.index.about(k))

    def tearDown(self):
        self.tmp.cleanup()

    def _contract(self, steps, mutation=None):
        return OperationContract.from_json(
            {"schema_version": 1, "task_ref": "T", "summary": "s", "steps": steps,
             "mutation": mutation, "citations": [self.about_sprout]})

    def test_a_step_cited_with_evidence_about_another_request_is_dropped(self):
        from web_operation_agent.planning.contract import prune_contract
        c = self._contract([{"intent": "sprout it", "webapp": "glasshouse",
                             "request": "sproutSeedling", "citations": [self.about_other]}])
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertEqual(pruned.steps, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("is evidence about", dropped[0])
        self.assertIn("that move is unsupported", dropped[0])
        self.assertIn(dropped[0], pruned.unknowns[0])

    def test_the_same_step_cited_properly_is_kept(self):
        from web_operation_agent.planning.contract import prune_contract
        c = self._contract([{"intent": "sprout it", "webapp": "glasshouse",
                             "request": "sproutSeedling", "citations": [self.about_sprout]}])
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertEqual(dropped, [])
        self.assertEqual(len(pruned.steps), 1)

    def test_an_unsupported_write_endpoint_is_removed(self):
        from web_operation_agent.planning.contract import prune_contract
        c = self._contract([], {"webapp": "glasshouse", "request": "sproutSeedling",
                                "citations": [self.about_other]})
        pruned, dropped = prune_contract(c, self.map, segment=SEGMENT)
        self.assertIsNone(pruned.mutation)
        self.assertIn("the write endpoint is unsupported", dropped[0])

    def test_evidence_about_a_service_supports_the_request_that_runs_it(self):
        """A service parameter is a fact about every request invoking that service."""
        param_key = next(k for k in self.map.evidence_index()
                         if any(r.kind == "service_parameter" and r.subject == "sproutSeedling"
                                for r in self.map.relations if any(e.key == k for e in r.evidence)))
        self.assertIn("glasshouse/sproutseedling", self.index.about(param_key))

    def test_servlet_paths_and_bare_names_compare_equal(self):
        """Inter-app menu links carry /webapp/do/name; ordinary ones carry the name."""
        from web_operation_agent.planning.contract import bare_request
        for spelling in ("/accounting/do/findPayments", "accounting/do/findPayments",
                         "http://host:8080/ap/do/findPayments", "findPayments?x=1"):
            self.assertEqual(bare_request(spelling, segment=SEGMENT), "findPayments", spelling)
        self.assertEqual(bare_request("WorkEffortPartyAssigns/create", segment=SEGMENT),
                         "WorkEffortPartyAssigns/create")


class TheTwoArmsShareEverythingButTheEvidence(unittest.TestCase):
    """The comparison is only worth running if a difference in results cannot come from a
    difference in machinery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.map = _map(Path(self.tmp.name))
        self.bundle = SourceRetriever(self.map).retrieve("T", TASK)
        from web_operation_agent.planning.contract import SupportIndex
        self.empty = EvidenceBundle(task_ref="T")
        index = SupportIndex(self.map, segment=SEGMENT)
        # A citation the bundle really contains and that is really about the step it backs;
        # a source-arm contract with no surviving move is rejected, as it should be.
        self.key = next(k for k in sorted(self.bundle.citation_keys)
                        if "glasshouse/sproutseedling" in index.about(k))
        self.sourced = ('{"task_ref":"T","summary":"s","steps":[{"intent":"go",'
                        '"webapp":"glasshouse","request":"sproutSeedling",'
                        f'"citations":["{self.key}"]}}],"citations":["{self.key}"]}}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_instructions_differ_only_where_the_manipulation_is(self):
        from web_operation_agent.planning.contract import PLANNER_SYSTEM, PLANNER_SYSTEM_PAGE_ONLY
        a, b = PLANNER_SYSTEM.splitlines(), PLANNER_SYSTEM_PAGE_ONLY.splitlines()
        self.assertEqual(len(a), len(b))
        differing = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        self.assertLessEqual(len(differing), 4)
        for i in differing:
            self.assertTrue(any(w in (a[i] + b[i]).lower()
                                for w in ("evidence", "cite", "citation", "source", "see")),
                            f"line {i} differs for a reason unrelated to the manipulation: "
                            f"{a[i]!r} vs {b[i]!r}")

    def test_the_page_only_arm_is_not_asked_for_facts_it_does_not_have(self):
        model = _StubModel(['{"task_ref":"T","summary":"from the page","steps":[],'
                            '"citations":[]}'])
        contract, attempts = ContractPlanner(model, None, segment=SEGMENT).plan("T", TASK, self.empty, "r1",
                                                               page=None)
        self.assertEqual(attempts[0]["problems"], [])
        self.assertEqual(contract.summary, "from the page")
        sent = "\n".join(m["content"] for m in model.calls[0])
        self.assertNotIn("FACTS FROM THE DEPLOYED SOURCE", sent)
        self.assertNotIn("sproutSeedling", sent, "no source may reach the control arm")

    def test_the_source_arm_is_still_asked_for_facts(self):
        model = _StubModel([self.sourced])
        ContractPlanner(model, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        sent = "\n".join(m["content"] for m in model.calls[0])
        self.assertIn("FACTS FROM THE DEPLOYED SOURCE", sent)

    def test_the_page_only_arm_may_not_invent_citations(self):
        """It has nothing to cite; a key it produces anyway is fabricated by definition."""
        model = _StubModel(['{"task_ref":"T","summary":"s","citations":["made/up.xml:1-2"]}',
                            '{"task_ref":"T","summary":"s","citations":["still/made/up.xml:1-2"]}'])
        with self.assertRaises(PlanningFailed):
            ContractPlanner(model, None, segment=SEGMENT).plan("T", TASK, self.empty, "r1")

    def test_the_page_only_arm_is_not_filtered_by_the_source_map(self):
        """Pruning against the descriptors is a use of source evidence and belongs to the arm
        that has it; applying it here would hand the control arm the manipulated variable."""
        from web_operation_agent.planning.contract import prune_contract
        invented = ('{"task_ref":"T","summary":"s","steps":[{"intent":"go",'
                    '"webapp":"glasshouse","request":"harvestEverything","citations":[]}],'
                    '"citations":[]}')
        contract, attempts = ContractPlanner(model := _StubModel([invented]), None) \
            .plan("T", TASK, self.empty, "r1")
        self.assertEqual(attempts[0]["dropped"], [])
        self.assertEqual(contract.steps[0].request, "harvestEverything")
        # the same contract, in the source arm, would lose that step
        pruned, dropped = prune_contract(contract, self.map, segment=SEGMENT)
        self.assertEqual(pruned.steps, [])
        self.assertTrue(dropped)
        self.assertIs(model.map if hasattr(model, "map") else None, None)

    def test_both_arms_use_the_same_budget_growth(self):
        good = '{"task_ref":"T","summary":"s","steps":[],"citations":[]}'
        page = _StubModel([{"content": "", "finish_reason": "length"}, good])
        src = _StubModel([{"content": "", "finish_reason": "length"}, self.sourced])
        ContractPlanner(page, None, segment=SEGMENT).plan("T", TASK, self.empty, "r1")
        ContractPlanner(src, self.map, segment=SEGMENT).plan("T", TASK, self.bundle, "r1")
        self.assertEqual(page.budgets, src.budgets)
