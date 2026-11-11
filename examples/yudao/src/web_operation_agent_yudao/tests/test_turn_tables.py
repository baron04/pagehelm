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

    def test_current_occurrences_end_today_and_plans_cover_the_period(self):
        for unit, begin, end in (
            ("week", "2026-10-19", "2026-10-25"),
            ("month", "2026-10-01", "2026-10-31"),
            ("quarter", "2026-10-01", "2026-12-31"),
            ("year", "2026-01-01", "2026-12-31"),
        ):
            with self.subTest(unit=unit):
                occurred = date_range(today="2026-10-21", zone="Asia/Shanghai", unit=unit)
                planned = date_range(today="2026-10-21", zone="Asia/Shanghai", unit=unit, scope="planned")
                self.assertEqual((occurred["start"], occurred["end_inclusive"]), (begin, "2026-10-21"))
                self.assertEqual((planned["start"], planned["end_inclusive"]), (begin, end))
        full = date_range(today="2026-10-21", zone="Asia/Shanghai", unit="month", elapsed=False)
        self.assertEqual(full["end_inclusive"], "2026-10-31")
        tomorrow = date_range(today="2026-10-21", zone="Asia/Shanghai", unit="day", offset=1)
        self.assertEqual(tomorrow["start"], "2026-10-22")

    def test_rolling_days_include_today_and_differ_from_previous_calendar_month(self):
        last_thirty = date_range(today="2026-10-25", zone="Asia/Shanghai", unit="rolling_days", days=30)
        previous_month = date_range(today="2026-10-25", zone="Asia/Shanghai", unit="month", offset=-1)
        self.assertEqual((last_thirty["start"], last_thirty["end_inclusive"]), ("2026-09-26", "2026-10-25"))
        self.assertEqual((previous_month["start"], previous_month["end_inclusive"]), ("2026-09-01", "2026-09-30"))
        for today, days, begin in (
            ("2026-01-01", 1, "2026-01-01"),
            ("2026-01-01", 2, "2025-12-31"),
            ("2024-03-01", 2, "2024-02-29"),
        ):
            with self.subTest(today=today, days=days):
                result = date_range(today=today, zone="Asia/Shanghai", unit="rolling_days", days=days)
                self.assertEqual((result["start"], result["end_inclusive"]), (begin, today))
        dst = date_range(today="2026-03-08", zone="America/New_York", unit="rolling_days", days=2)
        self.assertEqual(dst["end_exclusive_ms"] - dst["start_ms"], 47 * 3600000)

    def test_explicit_range_has_priority_over_elapsed_default(self):
        result = date_range(today="2026-10-25", zone="Asia/Shanghai", unit="range",
                            start_date="2026-10-01", end_date="2026-10-31", elapsed=True)
        self.assertEqual((result["start"], result["end_inclusive"], result["end_exclusive"]),
                         ("2026-10-01", "2026-10-31", "2026-11-01"))
        for args in (
            {"unit": "rolling_days", "days": 0},
            {"unit": "rolling_days", "days": True},
            {"unit": "rolling_days", "days": 30, "offset": 1},
            {"unit": "month", "days": 30},
            {"unit": "range", "start_date": "2026-10-31", "end_date": "2026-10-01"},
            {"unit": "range", "start_date": "2026-10-01"},
            {"unit": "week", "scope": "unknown"},
        ):
            with self.subTest(args=args), self.assertRaises(TurnReadError):
                date_range(today="2026-10-25", zone="Asia/Shanghai", **args)
