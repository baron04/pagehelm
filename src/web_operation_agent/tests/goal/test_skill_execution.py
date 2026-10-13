"""Pre-approval reference errors stop for input, without retrying a write."""
from contextlib import nullcontext
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate, BusinessToolPermissionError,
    BusinessToolReferenceError,
)
from web_operation_agent.goal.skill_execution import execute_skill_plan
from web_operation_agent.goal.ports import Candidate


class SkillReferenceRecoveryTests(unittest.TestCase):
    def execute(self, error, *, phase="prepare", prior_effect=False, grounding=False,
                scopes=("catalog",)):
        self.events = []
        self.calls = []
        definition = NS(name="revise", effect="write", reference_inputs=("target",),
                        effect_contract=None, scope_entities=scopes)
        unavailable = NS(name="restricted_revise", effect="write", scope_entities=("restricted",))
        tools = NS(definitions=(definition, unavailable), get=lambda name: definition)
        operations = NS(tools=tools)
        context = NS(loaded=(NS(write_operations=("revise",)),))
        operation_context = NS(principal=NS(scopes=("catalog.write",)),
                               audit_projection=lambda: {})
        approvals = NS(mutations_enabled=True, approval_hold_ms=1000,
                       context=lambda *a, **k: operation_context,
                       mutation_indeterminate=lambda: self.calls.append("indeterminate"))
        state = NS(approvals=approvals, step=1, write_intent=True,
            scope_parts=[], references=(), candidates=(),
            ledger=NS(append=lambda event, **data: self.events.append((event, data))),
            receipt=NS(stage=lambda name: nullcontext()), chat=None,
            message=NS(text="Revise the requested catalog record."),
            index=None, run_id="reference-test", cancelled=lambda: False,
            budget=NS(remaining=lambda: 10, check=lambda: None), today="",
            unsupported="", chinese=False, terminal_reason="", clarification=None,
            mutation_outcomes=([{"verified": True}] if prior_effect else []),
            source_requests=())
        runtime = NS(available_schemas=lambda **kwargs: (
            {"function": {"name": "revise"}},))
        executor = NS(application=NS(ports=NS(operations=operations), business_tools=runtime),
            values=False, goal_port=None, config=NS(time_zone="UTC"),
            conversation_state=NS(model_context=lambda: None))
        if grounding:
            state.scope_parts = [{"kind": "handoff", "entity": "catalog", "names": ["Sample"]}]
            executor.values = True
            executor.goal_port = NS(capabilities=lambda: frozenset({"values"}),
                locate_names=lambda *a, **k: (),
                candidates=lambda **kwargs: self.offered(kwargs))
        plan = {"intent": "execute", "steps": [{"kind": "operation", "operation": "revise",
            "skill_id": "catalog-maintenance", "inputs": {"target": "Sample"}}],
            "knowledge": []}

        class Service:
            def __init__(service, *args):
                pass
            def prepare(service, *args, **kwargs):
                self.calls.append("prepare")
                if phase == "prepare":
                    raise error
                return NS()
            def settle(service, *args, **kwargs):
                self.calls.append("settle")
                raise error

        def declare(**kwargs):
            if grounding:
                self.assertIn("Sample (East)", kwargs["current_values"])
                self.assertIn("Sample (West)", kwargs["current_values"])
                self.assertEqual(kwargs["scope"]["known_references"], [])
            return plan

        with patch("web_operation_agent.goal.skill_execution.declare_plan", side_effect=lambda chat, **k: declare(**k)), \
                patch("web_operation_agent.goal.skill_execution.BusinessWriteService", Service):
            answer = execute_skill_plan(executor, state, ("catalog-maintenance",), context=context)
        return state, answer

    def offered(self, kwargs):
        self.grounding = kwargs
        return tuple(Candidate(kind="record", entity="catalog", display=name)
                     for name in ("Sample (East)", "Sample (West)"))

    def test_unresolved_scoped_names_reach_candidate_port_without_becoming_unique_references(self):
        state, _ = self.execute(self.reference_error(), grounding=True)
        self.assertEqual(state.references, ())
        self.assertEqual(len(state.candidates), 2)
        self.assertEqual(self.calls, ["prepare"])
        self.assertEqual(self.grounding["known"], (("catalog", "Sample"),))

    def test_compound_operation_offers_linked_entities_without_claiming_unique_targets(self):
        state, _ = self.execute(self.reference_error(), grounding=True,
                                scopes=("catalog", "people", "projects"))
        self.assertEqual(self.grounding["entities"], ("catalog", "people", "projects"))
        self.assertEqual(self.grounding["known"], (
            ("catalog", "Sample"), ("people", "Sample"), ("projects", "Sample")))
        self.assertEqual(state.references, ())
        self.assertEqual(self.calls, ["prepare"])

    def reference_error(self):
        return BusinessToolReferenceError("Target is ambiguous", entity="catalog",
            choices=("Sample (East)", "Sample (West)"))

    def test_prepare_ambiguity_returns_complete_choices_without_approval_or_retry(self):
        state, answer = self.execute(self.reference_error())
        self.assertEqual(state.terminal_reason, "needs_clarification")
        self.assertEqual(self.calls, ["prepare"])
        self.assertEqual(state.clarification["response_kind"], "single_choice")
        self.assertEqual([c["record_ref"] for c in state.clarification["choices"]],
                         ["Sample (East)", "Sample (West)"])
        self.assertIn(state.clarification["question"], answer)

    def test_prior_verified_write_prevents_whole_request_clarification_replay(self):
        state, _ = self.execute(self.reference_error(), prior_effect=True)
        self.assertEqual(state.terminal_reason, "agent_refused")
        self.assertIsNone(state.clarification)
        self.assertEqual(self.calls, ["prepare"])
        self.assertEqual(len(state.mutation_outcomes), 1)

    def test_reference_error_after_preparation_does_not_offer_a_write_retry(self):
        state, _ = self.execute(self.reference_error(), phase="settle")
        self.assertEqual(state.terminal_reason, "agent_refused")
        self.assertIsNone(state.clarification)
        self.assertEqual(self.calls, ["prepare", "settle"])

    def test_permission_and_uncertain_outcomes_keep_their_terminal_states(self):
        for error, reason in ((BusinessToolPermissionError("refused"), "permission_denied"),
                (BusinessMutationIndeterminate("unknown", phase="dispatch"), "indeterminate_write")):
            with self.subTest(reason=reason):
                state, _ = self.execute(error, phase="settle")
                self.assertEqual(state.terminal_reason, reason)
                self.assertIsNone(state.clarification)
                self.assertEqual(self.calls.count("prepare"), 1)
                self.assertEqual(self.calls.count("settle"), 1)


if __name__ == "__main__":
    unittest.main()
