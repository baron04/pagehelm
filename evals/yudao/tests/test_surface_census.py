"""Unit tests for the 5-family surface census apparatus."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import signal
import tempfile
import unittest

from evals.yudao.surface_census import (
    FAMILIES,
    REASON_CODES,
    CellResult,
    Ledger,
)
from evals.yudao.surface_census.inventory import build_inventory, is_core_route
from evals.yudao.surface_census.oracle import IndependentApiOracle
from evals.yudao.surface_census.runner import EnvLeaseGuard
from evals.yudao.surface_census.templates import TurnExecution, evaluate_cell, make_scripted_turn
from evals.yudao.write_contract.fullstack import FullStack
from evals.yudao.write_contract.stack import Stack

CENSUS_DIR = Path(__file__).resolve().parents[1] / "surface_census"


class SurfaceCensusTests(unittest.TestCase):
    def test_core_route_scope_excludes_non_core_and_demo_routes(self):
        self.assertTrue(is_core_route("/crm/customer", "crm/customer"))
        self.assertTrue(is_core_route("/pms/project", "pms/project"))
        self.assertTrue(is_core_route("/pay/order", "pay/order"))
        self.assertTrue(is_core_route("/bpm/oa/leave", "bpm/oa-leave"))
        self.assertFalse(is_core_route("/infra/config", "infra/config"))
        self.assertFalse(is_core_route("/ai/chat", "ai/chat"))
        self.assertFalse(is_core_route("/im/message", "im/message"))
        self.assertFalse(is_core_route("/mp/account", "mp/account"))
        self.assertFalse(is_core_route("/pay/demo/order", "pay/demo-order"))
        self.assertFalse(is_core_route("/pay/demo", ""))

    def test_inventory_builds_from_menus_and_openapi_only(self):
        menus = [
            {"id": 1, "name": "核心", "path": "/crm", "component": "Layout", "children": [
                {"id": 2, "name": "客户", "path": "customer", "component": "crm/customer/index"},
            ]},
            {"id": 3, "name": "运维", "path": "/infra", "component": "Layout", "children": [
                {"id": 4, "name": "配置", "path": "config", "component": "infra/config/index"},
            ]},
            {"id": 5, "name": "演示", "path": "/pay", "component": "Layout", "children": [
                {"id": 6, "name": "示例", "path": "demo/order", "component": "pay/demo/order/index"},
            ]},
        ]
        docs = {
            "paths": {
                "/admin-api/crm/customer/page": {
                    "get": {
                        "tags": ["管理后台 - CRM 客户"],
                        "parameters": [{"name": "name", "in": "query", "required": False}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageWrap"}}}}},
                    }
                },
                "/admin-api/crm/customer/get": {
                    "get": {
                        "parameters": [{"name": "id", "in": "query", "required": True}],
                    }
                },
            },
            "components": {
                "schemas": {
                    "PageWrap": {"properties": {"data": {"$ref": "#/components/schemas/PageData"}}},
                    "PageData": {"properties": {"list": {"items": {"$ref": "#/components/schemas/Item"}}}},
                    "Item": {"properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string", "description": "名称"},
                        "status": {"type": "integer", "description": "状态"},
                        "amount": {"type": "number", "description": "金额"},
                    }},
                }
            },
        }
        inv = build_inventory(menus=menus, api_docs=docs)
        self.assertEqual([e["key"] for e in inv["core_entities"]], ["crm/customer"])
        self.assertEqual([p["route"] for p in inv["core_pages"]], ["/crm/customer"])
        self.assertEqual(len(inv["excluded_pages"]), 2)
        ent = inv["core_entities"][0]
        self.assertTrue(ent["has_get"])
        self.assertEqual(ent["name_field"], "name")
        self.assertIn("amount", ent["numeric_fields"])

    def test_ledger_reason_validation_summary_and_class_grouping(self):
        with self.assertRaises(ValueError):
            CellResult("c1", "find", "a/b", "crm", "find", "name", "display_name", "fail", reason_code="not_a_code")
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "ledger.jsonl")
            for i in range(3):
                ledger.append(CellResult(
                    f"detail:e{i}:1", "detail", f"mod/e{i}", "crm", "get", "status",
                    "coded_enum", "fail", reason_code="label_mismatch", elapsed_s=1.0,
                ))
            ledger.append(CellResult(
                "open_page:mod/x:menu", "open_page", "mod/x", "crm", "open", "/mod/x",
                "menu_route", "fail", reason_code="receipt_mismatch", elapsed_s=2.0,
            ))
            ledger.append(CellResult(
                "find:mod/x:1", "find", "mod/x", "crm", "find", "name",
                "display_name", "pass", elapsed_s=0.5,
            ))
            summary = ledger.summary()
            self.assertEqual(summary["total_cells"], 5)
            self.assertEqual(summary["pass"], 1)
            self.assertEqual(summary["fail"], 4)
            classes = summary["failure_classes"]
            self.assertEqual(len(classes), 2)
            self.assertTrue(classes[0]["qualifies_for_fix"])  # count == 3
            self.assertTrue(classes[1]["qualifies_for_fix"])  # security surface receipt_mismatch
            self.assertEqual(set(summary["by_family"].keys()), set(FAMILIES))

    def test_census_code_contains_no_entity_or_route_literals(self):
        forbidden = re.compile(r"/admin-api/(?:crm|erp|wms|fms|hrm|oa|bpm|mes|iot|product|trade|promotion|member|pay)/|crm/customer|erp/sale-order|hrm/employee")
        for path in sorted(CENSUS_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(forbidden.search(text), f"{path.name} must stay entity-agnostic")

    def test_aggregate_evaluation_catches_b2_defects_and_passes_valid_run(self):
        cell = {
            "cell_id": "aggregate:m/x:unfiltered",
            "family": "aggregate",
            "entity": "m/x",
            "domain": "crm",
            "operation": "aggregate_unfiltered",
            "field_or_control": "amt",
            "field_or_control_type": "large_table",
            "route": "/m/x",
            "list_path": "/admin-api/m/x/page",
            "paged": True,
            "params": {},
            "metric_col": "amt",
            "entity_label": "测试记录",
        }
        _prompt, _script, bag = make_scripted_turn(cell, today_str="2026-11-23")
        oracle = IndependentApiOracle(type("C", (), {
            "get": staticmethod(lambda path, **kw: {
                "total": 2,
                "list": [{"id": 1, "amt": 10}, {"id": 2, "amt": 10}],
            })
        })())

        # Case 1: valid aggregate with tie disclosure -> pass
        bag.update({
            "call_api_output": '本轮行表 t1\n{"name": "t1", "receipt": "ev_abc123def456_1", "row_count": 2, "total": 2, "complete": true}',
            "table_info": {"name": "t1", "receipt": "ev_abc123def456_1", "row_count": 2, "total": 2, "complete": True},
            "sql_agg_result": {"columns": ["cnt", "non_null_cnt", "total_val", "max_val", "min_val"], "rows": [[2, 2, 20, 10, 10]]},
            "sql_tie_output": "该极值对应的记录在本轮行表中共有 2 行同值并列",
        })
        exe = TurnExecution("p", "done", False, "http://localhost:48300/", [], [], [], [], 1, 1)
        res = evaluate_cell(cell, exe, bag, oracle=oracle, route_snapshot=None, elapsed_s=1.0)
        self.assertEqual(res.status, "pass")

        # Case 2: B2 pageSize rejected
        bag["call_api_output"] = "未执行：pageSize: maximum 200"
        res2 = evaluate_cell(cell, exe, bag, oracle=oracle, route_snapshot=None, elapsed_s=1.0)
        self.assertEqual(res2.status, "fail")
        self.assertEqual(res2.reason_code, "page_size_rejected")

        # Case 3: B2 incomplete read
        bag["call_api_output"] = "ok"
        bag["table_info"] = {"name": "t1", "row_count": 1, "total": 2, "complete": False}
        res3 = evaluate_cell(cell, exe, bag, oracle=oracle, route_snapshot=None, elapsed_s=1.0)
        self.assertEqual(res3.status, "fail")
        self.assertEqual(res3.reason_code, "incomplete_read")
        self.assertIn("incomplete_read", REASON_CODES)

    def test_env_lease_guard_enforces_and_releases_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "env-state.json"
            lock_file = root / "locks/env-state.lock"
            initial = {
                "version": 1,
                "environments": {
                    "1": {"status": "idle", "experiment": None, "snapshot": "restored-default", "fingerprint": "abc"},
                    "2": {"status": "occupied", "experiment": "other", "pid": 999, "token": "tok-other"},
                },
            }
            state_file.write_text(json.dumps(initial), encoding="utf-8")

            # Refuses occupied env2
            with self.assertRaisesRegex(RuntimeError, "environment_occupied: env2"):
                with EnvLeaseGuard((2,), state_file=state_file, lock_file=lock_file):
                    pass

            # Refuses caller lease mismatch without modifying env1 or env2
            with self.assertRaisesRegex(RuntimeError, "environment_lease_mismatch: env2"):
                with EnvLeaseGuard((2,), lease_token="wrong", state_file=state_file, lock_file=lock_file):
                    pass

            # Accepts matching caller lease token, refreshes pid, and preserves state on exit
            with EnvLeaseGuard((2,), lease_token="tok-other", state_file=state_file, lock_file=lock_file):
                during_match = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(during_match["environments"]["2"]["pid"], os.getpid())
            after_match = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(after_match["environments"]["2"]["status"], "occupied")
            self.assertEqual(after_match["environments"]["2"]["pid"], os.getpid())

            # Acquires idle env1, installs/restores signal handlers, and releases cleanly
            prev_term = signal.getsignal(signal.SIGTERM)
            with EnvLeaseGuard((1,), experiment="unit-test", state_file=state_file, lock_file=lock_file) as g:
                during = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(during["environments"]["1"]["status"], "occupied")
                self.assertEqual(during["environments"]["1"]["token"], g.active_token)
                self.assertEqual(during["environments"]["2"]["token"], "tok-other")
                self.assertIsNot(signal.getsignal(signal.SIGTERM), prev_term)
            self.assertIs(signal.getsignal(signal.SIGTERM), prev_term)
            released = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(released["environments"]["1"]["status"], "idle")
            self.assertEqual(released["environments"]["1"]["snapshot"], "restored-default")
            self.assertEqual(released["environments"]["1"]["fingerprint"], "abc")
            self.assertNotIn("token", released["environments"]["1"])
            self.assertEqual(released["environments"]["2"]["token"], "tok-other")

            # Simulated SIGTERM handler releases lease and restores prior handler
            with self.assertRaises(SystemExit) as cm:
                with EnvLeaseGuard((1,), experiment="sigterm-test", state_file=state_file, lock_file=lock_file):
                    handler = signal.getsignal(signal.SIGTERM)
                    handler(signal.SIGTERM, None)
            self.assertEqual(cm.exception.code, 128 + signal.SIGTERM)
            self.assertIs(signal.getsignal(signal.SIGTERM), prev_term)
            after_sig = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(after_sig["environments"]["1"]["status"], "idle")
            self.assertNotIn("token", after_sig["environments"]["1"])

            # Stack and FullStack refuse to start against an occupied env without matching lease_token
            stack = Stack(
                2, root / "s", root / "accounts.json",
                lease_state_file=state_file, lease_lock_file=lock_file,
            )
            with self.assertRaisesRegex(RuntimeError, "environment_occupied: env2"):
                stack.start()
            fstack = FullStack(
                2, root / "fs", root / "accounts.json", lambda _b: {"role": "assistant", "content": "ok"},
                lease_state_file=state_file, lease_lock_file=lock_file,
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "environment_occupied: env2"):
                    fstack.start()
            finally:
                fstack.model.close()

    def test_oracle_auto_refreshes_on_401(self):
        class ExpiredClient:
            @staticmethod
            def get(_rel, **_kw):
                raise RuntimeError("401 账号未登录")

        class FreshClient:
            @staticmethod
            def get(_rel, **_kw):
                return {"total": 1, "list": [{"id": 7, "amt": 42}]}

        calls = []

        def refresh():
            calls.append("refreshed")
            return FreshClient()

        oracle = IndependentApiOracle(ExpiredClient(), refresh_client=refresh)
        res = oracle.fetch_all("/admin-api/m/x/page", {}, paged=True)
        self.assertEqual(calls, ["refreshed"])
        self.assertEqual(res["error"], "")
        self.assertEqual(res["total"], 1)

    def test_inventory_excludes_fk_display_name_and_secret_numeric_and_partial_cache(self):
        menus = [
            {"id": 1, "name": "CRM", "path": "/crm", "component": "Layout", "children": [
                {"id": 2, "name": "回款计划", "path": "receivable-plan", "component": "crm/receivable/plan/index"},
            ]},
        ]
        docs = {
            "paths": {
                "/admin-api/crm/receivable-plan/page": {
                    "get": {
                        "parameters": [{"name": "customerId", "in": "query", "required": False, "schema": {"type": "integer"}}],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PageWrap"}}}}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "PageWrap": {"properties": {"data": {"$ref": "#/components/schemas/PageData"}}},
                    "PageData": {"properties": {"list": {"items": {"$ref": "#/components/schemas/PlanItem"}}}},
                    "PlanItem": {"properties": {
                        "id": {"type": "integer"},
                        "customerId": {"type": "integer"},
                        "customerName": {"type": "string"},
                        "accessTokenValiditySeconds": {"type": "integer"},
                        "price": {"type": "number"},
                    }},
                }
            },
        }
        inv = build_inventory(menus=menus, api_docs=docs)
        ent = inv["core_entities"][0]
        self.assertEqual(ent["name_field"], "")
        self.assertEqual(ent["query_param_types"], {"customerId": "integer"})
        self.assertNotIn("accessTokenValiditySeconds", ent["numeric_fields"])
        self.assertIn("price", ent["numeric_fields"])

        # Partial max_rows slice must not poison _list_cache for later full reads
        calls = []

        def fake_get(rel, **kw):
            calls.append((rel, kw))
            page = kw.get("pageNo", 1)
            return {"total": 2, "list": [{"id": page, "price": 10}]}

        oracle = IndependentApiOracle(type("C", (), {"get": staticmethod(fake_get)})())
        partial = oracle.fetch_all("/admin-api/crm/receivable-plan/page", {}, paged=True, max_rows=1)
        self.assertFalse(partial["complete"])
        full = oracle.fetch_all("/admin-api/crm/receivable-plan/page", {}, paged=True, max_rows=10)
        self.assertTrue(full["complete"])
        self.assertEqual(len(full["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
