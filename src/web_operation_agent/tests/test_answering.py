from __future__ import annotations

import unittest

from web_operation_agent.answering import (
    StructuredQueryAnswer, render_structured_query_answer,
)


def _projection(*, complete: bool = True, records=None) -> StructuredQueryAnswer:
    selected = records if records is not None else (("Acme", "acme.test"),)
    return StructuredQueryAnswer(
        collection="companies",
        fields=("name", "domain"),
        records=tuple(selected),
        source="fixture typed API",
        scope="current_workspace",
        observed_at="2026-09-10T00:00:00+00:00",
        source_complete=complete,
        result_truncated=not complete,
        matched_count=len(selected),
        source_total=(len(selected) if complete else None),
        records_scanned=len(selected),
        incomplete_reason="" if complete else "bounded_scan_stopped",
    )


class StructuredQueryAnswerTests(unittest.TestCase):
    def test_projection_is_immutable_bounded_and_self_describing(self):
        projection = _projection()
        public = projection.to_json()

        self.assertEqual(public["records"], [{"name": "Acme", "domain": "acme.test"}])
        self.assertTrue(public["completeness"]["complete"])
        self.assertEqual(public["source"]["label"], "fixture typed API")
        self.assertEqual(len(projection.content_digest), 64)

        with self.assertRaisesRegex(ValueError, "incomplete source requires"):
            StructuredQueryAnswer(
                collection="companies", fields=("name",), records=(("Acme",),),
                source="fixture", scope="workspace", observed_at="now",
                source_complete=False, result_truncated=True, matched_count=1,
                source_total=None, records_scanned=1,
            )
        with self.assertRaisesRegex(ValueError, "record does not match"):
            StructuredQueryAnswer(
                collection="companies", fields=("name", "domain"),
                records=(("Acme",),), source="fixture", scope="workspace",
                observed_at="now", source_complete=True, result_truncated=False,
                matched_count=1, source_total=1, records_scanned=1,
            )
        with self.assertRaisesRegex(ValueError, "omitted matching record"):
            StructuredQueryAnswer(
                collection="companies", fields=("name",), records=(),
                source="fixture", scope="workspace", observed_at="now",
                source_complete=True, result_truncated=False, matched_count=1,
                source_total=1, records_scanned=1,
            )
        with self.assertRaisesRegex(ValueError, "query answer value is invalid"):
            _projection(records=(("safe\u202e", "domain.test"),))

    def test_renderer_states_only_real_limits_and_never_cuts_a_row(self):
        records = tuple((f"Company {index}", "x" * 80) for index in range(20))
        projection = _projection(records=records)
        rendered = render_structured_query_answer(
            projection, user_text="逐条列出所有公司", max_chars=512)

        self.assertLessEqual(len(rendered.text), 512)
        self.assertTrue(rendered.presentation_truncated)
        self.assertNotIn("完整性", rendered.text)
        self.assertNotIn("未截断", rendered.text)
        self.assertIn("显示限制", rendered.text)
        for line in rendered.text.splitlines():
            if "domain:" in line:
                self.assertTrue(line.endswith("x" * 80), line)

    def test_incomplete_result_carries_the_tool_reason_in_english(self):
        rendered = render_structured_query_answer(
            _projection(complete=False), user_text="List companies", max_chars=2_000)

        self.assertIn("may be incomplete", rendered.text)
        self.assertIn("bounded_scan_stopped", rendered.text)

    def test_complete_result_is_concise_while_preserving_source_evidence(self):
        rendered = render_structured_query_answer(
            _projection(), user_text="列出公司", max_chars=2_000)

        self.assertIn("查询结果：companies（1 条）", rendered.text)
        self.assertIn("数据来源：fixture", rendered.text)
        self.assertNotIn("完整性", rendered.text)
        self.assertNotIn("未截断", rendered.text)


if __name__ == "__main__":
    unittest.main()
