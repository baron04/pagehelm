import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent_yudao.reads.turn_tables import TurnReadError, TurnTables, date_range


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
        from web_operation_agent_yudao.executor.slim_loop import ActorKnowledge
        from web_operation_agent_yudao.tests.slim_loop_support import Client, DOCS

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

    def test_page_delivery_is_checked_against_the_page_not_a_quoted_receipt(self):
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
        from web_operation_agent_yudao.executor.slim_loop import TableReadTurnExecutor

        executor = object.__new__(TableReadTurnExecutor)
        observation = SimpleNamespace(url="https://synthetic.invalid/list", to_json=lambda: {
            "url": "https://synthetic.invalid/list", "epoch": 1, "content": "Synthetic row", "elements": []})
        read_reference = "ev_synthetic_read"
        self.tables.receipts.add(read_reference)
        state = SimpleNamespace(obs=observation, ledger=Mock(
            of_kind=lambda event: [{"tool": "call_api", "reference": read_reference}]))
        executor._tool_receipts = [SimpleNamespace(action="goto", ok=True, observation=observation,
                                                   command_id="synthetic-command")]
        executor.turn_context = SlimTurnContext(turn_tables=(self.tables, "2026-10-25", "Asia/Shanghai"))
        turn = {}
        result = LoopToolResult("已打开", kind="page")
        self.assertFalse(executor._completion_proved(state, "goto", {"answer": "已打开"}, result))
        feedback = executor._tool_feedback(state, turn, "goto", result, result.feedback)
        reference = next(iter(turn["page_delivery_receipts"]))
        self.assertIn(reference, feedback)
        # A current page receipt is sufficient; a separate API read is optional.
        args = {"answer": "已打开", "items": [
            {"item": "用户页", "kind": "page", "status": "delivered",
             "receipts": [reference, read_reference]}]}
        self.assertEqual(executor._finish_feedback(state, turn, args), "")
        # Page-only delivery does not acquire an extra API-read obligation.
        args["items"][0]["receipts"] = [reference]
        self.assertEqual(executor._finish_feedback(state, turn, args), "")
        args["items"][0]["receipts"] = [reference, read_reference]

        # No receipt has to be quoted for a page: a data receipt, none at all, or one for an
        # earlier state of the page all pass while the page itself is where it should be.
        data_reference = self.tables.issue_receipt()
        for quoted in ([data_reference], [], [reference, read_reference]):
            args["items"][0]["receipts"] = quoted
            self.assertEqual(executor._finish_feedback(state, turn, args), "")
        args["items"][0]["receipts"] = [reference]
        state.obs = SimpleNamespace(url="https://synthetic.invalid/other", page_route="/other", elements=[],
            to_json=lambda: {"url": "https://synthetic.invalid/other", "epoch": 2, "content": "Other row", "elements": []})
        self.assertEqual(executor._finish_feedback(state, turn, args), "")
        # What is still checked: the user's page is not the home page.
        state.obs = SimpleNamespace(url="https://synthetic.invalid/", page_route="/", elements=[],
            to_json=lambda: {"url": "https://synthetic.invalid/", "epoch": 3, "content": "Home", "elements": []})
        self.assertEqual(executor._finish_feedback(state, turn, args).code, "delivery_page_home")
        # And a handoff the answer claims must still be on the page.
        state.obs = SimpleNamespace(url="https://synthetic.invalid/form", page_route="/form", elements=[],
            to_json=lambda: {"url": "https://synthetic.invalid/form", "epoch": 4, "content": "Form", "elements": []})
        turn["handoff"] = ("a-page-state-that-is-gone", None)
        args["answer"] = "表单已填好并交接，提交按钮已标出。"
        self.assertEqual(executor._finish_feedback(state, turn, args).code, "delivery_page_handoff")
        args["answer"] = "已打开"
        self.assertEqual(executor._finish_feedback(state, turn, args), "")

    def test_unobserved_page_result_cannot_issue_a_page_state_receipt(self):
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
        from web_operation_agent_yudao.executor.slim_loop import TableReadTurnExecutor

        executor = object.__new__(TableReadTurnExecutor)
        executor._tool_receipts = []
        executor.turn_context = SlimTurnContext(turn_tables=(self.tables, "2026-10-25", "Asia/Shanghai"))
        state = SimpleNamespace(ledger=Mock())
        turn = {}
        executor._tool_feedback(state, turn, "observe", LoopToolResult("缺少观察", kind="page"), "缺少观察")
        self.assertNotIn("page_delivery_receipts", turn)

    def test_current_dialog_can_be_the_delivery_object_without_allowing_a_residual_mask(self):
        from web_operation_agent.schemas import ElementRef, PageObservation
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
        from web_operation_agent_yudao.executor.slim_loop import TableReadTurnExecutor
        executor=object.__new__(TableReadTurnExecutor)
        observation=PageObservation(1,2,"https://synthetic.invalid/list","List",[],[
            ElementRef("e1","dialog","Details","div",2,{"form_scope":"dialog:details"})],
            content="Details",render_state="ready",page_route="/list")
        state=SimpleNamespace(obs=observation,ledger=Mock())
        executor._tool_receipts=[SimpleNamespace(ok=True,observation=observation,command_id="opened")]
        executor.turn_context=SlimTurnContext(turn_tables=(self.tables,"2026-11-14","Asia/Shanghai"))
        turn={}
        executor._tool_feedback(state,turn,"open_record",LoopToolResult("opened",kind="page"),"opened")
        reference=next(iter(turn["page_delivery_receipts"]))
        args={"status":"done","answer":"Details are open.","items":[{
            "item":"Details dialog","kind":"page","status":"delivered","receipts":[reference]}]}
        self.assertEqual(executor._finish_feedback(state,turn,args).code,"delivery_page_masked")
        args["items"][0]["container"]="dialog"
        self.assertEqual(executor._finish_feedback(state,turn,args),"")

    def test_like_glob_and_match_operators_run_inside_the_sandbox(self):
        self.tables.add(path="names", params={}, rows=[
            {"name": "Alice"}, {"name": "Bob"}, {"name": "Alicia"}], total=3, complete=True)
        self.assertEqual(
            self.tables.query("SELECT name FROM t1 WHERE name LIKE 'A%'")["rows"],
            [["Alice"], ["Alicia"]])
        self.assertEqual(
            self.tables.query("SELECT name FROM t1 WHERE name GLOB 'A*'")["rows"],
            [["Alice"], ["Alicia"]])

    def test_small_complete_api_reads_inline_all_rows_while_large_or_partial_reads_stay_sampled(self):
        from web_operation_agent_yudao.executor.slim_loop import ActorKnowledge
        from web_operation_agent_yudao.tests.slim_loop_support import Client, DOCS

        knowledge = ActorKnowledge(Client(), lambda: DOCS)
        small = knowledge.call_api("orders/page", {"pageNo": 1, "pageSize": 10},
                                   ["no", "status"], row_sink=self.tables.add)
        self.assertIn("全部行（共 2 行，已读完所有页", small)
        self.assertNotIn("样例（不是全表）", small)

        many_client = Client()
        many_rows = [{"id": i, "no": f"S{i}", "status": 1} for i in range(1, 36)]
        orig = many_client.get
        many_client.get = lambda path, **params: (
            {"list": many_rows, "total": 35} if path == "orders/page" else orig(path, **params))
        large = ActorKnowledge(many_client, lambda: DOCS).call_api(
            "orders/page", {"pageNo": 1, "pageSize": 50}, ["no", "status"], row_sink=self.tables.add)
        self.assertIn("样例（不是全表）", large)
        self.assertNotIn("全部行（共", large)

    def test_scalar_min_and_max_queries_report_tied_extrema(self):
        self.tables.add(path="scores/page", params={}, rows=[
            {"id": 1, "name": "A", "duration": 180},
            {"id": 2, "name": "B", "duration": 180},
            {"id": 3, "name": "C", "duration": 90},
        ], total=3, complete=True)
        tied = self.tables.query("SELECT MAX(duration) FROM t1")
        self.assertEqual(tied["rows"], [[180]])
        self.assertIn("共有 2 行同值并列", tied["tie_note"])
        self.assertNotIn("t1", tied["tie_note"])
        qualified = self.tables.query("SELECT MAX(a.duration) AS max_d FROM t1 a WHERE a.id >= 1")
        self.assertIn("共有 2 行同值并列", qualified["tie_note"])
        self.assertNotIn("t1", qualified["tie_note"])
        unique = self.tables.query("SELECT MIN(duration) FROM t1")
        self.assertEqual(unique["rows"], [[90]])
        self.assertNotIn("tie_note", unique)
        aliased_limit = self.tables.query("SELECT MAX(duration) max_d FROM t1 LIMIT 1")
        self.assertIn("共有 2 行同值并列", aliased_limit["tie_note"])

    def test_a_quoted_alias_cannot_inject_sql_into_the_tie_probe(self):
        self.tables.add(path="items", params={}, rows=[
            {"v": i} for i in range(1, 8)], total=7, complete=True)
        # `x LIMIT 0--` as an output alias must stay an identifier: the probe
        # re-quotes it, so the boundary is checked, not silently "verified".
        result = self.tables.query(
            'SELECT v AS "x LIMIT 0--" FROM t1 ORDER BY "x LIMIT 0--" LIMIT 3')
        self.assertIn("tie", result)
        self.assertTrue(result["sources_complete"])

    def test_blob_cells_serialize_instead_of_crashing_the_answer(self):
        self.tables.add(path="blobs", params={}, rows=[{"raw": b"\x00\xff"}], total=1, complete=True)
        result = self.tables.query("SELECT raw FROM t1")
        self.assertEqual(result["rows"], [["00ff"]])

    def test_table_names_resolve_case_insensitively(self):
        self.tables.add(path="ci", params={}, rows=[{"id": 1}], total=1, complete=True)
        self.assertEqual(self.tables.query("SELECT id FROM T1")["rows"], [[1]])
