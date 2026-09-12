"""Model suggestions cannot supply their own facts, scope or success evidence."""
from dataclasses import replace
import unittest

from web_operation_agent.answer_protocol import compute, evidence_index, submit, MAX_INDEX_BYTES
from web_operation_agent.answer_view import render_payload
from web_operation_agent.conclusion import ConclusionRefused
from web_operation_agent.tests.test_computation import _turn, AT


def _draft(kind="aggregate", ref="calc#1", **fields):
    return {"outcome": "answered", "answer": {"version": 1, "coverage": "complete",
            "conclusions": [{"kind": kind, "evidence_ref": ref, **fields}]}}


class AnswerWireContract(unittest.TestCase):
    def test_the_model_names_an_operation_and_receives_a_server_computed_result(self):
        turn = _turn()
        compute(turn, {"operation": "sum", "dataset_ref": "query#1", "field": "amount_total"},
                result_ref="calc#1", observed_at=AT)
        answer = submit(turn, _draft())
        self.assertEqual(answer["conclusions"][0]["value"], "7210.00")
        self.assertEqual(answer["prose"], "")

    def test_factual_prose_and_model_supplied_amounts_are_not_accepted(self):
        for location, key, value in (("answer", "prose", "The total is 90000"),
                                     ("conclusion", "value", "90000")):
            args = _draft("field_value", "record#1")
            target = args["answer"] if location == "answer" else args["answer"]["conclusions"][0]
            target[key] = value
            with self.subTest(location=location), self.assertRaises(ConclusionRefused):
                submit(_turn(), args)

    def test_not_found_subject_comes_from_the_executed_conditions(self):
        turn = _turn(records=())
        answer = submit(turn, _draft("not_found", "query#1"))
        self.assertEqual(answer["conclusions"][0]["looked_for"], "state in draft,sent")
        forged = _draft("not_found", "query#1", looked_for="another customer")
        with self.assertRaises(ConclusionRefused):
            submit(turn, forged)

    def test_a_partial_zero_match_is_not_an_absence_claim(self):
        with self.assertRaisesRegex(ConclusionRefused, "partial search"):
            submit(_turn(whole=False, records=()), _draft("not_found", "query#1"))

    def test_missing_value_does_not_erase_a_supported_part(self):
        turn = _turn()
        turn.record(replace(turn.get("record#1"), evidence_id="gap#1", field_name="phone",
                            field_was_read=False, value=None, value_type="", unit=""))
        args = _draft("field_value", "record#1")
        args["answer"]["conclusions"].append({"kind": "undetermined", "evidence_ref": "gap#1"})
        answer = submit(turn, args)
        self.assertEqual(answer["coverage"], "partial")
        self.assertIn("1740.00", render_payload(answer, user_text="金额和电话", max_chars=2000))

    def test_value_evidence_cannot_be_called_an_unread_gap(self):
        with self.assertRaises(ConclusionRefused):
            submit(_turn(), _draft("undetermined", "record#1"))

    def test_unknown_field_in_selection_is_not_rendered_empty(self):
        with self.assertRaisesRegex(ConclusionRefused, "not read"):
            submit(_turn(), _draft("selection", "query#1", fields=["phone"]))

    def test_wrong_conclusion_type_cannot_relabel_a_query_as_an_aggregate(self):
        with self.assertRaises(ConclusionRefused):
            submit(_turn(), _draft("aggregate", "query#1"))

    def test_large_index_is_explicitly_bounded_without_mutating_evidence(self):
        turn = _turn()
        turn.record(replace(turn.get("record#1"), evidence_id="large#1", value="x" * 30000))
        before = len(turn)
        text = evidence_index(turn, turn.ids())
        self.assertIn('"index_truncated":true', text)
        self.assertLessEqual(len(text.encode()), MAX_INDEX_BYTES)
        self.assertEqual(len(turn), before)

    def test_display_limit_keeps_the_partial_answer_warning(self):
        turn = _turn(whole=False, records=tuple((f"record/{i}", "100") for i in range(50)))
        answer = submit(turn, _draft("selection", "query#1", fields=["amount_total"]))
        text = render_payload(answer, user_text="列出所有金额", max_chars=512)
        self.assertLessEqual(len(text), 512)
        self.assertIn("部分回答", text)
        self.assertIn("显示限制", text)

    def test_action_prefix_does_not_hide_partial_answer_or_its_display_limit(self):
        turn = _turn(whole=False, records=tuple((f"record/{i}", "100") for i in range(50)))
        answer = submit(turn, _draft("selection", "query#1", fields=["amount_total"]))
        answer["action"] = {"kind": "navigation", "url": "http://app.test/records"}
        text = render_payload(answer, user_text="打开并列出所有金额", max_chars=512)
        self.assertLessEqual(len(text), 512)
        self.assertIn("已打开页面", text)
        self.assertIn("部分回答", text)
        self.assertIn("显示限制", text)


if __name__ == "__main__":
    unittest.main()
