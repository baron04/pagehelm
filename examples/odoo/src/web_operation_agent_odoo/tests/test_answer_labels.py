"""A Chinese answer names fields and scopes in Chinese (P7.5 CR-12).

The Odoo read tools describe their scopes and fields in English -- "customers visible to the
delegated user", `amount_total` -- because that is what the verifier checks against. Every
Chinese answer showed them as written: an English sentence in each source line, field names
as table headers, and a total's scope carrying query syntax ("state not in ('draft', 'sent',
'cancel'); currency_id eq USD"). J6.03 requires none of that in a Chinese conversation.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from web_operation_agent.business import BusinessToolRuntime
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.mutation import MemoryMutationJournal
from web_operation_agent.session import ServerAgentSession, TaskSpec
from web_operation_agent_odoo.adapter import ANSWER_LABELS_ZH, OdooApplicationAdapter
from web_operation_agent_odoo.tests.test_adapter import (
    BASE, ReadOnlyControl, _FakeOdoo, _key_file, _ProductRoutingBrowser, _ProductRoutingChat,
    _quotations)

QUOTATIONS = [("S00002", "draft"), ("S00003", "draft"), ("S00006", "sent")]


def _answer(prompt: str, script: list, rows=QUOTATIONS) -> tuple:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        adapter = OdooApplicationAdapter(
            base_url=BASE, api_key_path=_key_file(root), allow_mutations=True,
            mutation_journal=MemoryMutationJournal())
        adapter.business_tools._executor = _FakeOdoo({"sale.order": _quotations(rows)},
                                                     allow_mutations=True)
        now = int(time.time() * 1000)
        principal = PrincipalContext(
            schema_version=1, application_id="odoo", tenant_id="trial", workspace_id="7",
            actor_id="42", delegation_id="p75-cr12", issued_at=now - 1_000,
            expires_at=now + 300_000, scopes=tuple(sorted(adapter.ports.identity.required_scopes)))
        browser = _ProductRoutingBrowser()
        ledger = root / "turn.jsonl"
        session = ServerAgentSession(
            task=TaskSpec(task_ref="cr12", prompt=prompt, entry_path="/odoo/customers",
                          supported_scope="Read current Odoo records."),
            chat=_ProductRoutingChat(script), browser=browser, control=ReadOnlyControl(),
            ledger=SessionLedger(ledger, "cr12"), business_tools=adapter.business_tools,
            business_mutation_approval=lambda *_a, **_k: None,
            application_routes=adapter.ports.routes, principal=principal,
            session_id="cr12-session", turn_id="cr12-turn", allow_clarification=True,
            disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
        session.entry_observation = browser.observe()
        session.plan()
        result = session.run()
        return result, [json.loads(line) for line in ledger.read_text().splitlines()]


#: The Skill the session offers for these questions: "未确认报价单" selects the confirm Skill,
#: whose read tool the model uses, as in the dry run (see test_read_intent).
_LIST = [("load_skill", {"skill_id": "odoo.sales.sale-orders-confirm"}),
         ("query_quotations", {"limit": 50}),
         ("finish", {"outcome": "answered", "answer": {
             "version": 1, "coverage": "complete", "presentation": "table",
             "conclusions": [{"kind": "selection", "evidence_ref": "call2-query#1",
                              "fields": ["name", "amount_total", "state"]}]}})]


class AChineseAnswerSpeaksChinese(unittest.TestCase):
    def test_a_list_has_chinese_headers_and_scope(self):
        result, _ = _answer("当前有哪些未确认报价单？列出单号与金额。", _LIST)
        text = result.agent_said
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("| 名称 | 含税总额 | 状态 |", text)
        self.assertIn("| S00006 | 504.00 USD | 已发送 |", text)
        self.assertIn("| S00002 | 502.00 USD | 草稿 |", text)
        self.assertIn("范围：未确认、未取消的报价单", text)
        for english in ("open quotations", "amount\\_total", "| name |"):
            self.assertNotIn(english, text)

    def test_the_answer_carries_only_the_labels_it_used(self):
        result, _ = _answer("当前有哪些未确认报价单？", _LIST)
        labels = result.verified_answer["labels"]["zh"]
        self.assertEqual({key for key in labels if "=" not in key},
                         {"name", "amount_total", "state",
                          "open quotations, neither confirmed nor cancelled"})
        self.assertEqual({key.partition("=")[0] for key in labels if "=" in key}, {"state"})

    def test_a_value_is_labelled_only_in_its_own_field(self):
        from web_operation_agent.answer_view import _value_label
        words = {"labels": {"state=sale": "已确认"}}
        self.assertEqual(_value_label(words, "state", "sale"), "已确认")
        self.assertEqual(_value_label(words, "name", "sale"), "sale")

    def test_an_english_question_keeps_the_applications_own_words(self):
        result, _ = _answer("Which quotations are open?",
                            [("load_skill", {"skill_id": "odoo.sales.quotations-query"}),
                             *_LIST[1:]])
        self.assertIn("| name | amount\\_total | state |", result.agent_said)
        self.assertIn("open quotations, neither confirmed nor cancelled", result.agent_said)

    def test_a_filtered_total_names_its_filter_and_hides_the_query(self):
        script = [("load_skill", {"skill_id": "odoo.sales.sale-orders-confirm"}),
                  ("query_quotations", {"limit": 50}),
                  ("compute", {"operation": "filter", "dataset_ref": "call2-query#1",
                               "field": "state", "comparison": "eq", "threshold": "sent"}),
                  ("compute", {"operation": "sum", "dataset_ref": "calc#3",
                               "field": "amount_total"}),
                  ("finish", {"outcome": "answered", "answer": {
                      "version": 1, "coverage": "complete",
                      "conclusions": [{"kind": "aggregate", "evidence_ref": "calc#4"}]}})]
        result, rows = _answer("未确认报价单里已发送的合计多少钱？", script)
        text = result.agent_said.replace("**", "")
        self.assertEqual([row.get("detail") for row in rows
                          if row.get("event") == "answer_contract_refused"], [])
        self.assertIn("合计（筛选：状态 = 已发送）：504.00 USD", text)
        self.assertIn("范围：未确认、未取消的报价单", text)
        for internal in ("state in", "draft", "open quotations"):
            self.assertNotIn(internal, text)


class TheLabelTableIsBounded(unittest.TestCase):
    def test_a_malformed_table_is_refused(self):
        from web_operation_agent.business import _answer_labels
        for tables in ({"chinese": {"a": "b"}}, {"zh": {"a": ""}}, {"zh": {"a": "b\nc"}},
                       {"zh": ["a"]}):
            with self.subTest(tables=tables), self.assertRaises(ValueError):
                _answer_labels(tables)
        self.assertEqual(_answer_labels({"zh": {"a": "甲"}}), {"zh": {"a": "甲"}})

    def test_the_odoo_table_covers_every_field_and_scope_its_reads_report(self):
        from web_operation_agent_odoo.adapter import COLLECTIONS
        for collection in COLLECTIONS:
            with self.subTest(tool=collection.tool):
                self.assertIn(collection.scope, ANSWER_LABELS_ZH)
                for field in collection.fields:
                    self.assertIn(field, ANSWER_LABELS_ZH)
        self.assertIsInstance(BusinessToolRuntime, type)


if __name__ == "__main__":
    unittest.main()


class ARefusalNamesWhatWasRefused(unittest.TestCase):
    """"当前账号没有执行这项操作的权限" left the reader guessing which half of "the phone and
    the orders" the account could not read (P7.5 CR-24)."""

    def test_every_tool_the_adapter_ships_has_a_refusal_label(self):
        from web_operation_agent_odoo.adapter import (CONFIRM_QUOTATION_TOOL,
                                                      CREATE_CUSTOMER_TOOL, COLLECTIONS,
                                                      INVOICE_SALE_ORDER_TOOL,
                                                      PREPARE_CUSTOMER_FORM_TOOL)
        for tool in ([collection.tool for collection in COLLECTIONS]
                     + [CREATE_CUSTOMER_TOOL, CONFIRM_QUOTATION_TOOL,
                        INVOICE_SALE_ORDER_TOOL, PREPARE_CUSTOMER_FORM_TOOL]):
            with self.subTest(tool=tool):
                self.assertIn(tool, ANSWER_LABELS_ZH)

    def test_a_refused_read_is_named_in_the_answer(self):
        from web_operation_agent.product import AgentAnswerContext, AgentTurnMessage, RunResult
        from web_operation_agent.product import render_neutral_user_answer
        from web_operation_agent.tests.test_product import _message
        answer = render_neutral_user_answer(AgentAnswerContext(
            message=AgentTurnMessage.from_wire({**_message(), "text": "它有哪些销售订单？"}),
            agent=RunResult(run_id="run", task_ref="task", arm="U",
                            terminal_reason="permission_denied",
                            refused_operation="读取销售订单"),
            business_result=None, max_chars=4_000))
        self.assertIn("当前账号没有读取销售订单的权限", answer)
        self.assertIn("可以单独再问一次", answer)


class ASessionCarriesTheRefusedOperation(unittest.TestCase):
    def test_an_application_refusal_names_the_read_it_refused(self):
        from web_operation_agent.business import BusinessToolPermissionError

        class Refusing:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def execute(self, name, args, context=None):
                if name == "query_sale_orders":
                    raise BusinessToolPermissionError(
                        "Odoo's access rules do not permit the delegated user this operation",
                        layer="application")
                return self._inner.execute(name, args, context)

        script = [("load_skill", {"skill_id": "odoo.sales.sale-orders-query"}),
                  ("query_sale_orders", {"limit": 50})]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OdooApplicationAdapter(base_url=BASE, api_key_path=_key_file(root))
            adapter.business_tools._executor = Refusing(
                _FakeOdoo({"sale.order": _quotations(QUOTATIONS)}))
            now = int(time.time() * 1000)
            principal = PrincipalContext(
                schema_version=1, application_id="odoo", tenant_id="trial", workspace_id="7",
                actor_id="42", delegation_id="p75-cr24", issued_at=now - 1_000,
                expires_at=now + 300_000,
                scopes=tuple(sorted(adapter.ports.identity.required_scopes)))
            browser = _ProductRoutingBrowser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="cr24", prompt="它有哪些销售订单？",
                              entry_path="/odoo/customers",
                              supported_scope="Read current Odoo records."),
                chat=_ProductRoutingChat(script), browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(root / "turn.jsonl", "cr24"),
                business_tools=adapter.business_tools,
                application_routes=adapter.ports.routes, principal=principal,
                session_id="cr24-session", turn_id="cr24-turn",
                disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
            session.entry_observation = browser.observe()
            session.plan()
            result = session.run()

        self.assertEqual(result.terminal_reason, "permission_denied")
        self.assertEqual(result.refused_operation, "读取销售订单")
        self.assertIn("refused_operation", result.to_json())
