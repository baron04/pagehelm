import unittest

from evals.odoo import outcomes
from evals.odoo.driver import (_cost, _trace_counts, _with_residue, agent_assertions,
                               final_turn_requests, scored_terminal, turn_bounds,
                               turns_before_fault)


def request(seq):
    return {"attempt_id": "a1", "request_seq": seq, "body_digest": "sha256:" + "0" * 64,
            "requested_model": "qwen3.8-flash", "max_output_tokens": 2048}


def response(seq, *, usage=True, cost=0.0012):
    return {"attempt_id": "a1", "request_seq": seq, "status": "ok", "http_status": 200,
            "provider_request_id": f"r{seq}", "response_model": "m",
            "usage": ({"input_tokens": 100, "output_tokens": 10, "cached_tokens": 40,
                       "reasoning_tokens": 3} if usage else None),
            "cost_cny": cost if usage else None, "error_code": None}


class CostTests(unittest.TestCase):
    def test_fully_metered_attempt_reports_its_total(self):
        cost = _cost("a1", [request(1), request(2)], [response(1), response(2)])
        self.assertEqual((cost["model_calls"], cost["usage_unavailable_calls"]), (2, 0))
        self.assertEqual((cost["input_tokens"], cost["cached_tokens"]), (200, 80))
        self.assertAlmostEqual(cost["total_cost_cny"], 0.0024)

    def test_unknown_usage_and_missing_responses_make_the_total_unknown(self):
        cost = _cost("a1", [request(1), request(2), request(3)],
                     [response(1), response(2, usage=False)])
        self.assertEqual(cost["usage_unavailable_calls"], 2)
        self.assertIsNone(cost["total_cost_cny"])
        self.assertAlmostEqual(cost["known_cost_cny"], 0.0012)

    def test_no_calls_costs_exactly_zero(self):
        cost = _cost("a1", [], [])
        self.assertEqual((cost["model_calls"], cost["total_cost_cny"]), (0, 0.0))


EMPTY = {"active_sessions": 0, "unresolved_mutations": 0, "prepared_proposals": [],
         "conversation_rows": 0}
DENIED = {"event": "business_mutation_denied", "proposal_digest": "p1", "dispatched": False,
          "side_effect_possible": False}


class AgentAssertionTests(unittest.TestCase):
    def test_a_declined_proposal_is_closed_but_an_undecided_one_is_residue(self):
        end = {**EMPTY, "prepared_proposals": ["p1"], "conversation_rows": 1}
        record, residue = agent_assertions(EMPTY, end, [DENIED], product_started=True)
        self.assertEqual((record["open_proposals"], record["closed_proposals"], residue),
                         (0, 1, []))
        self.assertEqual(record["end"], {"active_sessions": 0, "unresolved_mutations": 0,
                                         "prepared_proposals": 1, "conversation_rows": 1})
        self.assertEqual(_with_residue("PASS", residue), "PASS")
        record, residue = agent_assertions(EMPTY, end, [], product_started=True)
        self.assertEqual((record["open_proposals"], residue),
                         (1, ["agent_residue:open_proposals"]))
        self.assertEqual(_with_residue("PASS", residue), "FAIL")

    def test_an_audit_nobody_read_leaves_a_proposal_unasserted_not_open(self):
        """The review of 753817e: an audit that could not be exported was passed here as an
        empty one, so every proposal the product had prepared counted as still open and the
        reset -- which could only be undecided -- was published as a failure. The published
        reset axis counts INDETERMINATE and FAIL separately."""
        end = {**EMPTY, "prepared_proposals": ["p1"], "conversation_rows": 1}
        record, residue = agent_assertions(EMPTY, end, [], product_started=True,
                                           audit_observed=False)
        self.assertEqual((record["open_proposals"], residue), (1, ["agent_audit_unreadable"]))
        self.assertEqual(_with_residue("PASS", residue), "INDETERMINATE")
        # Residue the apparatus did observe is still a failure, unread audit or not.
        dirty = {**end, "active_sessions": 1}
        _, mixed = agent_assertions(EMPTY, dirty, [], product_started=True,
                                    audit_observed=False)
        self.assertEqual(_with_residue("PASS", mixed), "FAIL")

    def test_sessions_and_unresolved_mutations_are_residue(self):
        end = {**EMPTY, "active_sessions": 1, "unresolved_mutations": 1}
        _, residue = agent_assertions(EMPTY, end, [DENIED], product_started=True)
        self.assertEqual(residue, ["agent_residue:active_sessions",
                                   "agent_residue:unresolved_mutations"])
        self.assertEqual(_with_residue("NOT_REQUIRED", residue), "FAIL")
        self.assertEqual(_with_residue("INDETERMINATE", residue), "INDETERMINATE")

    def test_an_unread_end_state_is_never_clean(self):
        record, residue = agent_assertions(EMPTY, None, [], product_started=True)
        self.assertEqual((record["end"], residue), (None, ["agent_state_unreadable"]))
        self.assertEqual(_with_residue("PASS", residue), "INDETERMINATE")
        _, before_start = agent_assertions(None, None, [], product_started=False)
        self.assertEqual(_with_residue("NOT_REQUIRED", before_start), "NOT_REQUIRED")


class TurnsBeforeFaultTests(unittest.TestCase):
    def test_only_turns_that_ended_before_the_first_faulty_request_count(self):
        # Turn 0 ended after request 1, turn 1 after request 3.
        settled = {0: 1, 1: 3}
        self.assertEqual(turns_before_fault(settled, 2), 1)
        self.assertEqual(turns_before_fault(settled, 1), 0)
        self.assertEqual(turns_before_fault(settled, 4), 2)
        self.assertEqual(turns_before_fault(settled, None), 2)
        # A turn that never ended stops the count, whatever ended after it.
        self.assertEqual(turns_before_fault({1: 1}, None), 0)


class FinalTurnRequestTests(unittest.TestCase):
    def test_a_turn_made_the_requests_after_the_turn_before_it_ended(self):
        settled = {0: 1, 1: 3}
        self.assertEqual(turn_bounds(settled, 0), (0, 1))
        self.assertEqual(turn_bounds(settled, 1), (1, 3))
        # A turn still running, or one whose predecessor never reported ending, has an open edge.
        self.assertEqual(turn_bounds(settled, 2), (3, None))
        self.assertEqual(turn_bounds({1: 3}, 1), (None, 3))

    def test_the_last_turn_made_the_requests_after_the_turn_before_it(self):
        self.assertEqual(final_turn_requests({0: 1, 1: 3}, 2), (1, 3))
        self.assertEqual(final_turn_requests({0: 2}, 1), (0, 2))
        # The review of 8574cb0: a last turn that never reported ending has no provable span,
        # whatever the attempt requested in all.
        self.assertIsNone(final_turn_requests({0: 1}, 2))
        self.assertIsNone(final_turn_requests({}, 1))
        # Nor has one whose preceding turn never reported ending.
        self.assertIsNone(final_turn_requests({1: 2}, 2))
        self.assertIsNone(final_turn_requests({}, 0))


def ending(terminal, seq):
    return outcomes.GatewayFacts(terminal=terminal, terminal_seq=seq,
                                 terminals=((seq, terminal),) if seq is not None else ())


class ScoredTerminalTests(unittest.TestCase):
    def test_a_gateway_ending_replaces_the_generic_failure_only_from_its_ending_request(self):
        # The last turn made requests 2 and 3; request 3 ended it.
        final = (1, 3)
        for terminal in ("context_length_exceeded", "provider_deadline", "input_size_budget",
                         "model_call_budget"):
            with self.subTest(terminal=terminal):
                self.assertEqual(scored_terminal("provider_error", ending(terminal, 3), None,
                                                 final), terminal)
                # An earlier turn's record, or one the turn went on past, explains nothing.
                self.assertEqual(scored_terminal("provider_error", ending(terminal, 1), None,
                                                 final), "provider_error")
                self.assertEqual(scored_terminal("provider_error", ending(terminal, 2), None,
                                                 final), "provider_error")
                # Without a provable span, or without the record's number, nothing is replaced.
                self.assertEqual(scored_terminal("provider_error", ending(terminal, 3)),
                                 "provider_error")
                self.assertEqual(scored_terminal("provider_error", ending(terminal, None), None,
                                                 final), "provider_error")
        # An unmapped, bare or missing ending is never replaced, so the classification sees it.
        for runner in ("terminal_nobody_mapped", "failed", "control_plane_error", None):
            with self.subTest(runner=runner):
                self.assertEqual(scored_terminal(runner, ending("context_length_exceeded", 3),
                                                 None, final), runner)
                self.assertEqual(scored_terminal(runner, ending("model_call_budget", 3), None,
                                                 final), runner)
        self.assertEqual(scored_terminal("completed", ending("context_length_exceeded", 3),
                                         None, final), "completed")
        self.assertEqual(scored_terminal("completed", ending("model_call_budget", 1)),
                         "model_call_budget")
        self.assertEqual(scored_terminal("provider_error", outcomes.GatewayFacts()),
                         "provider_error")

    def test_a_provider_deadline_scores_alike_whether_it_timed_out_or_answered_408(self):
        for status in ("transport_error", "http_error"):
            with self.subTest(status=status):
                facts = outcomes.gateway_facts(
                    [{"request_seq": 1, "status": status, "error_code": "provider_deadline"}], [])
                self.assertEqual((facts.events, facts.terminal, facts.terminal_seq),
                                 ((), "provider_deadline", 1))
                self.assertEqual(scored_terminal("provider_error", facts, None, (0, 1)),
                                 "provider_deadline")
                self.assertEqual(scored_terminal("wall_clock_budget", facts, None, (0, 1)),
                                 "wall_clock_budget")

    def test_a_wall_clock_the_runner_enforced_ends_the_attempt_whatever_the_product_said(self):
        nothing = outcomes.GatewayFacts()
        for limit in ("turn_wall", "attempt_automatic_wall"):
            with self.subTest(limit=limit):
                self.assertEqual(scored_terminal("user_cancelled", nothing, limit),
                                 "wall_clock_budget")
        self.assertEqual(scored_terminal("user_cancelled", ending("model_call_budget", 1),
                                         "turn_wall"), "model_call_budget")
        self.assertEqual(scored_terminal("user_cancelled", nothing), "user_cancelled")
        # A product the runner stopped may report nothing, or the generic provider failure of the
        # call it abandoned; an ending nobody mapped stays.
        for stopped_ending in (None, "provider_error"):
            with self.subTest(ending=stopped_ending):
                self.assertEqual(scored_terminal(stopped_ending, nothing, "turn_wall",
                                                 stopped=True), "wall_clock_budget")
                # The reviews of 582e11c: a turn that ended by itself before the attempt ran out
                # of time keeps its own ending, so the classification still sees it.
                self.assertEqual(scored_terminal(stopped_ending, nothing, "attempt_automatic_wall"),
                                 stopped_ending)
        self.assertEqual(scored_terminal("terminal_nobody_mapped", nothing, "turn_wall",
                                         stopped=True), "terminal_nobody_mapped")


if __name__ == "__main__":
    unittest.main()


class TraceCountTests(unittest.TestCase):
    def test_a_trace_read_in_part_counts_nothing(self):
        """The review of 6376c20: a capture's completeness was recorded and never read, so an
        export that showed three of five turns counted three turns' work as the attempt's."""
        projected = {"page_actions": 3, "invalid_page_actions": 0, "business_calls": 2,
                     "repeated_observations": 1}
        self.assertEqual(_trace_counts(projected, whole=True), projected)
        self.assertEqual(_trace_counts(projected, whole=False),
                         {name: None for name in projected})
