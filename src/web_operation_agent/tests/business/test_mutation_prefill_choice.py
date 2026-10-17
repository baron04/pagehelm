"""A form choice consumes a write approval without authorising a mutation."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.prefill_conversion import MutationPrefillOffer
from web_operation_agent.business.tools.contracts import BusinessMutationProposal
from web_operation_agent.business.write_service import BusinessWriteService
from web_operation_agent.schemas import PageObservation
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.business import _operation_context


class _Runtime:
    def __init__(self, proposal):
        self.proposal = proposal
        self.dispatched = 0

    def prepare_mutation(self, *args, **kwargs):
        return self.proposal

    def execute_mutation(self, *args, **kwargs):
        self.dispatched += 1
        raise AssertionError("the form choice must not dispatch a write")


class _PrefillPort:
    required_scope = "test.read"

    def __init__(self, *, offered=True, wrong_digest=False):
        self.offered, self.wrong_digest = offered, wrong_digest

    def offer(self, proposal, *, context, observation):
        if not self.offered:
            return None
        return MutationPrefillOffer(
            "0" * 64 if self.wrong_digest else proposal.content_digest,
            context.binding_digest, "company:new", (("name", proposal.args["name"]),))

    def handoff(self, offer, proposal, *, context):
        raise AssertionError("approval settlement does not enter a form")


class PrefillChoiceTests(unittest.TestCase):
    def _run(self, *, offered=True, digest=True, binding=True, wrong_offer=False):
        context = _operation_context()
        proposal = BusinessMutationProposal(
            "create_company", {"name": "Synthetic Acme"}, "Create company",
            ("Synthetic Acme",), "1" * 32,
            operation_context_digest=context.binding_digest)
        seen = []

        def decide(current, *, context, offer_prefill=False):
            seen.append(offer_prefill)
            return SimpleNamespace(
                decision="prefill", approval_id="choice-1",
                plan_digest=current.content_digest if digest else "0" * 64,
                operation_context_digest=context.binding_digest if binding else "0" * 64)

        with tempfile.TemporaryDirectory() as directory:
            ledger = SessionLedger(Path(directory) / "run.jsonl", "prefill-choice")
            approvals = ApprovalWatch(
                ledger=ledger, run_id="prefill-choice", principal=context.principal,
                session_id=context.session_id, turn_id=context.turn_id,
                business_approval=decide, cancelled=lambda: False)
            approvals.start_turn(context.expires_at - 1)
            runtime = _Runtime(proposal)
            outcome = BusinessWriteService(runtime, approvals, set()).run(
                proposal.tool, {"name": "Synthetic Acme"}, step=1,
                context=context, prefill_port=_PrefillPort(
                    offered=offered, wrong_digest=wrong_offer),
                prefill_observation=PageObservation(1, 1, "https://synthetic.test/companies",
                                                   "Companies", [], [], "Companies"))
            return outcome, runtime.dispatched, seen, ledger.events, approvals.business_counts

    def test_exact_offered_choice_is_distinct_and_never_dispatches(self):
        outcome, dispatched, seen, events, counts = self._run()
        self.assertEqual((outcome.state, outcome.reason, outcome.asked),
                         ("prefill_selected", "prefill_selected", True))
        self.assertEqual(outcome.prefill_offer.fields, (("name", "Synthetic Acme"),))
        self.assertEqual((dispatched, seen), (0, [True]))
        self.assertEqual(counts["mutation_execution_started"], 0)
        self.assertEqual(counts["approval_denied"], 0)
        self.assertEqual(len([e for e in events if e["event"] == "business_mutation_prefill_selected"]), 1)
        self.assertFalse(any(e["event"] == "business_mutation_result" for e in events))

    def test_unoffered_or_mismatched_choice_fails_closed(self):
        for options in ({"offered": False}, {"digest": False}, {"binding": False}):
            with self.subTest(options):
                outcome, dispatched, _, events, _ = self._run(**options)
                self.assertEqual((outcome.state, outcome.reason, dispatched),
                                 ("not_approved", "approval_failed", 0))
                self.assertFalse(any(e["event"] == "business_mutation_prefill_selected"
                                     for e in events))

    def test_wrong_offer_fails_before_approval(self):
        outcome, dispatched, seen, _, _ = self._run(wrong_offer=True)
        self.assertEqual((outcome.state, outcome.asked, dispatched, seen),
                         ("refused", False, 0, []))


if __name__ == "__main__":
    unittest.main()
