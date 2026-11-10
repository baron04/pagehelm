import unittest

from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent_yudao.turn_tables import TurnReadError, TurnTables, date_range


class TurnTablesTest(unittest.TestCase):
    def setUp(self):
        self.tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(self.tables.close)

    def test_reads_preserve_nulls_codes_and_duplicate_names(self):
        info = self.tables.add(path="examples/page", params={"status": 0}, rows=[
            {"id": "001", "name": "同名", "amount": 3, "password": "synthetic-only"},
            {"id": "002", "name": "同名", "amount": None},
            {"id": "003", "name": "其它", "amount": 4},
        ], total=4, complete=False)
        self.assertEqual((info["row_count"], info["total"], info["complete"]), (3, 4, False))
        self.assertNotIn("password", info["columns"])
        answer = self.tables.query(
            'SELECT name, COUNT(*) n, SUM(amount), MIN(id) FROM t1 GROUP BY name HAVING COUNT(*) > 1')
        self.assertEqual(answer["rows"], [["同名", 2, 3, "001"]])
        self.assertEqual(answer["sources"], [info["receipt"]])
        self.tables.validate_delivery("读取了两条同名记录", [{
            "item": "读数", "status": "delivered", "receipts": [answer["receipt"]]}])

    def test_sql_cannot_read_outside_tables_or_change_rows(self):
        self.tables.add(path="example/list", params={}, rows=[{"id": 3}], total=1, complete=True)
        for statement in (
            "SELECT * FROM sqlite_schema",
            "SELECT load_extension('synthetic') FROM t1",
            "SELECT 1",
            "SELECT 1 FROM t1; DELETE FROM t1",
            "WITH q AS (SELECT id FROM t1) DELETE FROM t1 WHERE id IN q",
            "SELECT id AS password FROM t1",
        ):
            with self.subTest(statement=statement), self.assertRaises(TurnReadError):
                self.tables.query(statement)
        self.assertEqual(self.tables.query("WITH q AS (SELECT id FROM t1) SELECT * FROM q")["rows"], [[3]])

    def test_delivery_rejects_other_turn_and_sensitive_text(self):
        other = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(other.close)
        ref = other.issue_receipt()
        with self.assertRaisesRegex(TurnReadError, "不属于本轮"):
            self.tables.validate_delivery("值", [{"item": "读取", "status": "delivered", "receipts": [ref]}])
        with self.assertRaisesRegex(TurnReadError, "必须引用"):
            self.tables.validate_delivery("值", [{"item": "读取", "status": "delivered", "receipts": []}])
        with self.assertRaisesRegex(TurnReadError, "凭据"):
            self.tables.validate_delivery("password=very-secret-value", [{
                "item": "读取", "status": "undelivered", "receipts": []}])

    def test_empty_rows_can_be_counted_and_sql_results_are_bounded(self):
        self.tables.add(path="empty/page", params={}, rows=[], total=0, complete=True, published=("id",))
        self.assertEqual(self.tables.query("SELECT COUNT(id) FROM t1")["rows"], [[0]])
        self.tables.add(path="long/page", params={}, rows=[{"id": i} for i in range(220)], total=220, complete=True)
        result = self.tables.query("SELECT id FROM t2 ORDER BY id")
        self.assertEqual(result["returned_rows"], 200)
        self.assertTrue(result["truncated"])

    def test_api_read_records_actual_paged_rows_without_summary_output(self):
        from web_operation_agent_yudao.slim_loop import ActorKnowledge
        from web_operation_agent_yudao.tests.test_slim_loop import Client, DOCS

        knowledge = ActorKnowledge(Client(), lambda: DOCS)
        answer = knowledge.call_api("orders/page", {"pageNo": 1, "pageSize": 10},
                                    ["no", "status"], row_sink=self.tables.add)
        info = self.tables.describe()["tables"][0]
        self.assertIn(info["receipt"], answer)
        self.assertEqual((info["row_count"], info["total"], info["complete"]), (2, 2, True))
        self.assertEqual(self.tables.query("SELECT COUNT(*) FROM t1 WHERE no = 'S1'")["rows"], [[1]])
        self.assertNotIn("按 status 分组", answer)
        self.assertIsNone(knowledge.last_query_answer)

    def test_date_boundaries_use_actor_zone_and_iso_week(self):
        previous = date_range(today="2026-01-04", zone="Asia/Shanghai", unit="month", offset=-1)
        self.assertEqual((previous["start"], previous["end_exclusive"]), ("2025-12-01", "2026-01-01"))
        week = date_range(today="2026-01-04", zone="Asia/Shanghai", unit="week")
        self.assertEqual((week["start"], week["end_inclusive"]), ("2025-12-29", "2026-01-04"))
        self.assertEqual(week["end_exclusive_ms"] - week["start_ms"], 7 * 86400000)
        tuesday = date_range(today="2026-01-04", zone="Asia/Shanghai", unit="week", offset=1, weekday=2)
        self.assertEqual((tuesday["start"], tuesday["end_inclusive"]), ("2026-01-06", "2026-01-06"))
        elapsed = date_range(today="2026-01-04", zone="Asia/Shanghai", unit="year", elapsed=True)
        self.assertEqual((elapsed["start"], elapsed["end_inclusive"]), ("2026-01-01", "2026-01-04"))
