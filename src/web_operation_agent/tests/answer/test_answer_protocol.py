"""Model suggestions cannot supply their own facts, scope or success evidence."""
from dataclasses import replace
import unittest

from web_operation_agent.answer.answer_protocol import compute, evidence_index, submit, MAX_INDEX_BYTES
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.answer.conclusion import ConclusionRefused
from web_operation_agent.tests.support.answer import AT, _turn


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

    def test_source_details_follow_cited_computation_without_leaking_values_or_other_reads(self):
        turn = _turn()
        turn.record(replace(turn.get("record#1"), evidence_id="unrelated#1",
                            origin=replace(turn.get("record#1").origin, reference="other-call",
                                           scope="unrelated-private-scope"), value="private-value"))
        compute(turn, {"operation": "sum", "dataset_ref": "query#1", "field": "amount_total"},
                result_ref="calc#1", observed_at=AT)
        sources = submit(turn, _draft())["sources"]
        self.assertEqual({item["kind"] for item in sources}, {"typed", "computed"})
        self.assertNotIn("private", str(sources))
        self.assertNotIn("record#", str(sources))
        self.assertTrue(all(item["observed_at"] == AT for item in sources))
        self.assertTrue(any(item.get("records_scanned") == 5 for item in sources))

    def test_factual_prose_and_model_supplied_amounts_are_not_accepted(self):
        for location, key, value in (("answer", "prose", "The total is 90000"),
                                     ("conclusion", "value", "90000")):
            args = _draft("field_value", "record#1")
            target = args["answer"] if location == "answer" else args["answer"]["conclusions"][0]
            target[key] = value
            with self.subTest(location=location), self.assertRaises(ConclusionRefused):
                submit(_turn(), args)

    def test_not_found_subject_comes_from_the_trusted_query_not_the_model(self):
        turn = _turn(records=(), looked_for="S00042")
        answer = submit(turn, _draft("not_found", "query#1"))
        self.assertEqual(answer["conclusions"][0]["looked_for"], "S00042")
        forged = _draft("not_found", "query#1", looked_for="another customer")
        with self.assertRaises(ConclusionRefused):
            submit(turn, forged)

    def test_a_named_match_is_presented_as_an_existing_record_not_query_syntax(self):
        turn = _turn(scope="customers visible to the delegated user; state in draft,sent",
                     looked_for="P73 Manual Acceptance", records=(("res.partner/9", "1.00"),),
                     exact_matches=1)
        answer = submit(turn, _draft("selection", "query#1"))
        text = render_payload(answer, user_text="检查这个客户是否存在", max_chars=2000)
        self.assertIn("已存在匹配记录：P73 Manual Acceptance（1 条）", text.replace("**", ""))
        self.assertNotIn("state in draft,sent", text)
        self.assertIn("state in draft,sent", answer["sources"][0]["scope"],
                      "the executed condition remains available in answer provenance")

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

    def test_a_data_conclusion_with_prose_is_refused_with_the_repair_named(self):
        # A turn holding both a typed result and a Skill or source reference puts its
        # sentence on the data conclusion too. The refusal has to say which field to drop,
        # or every repair is spent rediscovering that.
        for field in ("statement", "support"):
            with self.subTest(field=field):
                draft = _draft("selection", "query#1", fields=["name"])
                draft["answer"]["conclusions"][0][field] = "four are still in draft"
                with self.assertRaises(ConclusionRefused) as caught:
                    submit(_turn(), draft)
                message = str(caught.exception)
                self.assertIn(f"selection conclusion carries no {field}", message)
                self.assertIn(f"Remove the {field} field", message)
                self.assertIn("skill_statement", message)

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
        self.assertIn("目前可以确认的部分", text)
        self.assertIn("显示限制", text)

    def test_action_prefix_does_not_hide_partial_answer_or_its_display_limit(self):
        turn = _turn(whole=False, records=tuple((f"record/{i}", "100") for i in range(50)))
        answer = submit(turn, _draft("selection", "query#1", fields=["amount_total"]))
        answer["action"] = {"kind": "navigation", "url": "http://app.test/records"}
        text = render_payload(answer, user_text="打开并列出所有金额", max_chars=512)
        self.assertLessEqual(len(text), 512)
        self.assertIn("已打开页面", text)
        self.assertIn("目前可以确认的部分", text)
        self.assertIn("显示限制", text)


if __name__ == "__main__":
    unittest.main()
