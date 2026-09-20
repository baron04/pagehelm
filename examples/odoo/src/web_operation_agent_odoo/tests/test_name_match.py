"""A search by name does not establish that a record of that name exists (P7.5 CR-11).

The Odoo customer search matches a substring of the name. Asked whether a customer existed
-- one visible only in another company -- the model searched for the first word of its name,
the search found six customers of this company whose names begin with it, and the answer
headlined "已存在匹配记录" (P7.4 v2 development-001, d24). The adapter now reports how many
matches carry the whole name, and only those let an answer say a record of that name exists.
"""
from __future__ import annotations

import unittest

from web_operation_agent.answer.answer_protocol import submit
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.answer.evidence import Origin, QueryEvidence, TurnEvidence
from web_operation_agent_odoo.tests.test_adapter import _customers, _FakeOdoo


def _search(rows, needle):
    result = _FakeOdoo({"res.partner": _customers(rows)}).execute(
        "query_customers", {"name_contains": needle})
    turn = TurnEvidence()
    for item in result.evidence:
        turn.record(item)
    answer = submit(turn, {"outcome": "answered", "answer": {
        "version": 1, "coverage": "complete", "presentation": "table",
        "conclusions": [{"kind": "selection", "evidence_ref": "query#1",
                         "fields": ["name", "email"]}]}})
    return result.evidence[0], render_payload(
        answer, user_text="有没有这个客户？", max_chars=4000).replace("**", "")


class ANameSearchIsNotAnExistenceClaim(unittest.TestCase):
    def test_matches_that_only_contain_the_name_do_not_say_it_exists(self):
        query, text = _search([("Harbor Lights", "l@example.test"),
                               ("Harbor Foods", "f@example.test")], "Harbor")
        self.assertEqual(query.exact_matches, 0)
        self.assertNotIn("已存在", text)
        self.assertIn("与“Harbor”匹配的记录：共 2 条", text)
        self.assertIn("Harbor Lights", text)

    def test_the_whole_name_says_it_exists(self):
        query, text = _search([("P73 Manual Acceptance", "m@example.test"),
                               ("Zephyr", "z@example.test")], "p73 manual acceptance")
        self.assertEqual(query.exact_matches, 1)
        self.assertIn("已存在匹配记录：p73 manual acceptance（1 条）", text)

    def test_one_exact_among_several_says_both(self):
        query, text = _search([("Acme", "a@example.test"),
                               ("Acme Holdings", "h@example.test")], "Acme")
        self.assertEqual((query.exact_matches, query.matched_count), (1, 2))
        self.assertIn("已存在匹配记录：Acme（1 条）；与“Acme”匹配的共 2 条", text)

    def test_a_read_without_a_name_has_no_exact_count(self):
        result = _FakeOdoo({"res.partner": _customers([("Acme", "a@example.test")])}).execute(
            "query_customers", {})
        self.assertIsNone(result.evidence[0].exact_matches)

    def test_the_count_cannot_outrun_the_matches_or_exist_without_a_name(self):
        origin = Origin(kind="typed", reference="r", observed_at="now", scope="s")
        for looked_for, exact in (("", 0), ("Acme", 3)):
            with self.subTest(looked_for=looked_for, exact=exact), \
                    self.assertRaises(ValueError):
                QueryEvidence(evidence_id="query#1", origin=origin, collection="customers",
                              conditions=("x",), matched_count=2, looked_for=looked_for,
                              exact_matches=exact)


if __name__ == "__main__":
    unittest.main()
