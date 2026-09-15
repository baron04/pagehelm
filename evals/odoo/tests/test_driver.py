import unittest

from evals.odoo import outcomes
from evals.odoo.driver import _cost, _with_residue, agent_assertions, scored_terminal


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


class ScoredTerminalTests(unittest.TestCase):
    def test_a_gateway_ending_names_the_terminal_only_where_the_runner_has_none(self):
        context = outcomes.GatewayFacts(terminal="context_length_exceeded")
        budget = outcomes.GatewayFacts(terminal="model_call_budget")
        for runner in ("provider_error", "failed", None):
            with self.subTest(runner=runner):
                self.assertEqual(scored_terminal(runner, context), "context_length_exceeded")
        self.assertEqual(scored_terminal("completed", context), "completed")
        self.assertEqual(scored_terminal("completed", budget), "model_call_budget")
        self.assertEqual(scored_terminal("provider_error", outcomes.GatewayFacts()),
                         "provider_error")

    def test_a_provider_deadline_scores_alike_whether_it_timed_out_or_answered_408(self):
        for status in ("transport_error", "http_error"):
            with self.subTest(status=status):
                facts = outcomes.gateway_facts(
                    [{"request_seq": 1, "status": status, "error_code": "provider_deadline"}], [])
                self.assertEqual((facts.events, facts.terminal), ((), "provider_deadline"))
                self.assertEqual(scored_terminal("provider_error", facts), "provider_deadline")
                self.assertEqual(scored_terminal("wall_clock_budget", facts), "wall_clock_budget")

    def test_a_wall_clock_the_runner_enforced_ends_the_attempt_whatever_the_product_said(self):
        nothing = outcomes.GatewayFacts()
        for limit in ("turn_wall", "attempt_automatic_wall"):
            with self.subTest(limit=limit):
                self.assertEqual(scored_terminal("user_cancelled", nothing, limit),
                                 "wall_clock_budget")
        self.assertEqual(scored_terminal("user_cancelled",
                                         outcomes.GatewayFacts(terminal="model_call_budget"),
                                         "turn_wall"), "model_call_budget")
        self.assertEqual(scored_terminal("user_cancelled", nothing), "user_cancelled")


if __name__ == "__main__":
    unittest.main()
