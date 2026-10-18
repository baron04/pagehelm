"""Exact calculations retain actor, scope, missingness and complete delivery."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import unittest

from web_operation_agent.answer.evidence import Origin
from web_operation_agent.execution.read_summary import (
    Missing, RelationValue, SummaryColumn, SummaryRead, SummaryRefused, SummaryRow,
    SummarySpec, compute_summary, render_summary, summary_schema,
)


def read(columns, rows=(), *, complete=True, matched_count=None, actor="actor-a"):
    return SummaryRead(Origin("typed", "synthetic:list", "2026-10-18T10:00:00+08:00",
        "actor-visible records; state=open"), actor, tuple(SummaryColumn(*column) for column in columns),
        tuple(SummaryRow(str(i), tuple(values)) for i, values in enumerate(rows)),
        len(rows) if matched_count is None else matched_count, len(rows), complete,
        "multiple page reads; not a transactional snapshot")


def summarize(source, **spec):
    return compute_summary(source, SummarySpec.from_dict(spec))


class ReadSummaryTests(unittest.TestCase):
    def assertRefused(self, code, function, *args, **kwargs):
        with self.assertRaises(SummaryRefused) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_decimal_sum_is_exact_and_retains_display_unit(self):
        source = read([("amount", "number", "元")], [(Decimal("0.10"),), (Decimal("0.20"),)])
        result = summarize(source, measures=[{"op": "sum", "column": "amount", "as": "total"}])
        self.assertEqual((("0.30",),), result.records)
        self.assertEqual("元", result.columns[0].unit)
        self.assertEqual(source.origin, result.origin)
        self.assertEqual(source.actor_digest, result.actor_digest)
        self.assertTrue(result.complete)

    def test_conditions_do_not_rewrite_names_or_merge_duplicates(self):
        source = read([("name", "text")], [(" Acme",), ("Acme",), ("Acme",), ("acme",), ("Acme East",)])
        exact = summarize(source, where=[{"column": "name", "op": "=", "value": "Acme"}], columns=["name"])
        contains = summarize(source, where=[{"column": "name", "op": "contains", "value": "Acme"}], columns=["name"])
        self.assertEqual((("Acme",), ("Acme",)), exact.rows)
        self.assertEqual(("1", "2"), exact.matched_record_ids)
        self.assertEqual(4, contains.matched_count)
        self.assertEqual(" Acme", contains.rows[0][0])

    def test_all_closed_condition_operators(self):
        source = read([("value", "number")], [(1,), (2,), (3,), (None,), ("",)])
        cases = [("=", 2, 1), ("!=", 2, 2), (">", 1, 2), (">=", 2, 2), ("<", 3, 2),
                 ("<=", 2, 2), ("between", [1, 2], 2), ("in", [1, 3], 2), ("empty", None, 2), ("not_empty", None, 3)]
        for op, value, count in cases:
            condition = {"column": "value", "op": op, **({"value": value} if op not in {"empty", "not_empty"} else {})}
            with self.subTest(op=op):
                result = summarize(source, where=[condition], measures=[{"op": "count", "as": "n"}])
                self.assertEqual(((str(count),),), result.rows)
        names = read([("name", "text")], [("A",), ("AB",), ("BA",)])
        self.assertEqual(2, summarize(names, where=[{"column": "name", "op": "prefix", "value": "A"}], columns=["name"]).matched_count)

    def test_and_conditions_keep_the_actual_subset(self):
        source = read([("name", "text"), ("amount", "number")], [("A", 3), ("A", 8), ("B", 8)])
        result = summarize(source, where=[{"column": "name", "op": "=", "value": "A"},
            {"column": "amount", "op": ">", "value": 5}], measures=[{"op": "sum", "column": "amount", "as": "total"}])
        self.assertEqual((("8",),), result.rows)
        self.assertEqual(("1",), result.matched_record_ids)
        self.assertEqual(3, result.source_count)
        self.assertEqual(1, result.matched_count)

    def test_relations_group_by_key_not_display_name(self):
        source = read([("department", "relation")], [(RelationValue("104", "Sales"),),
            (RelationValue("108", "Sales"),), (RelationValue("104", "Sales"),), (None,)])
        result = summarize(source, group_by=["department"], measures=[{"op": "count", "as": "people"}])
        self.assertEqual((("Sales (104)", "2"), ("Sales (108)", "1"), (None, "1")), result.rows)
        self.assertEqual(1, dict(result.null_counts)["department"])
        self.assertEqual((("104",), ("108",), (None,)), result.group_keys)
        self.assertEqual((("0", "2"), ("1",), ("3",)), result.row_record_ids)
        self.assertEqual([["104"], ["108"], [None]], result.to_json()["group_keys"])
        by_id = summarize(source, where=[{"column": "department", "op": "=", "value": "108"}], columns=["department"])
        self.assertEqual(1, by_id.matched_count)

    def test_two_grouping_columns_and_group_value_gaps(self):
        source = read([("dept", "text"), ("state", "text"), ("amount", "number")],
            [("A", "on", 3), ("A", "on", None), ("A", "off", 2)])
        result = summarize(source, group_by=["dept", "state"], measures=[{"op": "sum", "column": "amount", "as": "total"}])
        self.assertEqual((("A", "on", "3"), ("A", "off", "2")), result.rows)
        self.assertEqual(("amount", 0, 1, 0), result.group_stats[0][2][2])

    def test_count_distinct_is_explicit_preserves_empty_text_and_relation_identity(self):
        source = read([("name", "text")], [("A",), ("A",), ("",), (None,)])
        result = summarize(source, measures=[{"op": "count_distinct", "column": "name", "as": "names"}, {"op": "count", "as": "rows"}])
        self.assertEqual((("2", "4"),), result.rows)
        relations = read([("dept", "relation")], [(RelationValue("1", "Same"),), (RelationValue("2", "Same"),)])
        result = summarize(relations, measures=[{"op": "count_distinct", "column": "dept", "as": "departments"}])
        self.assertEqual((("2",),), result.rows)

    def test_nth_position_includes_all_cutoff_ties(self):
        source = read([("dept", "text"), ("amount", "number")], [("A", 9), ("B", 8), ("C", 8), ("D", 2)])
        result = summarize(source, group_by=["dept"], measures=[{"op": "sum", "column": "amount", "as": "total"}],
            top={"by": "total", "n": 2, "direction": "desc"})
        self.assertEqual((("A", "9"), ("B", "8"), ("C", "8")), result.rows)
        self.assertEqual("8", result.tie_cutoff)
        self.assertEqual(2, result.ties_at_cutoff)
        ascending = summarize(source, group_by=["dept"], measures=[{"op": "sum", "column": "amount", "as": "total"}],
            top={"by": "total", "n": 2, "direction": "asc"})
        self.assertEqual((("D", "2"), ("B", "8"), ("C", "8")), ascending.rows)
        self.assertEqual((("D",), ("B",), ("C",)), ascending.group_keys)
        self.assertEqual((("3",), ("1",), ("2",)), ascending.row_record_ids)
        self.assertEqual(("0", "1", "2", "3"), ascending.matched_record_ids)

    def test_record_ranking_requires_explicit_identity_group_and_constant_name(self):
        source = read([("id", "text"), ("name", "text"), ("price", "number")], [("1", "Same", 9), ("2", "Same", 8)])
        result = summarize(source, group_by=["id"], columns=["name"], measures=[{"op": "max", "column": "price", "as": "price_value"}],
            top={"by": "price_value", "n": 1, "direction": "desc"})
        self.assertEqual((("1", "Same", "9"),), result.rows)
        self.assertRefused("ambiguous_projection", summarize, source, columns=["id"], measures=[{"op": "count", "as": "n"}])

    def test_missing_null_empty_and_zero_are_different(self):
        source = read([("value", "number")], [(Missing.MISSING,), (None,), ("",), (0,)])
        result = summarize(source, measures=[{"op": "count", "as": "n"}])
        self.assertEqual((("4",),), result.rows)
        self.assertEqual({"value": 1}, dict(result.missing_counts))
        self.assertEqual({"value": 1}, dict(result.null_counts))
        self.assertEqual({"value": 1}, dict(result.empty_counts))
        for spec in ({"columns": ["value"]}, {"group_by": ["value"], "measures": [{"op": "count", "as": "n"}]},
                     {"where": [{"column": "value", "op": "empty"}], "columns": ["value"]},
                     {"measures": [{"op": "sum", "column": "value", "as": "total"}]}):
            self.assertRefused("missing_column", summarize, source, **spec)

    def test_empty_population_has_zero_count_sum_and_null_extremes(self):
        result = summarize(read([("amount", "number", "元")]), measures=[{"op": "count", "as": "n"},
            {"op": "sum", "column": "amount", "as": "total"}, {"op": "min", "column": "amount", "as": "least"},
            {"op": "max", "column": "amount", "as": "greatest"}])
        self.assertEqual((("0", "0", None, None),), result.rows)
        self.assertTrue(result.complete)
        self.assertEqual(((),), result.group_keys)
        self.assertEqual(((),), result.row_record_ids)

    def test_all_null_numeric_population_is_not_zero(self):
        result = summarize(read([("amount", "number")], [(None,), ("",)]),
            measures=[{"op": "sum", "column": "amount", "as": "total"}])
        self.assertEqual(((None,),), result.rows)
        self.assertIn("not numeric zero", result.incomplete_reason)
        self.assertTrue(result.complete)

    def test_incomplete_read_never_produces_partial_calculation(self):
        result = summarize(read([("amount", "number")], [(4,)], complete=False, matched_count=10),
            measures=[{"op": "sum", "column": "amount", "as": "total"}])
        self.assertFalse(result.complete)
        self.assertEqual((), result.rows)
        self.assertEqual(10, result.source_matched_count)
        self.assertIn("no summary", result.incomplete_reason)
        self.assertEqual((), result.group_keys)
        self.assertEqual((), result.row_record_ids)

    def test_unknown_fields_and_invalid_types_are_refused_even_for_empty_sets(self):
        source = read([("name", "text")])
        self.assertRefused("unknown_column", summarize, source, columns=["unknown"])
        self.assertRefused("type_mismatch", summarize, source, measures=[{"op": "sum", "column": "name", "as": "total"}])
        self.assertRefused("type_mismatch", summarize, source, where=[{"column": "name", "op": ">", "value": "A"}], columns=["name"])
        self.assertRefused("invalid_spec", summarize, read([("value", "number")]),
            where=[{"column": "value", "op": "between", "value": [3, 1]}], columns=["value"])

    def test_boolean_float_numeric_strings_and_nonfinite_values_are_not_numbers(self):
        for value in (True, False, 0.1, "2", Decimal("NaN"), Decimal("Infinity"), Decimal("1E+999999999"), Decimal("1E-999999999")):
            with self.subTest(value=value):
                self.assertRefused("type_mismatch", read, [("value", "number")], [(value,)])
        self.assertRefused("type_mismatch", summarize, read([("value", "number")]),
            where=[{"column": "value", "op": "=", "value": True}], columns=["value"])

    def test_numeric_group_identity_does_not_depend_on_python_storage_type(self):
        result = summarize(read([("value", "number")], [(1,), (Decimal("1.0"),)]), group_by=["value"],
            measures=[{"op": "count", "as": "n"}])
        self.assertEqual((("1", "2"),), result.rows)
        self.assertEqual((("1",),), result.group_keys)
        self.assertEqual((("0", "1"),), result.row_record_ids)

    def test_group_machine_keys_preserve_scalar_types_and_calendar_instants(self):
        source = read([("day", "date"), ("instant", "datetime")], [
            (date(2026, 10, 19), "2026-10-19T08:00:00+08:00"),
            ("2026-10-19", datetime(2026, 10, 19, tzinfo=timezone.utc))])
        result = summarize(source, group_by=["day", "instant"], measures=[{"op": "count", "as": "n"}])
        self.assertEqual((("2026-10-19", "2026-10-19T00:00:00+00:00"),), result.group_keys)
        self.assertEqual((("0", "1"),), result.row_record_ids)

    def test_list_and_global_rows_have_unambiguous_native_members(self):
        source = read([("name", "text")], [("Same",), ("Same",)])
        listed = summarize(source, columns=["name"])
        self.assertEqual(((), ()), listed.group_keys)
        self.assertEqual((("0",), ("1",)), listed.row_record_ids)
        counted = summarize(source, measures=[{"op": "count", "as": "n"}])
        self.assertEqual(((),), counted.group_keys)
        self.assertEqual((("0", "1"),), counted.row_record_ids)

    def test_native_group_membership_cannot_invent_repeat_or_omit_records(self):
        source = read([("dept", "relation")], [(RelationValue("1", "Same"),), (RelationValue("2", "Same"),)])
        result = summarize(source, group_by=["dept"], measures=[{"op": "count", "as": "n"}])
        for change in ({"group_keys": ()}, {"group_keys": (("1",),)},
                       {"group_keys": (("1",), ("1",))}, {"group_keys": (("1", "extra"), ("2",))},
                       {"group_keys": [["1"], ["2"]]}, {"row_record_ids": (("0",), ("0",))},
                       {"row_record_ids": (("0",), ("absent",))}, {"row_record_ids": (("0",), ())},
                       {"row_record_ids": [["0"], ["1"]]}):
            with self.subTest(change=change):
                self.assertRefused("invalid_result", replace, result, **change)

    def test_dates_are_typed_and_datetimes_require_explicit_offsets(self):
        source = read([("day", "date"), ("instant", "datetime")],
            [(date(2026, 10, 18), datetime(2026, 10, 18, 10, tzinfo=timezone.utc)),
             ("2026-10-19", "2026-10-19T10:00:00+08:00")])
        result = summarize(source, where=[{"column": "day", "op": "=", "value": "2026-10-18"}],
            measures=[{"op": "min", "column": "instant", "as": "earliest"}])
        self.assertEqual((("2026-10-18 10:00:00+00:00",),), result.rows)
        self.assertRefused("type_mismatch", summarize, source,
            where=[{"column": "instant", "op": ">=", "value": "2026-10-18 00:00:00"}], columns=["day"])
        self.assertRefused("type_mismatch", read, [("instant", "datetime")], [(datetime(2026, 10, 18),)])
        self.assertRefused("type_mismatch", read, [("day", "date")], [("2026-02-30",)])
        self.assertRefused("type_mismatch", summarize, source,
            where=[{"column": "day", "op": "=", "value": "20261018"}], columns=["day"])

    def test_half_open_calendar_ranges_include_fractional_last_second_but_not_next_period(self):
        source = read([("instant", "datetime")], [
            ("2026-08-31T23:59:59.999+08:00",), ("2026-09-01T00:00:00+08:00",),
            ("2026-09-30T23:59:59.999+08:00",), ("2026-10-01T00:00:00+08:00",)])
        result = summarize(source, where=[
            {"column": "instant", "op": ">=", "value": "2026-09-01T00:00:00+08:00"},
            {"column": "instant", "op": "<", "value": "2026-10-01T00:00:00+08:00"}],
            measures=[{"op": "count", "as": "n"}])
        self.assertEqual(result.rows, (("2",),))
        inclusive = summarize(source, where=[{"column": "instant", "op": "between",
            "value": ["2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00"]}],
            measures=[{"op": "count", "as": "n"}])
        self.assertEqual(inclusive.rows, (("3",),))

    def test_read_identity_count_and_actor_contracts_are_bounded(self):
        source = read([("name", "text")], [("A",)])
        self.assertRefused("invalid_input", replace, source, rows=(source.rows[0], source.rows[0]), matched_count=2, records_scanned=2)
        self.assertRefused("invalid_input", replace, source, matched_count=2)
        self.assertRefused("invalid_input", replace, source, actor_digest="")
        self.assertRefused("invalid_input", replace, source, rows=tuple(SummaryRow(str(i), ("A",)) for i in range(5001)), matched_count=5001, records_scanned=5001)
        self.assertRefused("missing_column", replace, source, rows=(SummaryRow("1", ()),))

    def test_inconsistent_relation_labels_and_unrankable_nulls_fail_closed(self):
        source = read([("dept", "relation")], [(RelationValue("1", "Old"),), (RelationValue("1", "New"),)])
        self.assertRefused("ambiguous_projection", summarize, source, group_by=["dept"], measures=[{"op": "count", "as": "n"}])
        source = read([("dept", "text"), ("price", "number")], [("A", 2), ("B", None)])
        self.assertRefused("unrankable_result", summarize, source, group_by=["dept"],
            measures=[{"op": "max", "column": "price", "as": "greatest"}], top={"by": "greatest", "n": 1, "direction": "desc"})

    def test_schema_is_closed_and_does_not_expose_rows_or_actor(self):
        schema = summary_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual({"where", "group_by", "measures", "columns", "top"}, set(schema["properties"]))
        self.assertEqual(50, schema["properties"]["top"]["properties"]["n"]["maximum"])
        self.assertNotIn("pattern", schema["properties"]["columns"]["items"])
        json.dumps(schema)
        for value in ({"rows": []}, {}, {"columns": ["name"], "where": [{"column": "name", "op": "eval", "value": "x"}]},
                      {"columns": ["name"], "where": [{"column": "name", "op": "empty", "value": None}]},
                      {"columns": ["name", "name"]}, {"group_by": ["name"], "columns": ["name"]},
                      {"measures": [{"op": "count", "column": "name", "as": "n"}]},
                      {"measures": [{"op": "sum", "as": "n"}]},
                      {"measures": [{"op": "count", "as": "n"}], "top": {"by": "n", "n": 51, "direction": "desc"}}):
            with self.subTest(value=value):
                self.assertRefused("invalid_spec", SummarySpec.from_dict, value)

    def test_summary_schema_is_accepted_by_the_typed_tool_registry(self):
        from web_operation_agent.business.tools.registry import _validate_schema, validate_input
        schema = summary_schema()
        _validate_schema(schema)
        validate_input(schema, {"where": [{"column": "value", "op": "in", "value": [1.25, "A", True, None]}],
            "measures": [{"op": "count", "as": "n"}]})

    def test_results_and_specs_are_immutable_and_digest_binds_actor_scope_and_values(self):
        source = read([("name", "text")], [("A",)])
        result = summarize(source, columns=["name"])
        with self.assertRaises(FrozenInstanceError):
            result.actor_digest = "other"
        before = result.content_digest
        public = result.to_json()
        public["rows"][0][0] = "Changed"
        self.assertEqual(before, result.content_digest)
        self.assertNotEqual(before, summarize(replace(source, actor_digest="actor-b"), columns=["name"]).content_digest)
        self.assertNotEqual(before, summarize(replace(source, origin=replace(source.origin, scope="other")), columns=["name"]).content_digest)

    def test_result_constructor_refuses_mutability_and_inconsistent_proofs(self):
        result = summarize(read([("name", "text")], [("A",)]), columns=["name"])
        for change in ({"rows": [["A"]]}, {"rows": ((1,),)}, {"rows": (("A", "B"),)},
                       {"source_matched_count": 2}, {"matched_count": 0}, {"matched_record_ids": ("1", "1")},
                       {"missing_counts": (("name", 2),)}, {"columns": ()}, {"columns": (SummaryColumn("other", "text"),)}, {"result_truncated": True},
                       {"source_complete": False}, {"rows": ()}):
            with self.subTest(change=change):
                self.assertRefused("invalid_result", replace, result, **change)
        aggregate = summarize(read([("value", "number")]), measures=[{"op": "count", "as": "n"}])
        self.assertEqual((("0",),), aggregate.rows)
        self.assertTrue(aggregate.complete)
        self.assertRefused("invalid_result", replace, aggregate, rows=(("NaN",),))
        self.assertRefused("invalid_result", replace, aggregate, rows=(("not a number",),))

    def test_renderer_explains_conditions_without_exposing_spec_or_alias(self):
        source = read([("name", "text"), ("amount", "number", "元")], [("A", 3)])
        source = replace(source, origin=replace(source.origin, scope='{"state":"open","ownerId":7}'))
        result = summarize(source, where=[{"column": "amount", "op": ">", "value": 1}],
            group_by=["name"], measures=[{"op": "sum", "column": "amount", "as": "internal_alias"}],
            top={"by": "internal_alias", "n": 1, "direction": "desc"})
        text = render_summary(result, max_chars=4000, user_text="按名称汇总").text
        self.assertIn("amount (元) 大于 1", text)
        self.assertIn("amount 合计 (元)", text)
        self.assertIn("临界值全部并列", text)
        self.assertIn("state=open; ownerId=7", text)
        self.assertNotIn("internal", text)
        self.assertNotIn('"where"', text)

    def test_relation_key_already_used_as_label_is_not_duplicated(self):
        result = summarize(read([("dept", "relation")], [(RelationValue("104", "104"),)]), columns=["dept"])
        self.assertEqual((("104",),), result.rows)

    def test_filter_source_units_remain_auditable_when_only_a_count_is_delivered(self):
        result = summarize(read([("amount", "number", "元")], [(Decimal("1.20"),), (Decimal("0.20"),)]),
            where=[{"column": "amount", "op": ">", "value": 1}], measures=[{"op": "count", "as": "n"}])
        self.assertEqual((SummaryColumn("amount", "number", "元"),), result.source_columns)
        self.assertEqual((SummaryColumn("n", "number"),), result.columns)
        self.assertEqual([{"name": "amount", "kind": "number", "unit": "元"}], result.to_json()["source_columns"])
        self.assertIn("amount (元) 大于 1", render_summary(result, max_chars=4000, user_text="多少条").text)
        for change in ({"source_columns": ()}, {"source_columns": [SummaryColumn("amount", "number", "元")]},
                       {"source_columns": (SummaryColumn("other", "number", "元"),)}):
            with self.subTest(change=change):
                self.assertRefused("invalid_result", replace, result, **change)

    def test_output_units_and_types_cannot_disagree_with_saved_source_columns(self):
        result = summarize(read([("amount", "number", "元")], [(Decimal("1.20"),)]),
            measures=[{"op": "sum", "column": "amount", "as": "total"}])
        for column in (SummaryColumn("total", "number", "分"), SummaryColumn("total", "text")):
            self.assertRefused("invalid_result", replace, result, columns=(column,))
        changed = replace(result, source_columns=(SummaryColumn("amount", "number", "分"),),
            columns=(SummaryColumn("total", "number", "分"),))
        self.assertNotEqual(result.content_digest, changed.content_digest)

    def test_markdown_values_are_literal_and_original_spaces_remain(self):
        result = summarize(read([("name", "text")], [(" **[x](https://bad.test)** ",), ("a|b",)]), columns=["name"])
        rendered = render_summary(result, max_chars=4000, user_text="列出名字")
        self.assertIn(r" \*\*\[x\](https://bad.test)\*\* ", rendered.text)
        self.assertIn("a|b", rendered.text)
        self.assertNotIn("| ---", rendered.text)
        self.assertIn("state=open", rendered.text)
        self.assertIn("multiple page reads", rendered.text)
        self.assertEqual(2, rendered.presented_records)
        self.assertFalse(rendered.presentation_truncated)

    def test_final_render_budget_never_silently_omits_rows(self):
        result = summarize(read([("name", "text")], [(str(i) * 80,) for i in range(20)]), columns=["name"])
        rendered = render_summary(result, max_chars=512)
        self.assertTrue(rendered.presentation_truncated)
        self.assertEqual(0, rendered.presented_records)
        self.assertLessEqual(len(rendered.text), 512)
        self.assertNotIn(result.rows[0][0], rendered.text)

    def test_computation_budget_does_not_select_arbitrary_tied_groups(self):
        source = read([("id", "text"), ("name", "text")], [(str(i), "x" * 1000) for i in range(110)])
        result = summarize(source, group_by=["id"], columns=["name"], measures=[{"op": "count", "as": "n"}],
            top={"by": "n", "n": 1, "direction": "desc"})
        self.assertEqual(110, result.ties_at_cutoff)
        self.assertFalse(result.complete)
        self.assertTrue(result.result_truncated)
        self.assertEqual((), result.rows)
        self.assertEqual(110, result.matched_count)
        self.assertEqual((), result.group_keys)
        self.assertEqual((), result.row_record_ids)


if __name__ == "__main__":
    unittest.main()
