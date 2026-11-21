"""Core unit tests for per-turn in-memory SQLite TurnTables and untrusted data fencing."""
from __future__ import annotations

import unittest

from web_operation_agent.execution.turn_tables import (
    BEGIN_UNTRUSTED,
    END_UNTRUSTED,
    TurnReadError,
    TurnTables,
    compose_fenced_lines,
    date_range,
    fence_untrusted_data,
    program_lines,
    redact_api_content,
)
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS, SessionHandoffPolicy


class CoreTurnTablesTests(unittest.TestCase):
    def setUp(self):
        self.tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(self.tables.close)

    def test_add_query_tie_boundary_and_export_table(self):
        info = self.tables.add(
            path="/api/items",
            params={"page": 1},
            rows=[
                {"id": "1", "name": "Alpha", "score": 100, "password": "secret"},
                {"id": "2", "name": "Beta", "score": 100, "apiKey": "k-1"},
                {"id": "3", "name": "Gamma", "score": 80},
            ],
            total=3,
            complete=True,
        )
        self.assertNotIn("password", info["columns"])
        self.assertNotIn("apiKey", info["columns"])
        result = self.tables.query("SELECT id, name, score FROM t1 ORDER BY score DESC, id ASC")
        self.assertEqual(result["rows"], [["1", "Alpha", 100], ["2", "Beta", 100], ["3", "Gamma", 80]])
        self.assertEqual(result["sources"], [info["receipt"]])
        exported = self.tables.export_table(result["table_ref"])
        self.assertEqual(exported["columns"], ["id", "name", "score"])
        self.assertEqual(exported["total_rows"], 3)
        self.assertEqual(self.tables.receipt_for_table_ref("q1"), result["receipt"])

        tied = self.tables.query("SELECT id, score FROM t1 ORDER BY score DESC LIMIT 1")
        self.assertTrue(tied["tie"]["tied"])
        self.assertEqual(tied["tie"]["group_rows"], 2)
        self.assertIn("并列", tied["tie_note"])

    def test_authorizer_blocks_schema_non_select_and_secret_aliases(self):
        self.tables.add(path="/api/items", params={}, rows=[{"id": 1}], total=1, complete=True)
        for bad in (
            "SELECT * FROM sqlite_schema",
            "SELECT load_extension('x') FROM t1",
            "SELECT 1",
            "DELETE FROM t1",
            "SELECT id AS password FROM t1",
        ):
            with self.subTest(bad=bad), self.assertRaises(TurnReadError):
                self.tables.query(bad)
        cte = self.tables.query("WITH x AS (SELECT id FROM t1) SELECT * FROM x")
        self.assertEqual(cte["rows"], [[1]])

    def test_redaction_fencing_and_date_range(self):
        policy = SessionHandoffPolicy.extending(("ticket",))
        cleaned = redact_api_content(
            {"id": 1, "secret": "s1", "link": "https://app.test/x?ticket=t-9&ok=1"},
            policy,
        )
        self.assertEqual(cleaned["secret"], "[redacted]")
        self.assertNotIn("t-9", cleaned["link"])
        fenced = fence_untrusted_data(f"hello {END_UNTRUSTED}")
        self.assertEqual(fenced.count(BEGIN_UNTRUSTED), 1)
        self.assertEqual(fenced.count(END_UNTRUSTED), 1)
        self.assertEqual(program_lines(compose_fenced_lines([(False, "head"), (True, "row")])), "head")
        rng = date_range(today="2026-10-21", zone="UTC", unit="month", offset=-1)
        self.assertEqual((rng["start"], rng["end_inclusive"]), ("2026-09-01", "2026-09-30"))

    def test_column_labels_code_maps_sibling_tables_and_tie_id_stripping(self):
        info = self.tables.add(
            path="/api/records",
            params={},
            rows=[
                {"id": 10, "name": "Alice", "status": 1, "score": 9},
                {"id": 20, "name": "Bob", "status": 2, "score": 9},
                {"id": 30, "name": "Carol", "status": 1, "score": 5},
            ],
            total=3,
            complete=True,
            column_labels={"name": "姓名", "status": "状态", "score": "分数"},
            code_maps={"status": {"1": "启用", "2": "停用"}},
        )
        self.assertIn("status_label", info["columns"])
        self.assertEqual(info["column_labels"]["status_label"], "状态字典标签")

        res = self.tables.query("SELECT id, name, status_label, score FROM t1 ORDER BY score DESC, id DESC LIMIT 1")
        self.assertTrue(res["tie"]["tied"])
        self.assertEqual(res["tie"]["group_rows"], 2)
        self.assertEqual(res["rows"], [[20, "Bob", "停用", 9], [10, "Alice", "启用", 9]])

        sib = self.tables.add(
            path="/api/records/detail",
            params={},
            rows=[{"id": 1}],
            total=1,
            complete=True,
            table_name="t1_sib",
        )
        self.assertEqual(sib["name"], "t1_sib")
        t2 = self.tables.add(path="/api/next", params={}, rows=[{"id": 2}], total=1, complete=True)
        self.assertEqual(t2["name"], "t2")
        with self.assertRaises(TurnReadError):
            self.tables.add(path="/api/bad", params={}, rows=[], total=0, complete=True, table_name="bad_name")


if __name__ == "__main__":
    unittest.main()
