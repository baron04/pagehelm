"""Read labels remain usable across API evidence, selection and calculation."""
from dataclasses import replace
import unittest

from web_operation_agent.answer.answer_protocol import compute, submit
from web_operation_agent.answer.conclusion import ConclusionRefused
from web_operation_agent.answer.computation import ComputationRefused
from web_operation_agent.answer.evidence import RecordEvidence
from web_operation_agent.tests.support.answer import _turn, AT


class ReadFieldBindingTests(unittest.TestCase):
    def turn(self):
        turn = _turn(records=(("record/1", "10"), ("record/2", "20"), ("record/3", "20")))
        turn._items = {key: replace(item, field_name="field_2", field_meaning="总金额")
                       if isinstance(item, RecordEvidence) else item
                       for key, item in turn._items.items()}
        return turn

    def test_read_label_selects_all_numeric_ties_and_their_actual_column(self):
        turn = self.turn()
        selected = compute(turn, {"operation":"argmax", "dataset_ref":"query#1", "field":"总金额"},
                           result_ref="calc#1", observed_at=AT)
        self.assertEqual(("record/2", "record/3"), selected.members)
        result = submit(turn, {"outcome":"answered", "answer":{"version":1,
            "coverage":"complete", "conclusions":[{"kind":"selection",
                "evidence_ref":"calc#1", "fields":["总金额"]}]}})
        item = result["conclusions"][0]
        self.assertEqual(("field_2",), item["fields"])
        self.assertEqual(("总金额",), item["field_labels"])
        self.assertEqual(["20", "20"], [row[0]["value"] for row in item["rows"]])

    def test_ambiguous_label_requires_key_in_both_answer_and_calculation(self):
        turn = self.turn()
        turn.record(replace(turn.get("record#1"), evidence_id="record#4", field_name="other_total"))
        with self.assertRaisesRegex(ComputationRefused, "ambiguous"):
            compute(turn, {"operation":"sum", "dataset_ref":"query#1", "field":"总金额"},
                    result_ref="calc#1", observed_at=AT)
        with self.assertRaisesRegex(ConclusionRefused, "ambiguous"):
            submit(turn, {"outcome":"answered", "answer":{"version":1, "coverage":"complete",
                "conclusions":[{"kind":"selection", "evidence_ref":"query#1", "fields":["总金额"]}]}})
        result = compute(turn, {"operation":"sum", "dataset_ref":"query#1", "field":"field_2"},
                         result_ref="calc#1", observed_at=AT)
        self.assertEqual("50", result.value)

    def test_other_read_label_and_synonym_do_not_bind(self):
        turn = self.turn()
        turn.record(replace(turn.get("record#1"), evidence_id="record#4",
                            origin=replace(turn.get("record#1").origin, reference="another read"),
                            field_meaning="金额"))
        for field in ("金额", "合计金额"):
            with self.subTest(field=field), self.assertRaises(ComputationRefused):
                compute(turn, {"operation":"sum", "dataset_ref":"query#1", "field":field},
                        result_ref="calc#1", observed_at=AT)

    def test_partial_population_does_not_become_full_sum_by_label(self):
        turn = self.turn()
        turn._items["query#1"] = replace(turn.get("query#1"), covers_whole_scope=False)
        result = compute(turn, {"operation":"sum", "dataset_ref":"query#1", "field":"总金额"},
                         result_ref="calc#1", observed_at=AT)
        self.assertFalse(result.covers_whole_scope)
