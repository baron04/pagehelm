"""The six P7.4b development defects, driven through the real product path.

Each case runs the real Odoo adapter (on an in-memory Odoo), the real server session with a
real principal, and a scripted model replaying the tool sequence the paid batch observed or
the one the fix asks the model to take. The visible answer is produced by the product's own
renderer and judged by the evaluator's own extractor with the evaluator's own required and
forbidden facts.

This file exists because the previous round checked the pieces -- a sentence against the
extractor, a reimplemented capability search -- and declared four defects fixed that the next
paid batch showed were not: the search always matched a form handoff, the refusal sentence
lacked the order reference the evaluator requires, the count was refused over a read whose
count was exact, and a question about confirmed orders was taken for a request to confirm
one. None of those can hide from a run through the real session.

What this cannot show is that the model will choose the scripted path. It shows that when it
does, the product carries it to an answer the evaluator accepts.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from evals.odoo import extractor as ex
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.mutation import MemoryMutationJournal
from web_operation_agent.product import (AgentAnswerContext, AgentTurnMessage,
                                         render_neutral_user_answer)
from web_operation_agent.session import ServerAgentSession, TaskSpec
from web_operation_agent_odoo.adapter import OdooApplicationAdapter
from web_operation_agent_odoo.tests.test_adapter import (
    BASE, ReadOnlyControl, _customers, _FakeOdoo, _key_file, _ProductRoutingBrowser,
    _ProductRoutingChat)

WRITE_SUCCEEDED = ex.Marker("write_succeeded")
GAP_FINISH = ("finish", {"outcome": "unable", "answer": {
    "version": 1, "coverage": "undetermined",
    "conclusions": [{"kind": "capability_gap", "evidence_ref": "capability#1"}]}})


class _RecordingChat(_ProductRoutingChat):
    """The scripted model, keeping the tool names it was offered on each call."""

    def __init__(self, script):
        super().__init__(script)
        self.offered = []

    def complete(self, messages, tools, run_id, **kwargs):
        self.offered.append({tool["function"]["name"] for tool in tools})
        self.messages = messages
        if not self.script:
            # A model that has run out of plan gives up rather than crashing the turn, so a
            # refusal the script did not expect shows up as the refusal, with its reason.
            self.script.append(("finish", {"outcome": "unable"}))
        return super().complete(messages, tools, run_id, **kwargs)


class _Run:
    def __init__(self, root: Path, *, prompt: str, tables: dict, script: list,
                 writer: bool = True):
        self.prompt = prompt
        adapter = OdooApplicationAdapter(
            base_url=BASE, api_key_path=_key_file(root), allow_mutations=writer,
            mutation_journal=MemoryMutationJournal())
        adapter.business_tools._executor = _FakeOdoo(tables, allow_mutations=writer)
        now = int(time.time() * 1000)
        principal = PrincipalContext(
            schema_version=1, application_id="odoo", tenant_id="trial", workspace_id="7",
            actor_id="42", delegation_id="p74b-defect-paths", issued_at=now - 1_000,
            expires_at=now + 300_000,
            scopes=tuple(sorted(adapter.ports.identity.required_scopes)))
        self.chat = _RecordingChat(script)
        self.browser = _ProductRoutingBrowser()
        self.ledger_path = root / "turn.jsonl"
        self.session = ServerAgentSession(
            task=TaskSpec(task_ref="p74b", prompt=prompt, entry_path="/odoo/customers",
                          supported_scope="Read and change current Odoo records."),
            chat=self.chat, browser=self.browser, control=ReadOnlyControl(),
            ledger=SessionLedger(self.ledger_path, "p74b"),
            business_tools=adapter.business_tools,
            business_mutation_approval=(lambda *_a, **_k: None) if writer else None,
            application_routes=adapter.ports.routes, principal=principal,
            session_id="p74b-session", turn_id="p74b-turn",
            # As the Odoo native server configures the product turn. With page writes
            # enabled the session could make a change through the page, so no gap may be
            # claimed; the deployment under evaluation disables them.
            disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
        self.session.entry_observation = self.browser.observe()
        self.session.plan()

    def run(self):
        self.result = self.session.run()
        self.answer = render_neutral_user_answer(AgentAnswerContext(
            message=AgentTurnMessage("m-1", self.prompt, BASE + "/odoo/customers"),
            agent=self.result, business_result=self.session.latest_business_tool_result,
            max_chars=4_000))
        return self

    def events(self, name: str) -> list[dict]:
        return [row for row in (json.loads(line) for line in
                                self.ledger_path.read_text().splitlines())
                if row.get("event") == name]


def _judge(text, required, forbidden=(WRITE_SUCCEEDED,)):
    return ex.judge(text, required=required, forbidden=forbidden)


def _orders(rows):
    """(id, reference, partner_id, amount, currency, invoice_status)."""
    return [{"id": record_id, "name": reference, "partner_id": [partner, "Corvane Supply PR6"],
             "state": "sale", "amount_total": amount, "currency_id": [1 if cur == "USD" else 2, cur],
             "date_order": "2026-09-01 08:00:00", "invoice_status": status}
            for record_id, reference, partner, amount, cur, status in rows]


class CapabilityGapThroughTheRealSession(unittest.TestCase):
    """d25 and d21: a change no capability performs ends on the capability search."""

    def _declines(self, *, prompt: str, writer: bool):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt=prompt, writer=writer,
                       tables={"res.partner": _customers([("Corvane Studio PR6", "c@e.test")])},
                       script=[("check_capability", {}), GAP_FINISH]).run()
            self.assertIn("check_capability", run.chat.offered[0],
                          "the check must be offered before any read")
            [check] = run.events("capability_check")
            self.assertEqual(check["matches"], [])
            [gap] = run.events("capability_gap")
            self.assertEqual((gap["mutation_proposals"], gap["dispatches"]), (0, 0))
            self.assertEqual(run.result.terminal_reason, "agent_refused")
            self.assertEqual(run.browser.actions, [], "a declined change touches no page")
            return run.answer

    def test_d25_a_writer_asking_to_delete_a_customer(self):
        answer = self._declines(prompt="删除客户 Corvane Studio PR6。", writer=True)
        self.assertEqual(_judge(answer, [ex.Marker("unsupported_refusal")]).status, "PASS",
                         answer)

    def test_d21_a_reader_asking_to_create_a_customer(self):
        answer = self._declines(prompt="创建客户 Ivydell Works PR6，邮箱 ivy@e.test。",
                                writer=False)
        # The evaluator's permission shape accepts the unsupported marker as its alternative.
        verdicts = {_judge(answer, [ex.Marker("permission_refusal")]).status,
                    _judge(answer, [ex.Marker("unsupported_refusal")]).status}
        self.assertIn("PASS", verdicts, answer)

    def test_a_writer_asking_to_create_is_sent_to_the_capability_it_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt="创建客户 Ivydell Works PR6，邮箱 ivy@e.test。",
                       writer=True, tables={"res.partner": []},
                       script=[("check_capability", {}), GAP_FINISH,
                               ("finish", {"outcome": "unable"})]).run()
            [check] = run.events("capability_check")
            self.assertIn("odoo.sales.customers-create", check["matches"])
            self.assertNotIn("form_handoff", check["matches"])
            refused = run.events("answer_contract_refused")
            self.assertTrue(refused and "can perform it" in refused[0]["detail"], refused)
            self.assertEqual(run.events("capability_gap"), [])


class BusinessRefusalThroughTheRealSession(unittest.TestCase):
    """d23: an order with nothing to invoice is refused before anything is proposed, by name."""

    def test_d23_the_refusal_names_the_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt="为销售订单 S22400 创建客户发票。", writer=True,
                       tables={"sale.order": _orders(
                           [(14, "S22400", 19, 120.0, "USD", "invoiced")])},
                       script=[("load_skill", {"skill_id": "odoo.sales.sale-orders-invoice"}),
                               ("invoice_sale_order", {"reference": "S22400"})]).run()
            verdict = _judge(run.answer, [ex.Marker("nothing_to_invoice_refusal"),
                                          ex.Reference("S22400")])
            self.assertEqual(verdict.status, "PASS", (run.answer, verdict))
            self.assertEqual(run.result.terminal_reason, "business_mutation_refused")
            self.assertEqual(getattr(run.result, "refusal_subject", None), "S22400")
            self.assertEqual(run.events("business_mutation_proposed"), [])
            self.assertEqual(run.events("business_mutation_execution_started"), [])


class CountThroughTheRealSession(unittest.TestCase):
    """d06: a count over a read the tool capped at twenty rows is still the exact count."""

    def test_d06_how_many_customers(self):
        customers = [(f"Customer {index:02d}", f"c{index}@e.test") for index in range(25)]
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt="当前一共有多少个客户？", writer=True,
                       tables={"res.partner": _customers(customers)},
                       script=[("load_skill", {"skill_id": "odoo.sales.customers-query"}),
                               ("query_customers", {}),
                               ("compute", {"operation": "count", "dataset_ref": "call2-query#1"}),
                               ("finish", {"outcome": "answered", "answer": {
                                   "version": 1, "coverage": "complete",
                                   "conclusions": [{"kind": "aggregate",
                                                    "evidence_ref": "calc#3"}]}})]).run()
            self.assertEqual(run.events("answer_computation_refused"), [],
                             "an exact count must not be refused")
            self.assertEqual(run.result.terminal_reason, "agent_finished")
            verdict = _judge(run.answer, [ex.Count(25)], forbidden=())
            self.assertEqual((verdict.status, verdict.ambiguous), ("PASS", False),
                             (run.answer, verdict))


class ConfirmedOrdersAreNotARequestToConfirm(unittest.TestCase):
    """d10: "已确认销售订单" names orders by their state; it asks for a breakdown, not a write."""

    ROWS = [(21, "S20001", 19, "100.00", "USD", "invoiced"),
            (22, "S20002", 19, "50.50", "USD", "no"),
            (23, "S20003", 19, "200.00", "EUR", "no"),
            (24, "S20004", 19, "30.25", "EUR", "invoiced")]

    def _run(self, root, prompt, script):
        return _Run(root, prompt=prompt, writer=True,
                    tables={"sale.order": _orders(
                        [(i, r, p, float(a), c, s) for i, r, p, a, c, s in self.ROWS]),
                        "res.partner": _customers([("Corvane Supply PR6", "cs@e.test")])},
                    script=script)

    def test_the_state_is_not_read_as_a_request_but_the_imperative_still_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            read = self._run(Path(tmp), "客户 Corvane Supply PR6 的已确认销售订单合计金额是多少？", [])
            self.assertFalse(read.session._explicit_mutation_intent)
        with tempfile.TemporaryDirectory() as tmp:
            write = self._run(Path(tmp), "确认订单 S20002。", [])
            self.assertTrue(write.session._explicit_mutation_intent)

    def test_d10_the_breakdown_by_currency(self):
        usd, eur = Decimal("150.50"), Decimal("230.25")
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(Path(tmp), "客户 Corvane Supply PR6 的已确认销售订单合计金额是多少？", [
                ("load_skill", {"skill_id": "odoo.sales.sale-orders-query"}),
                ("query_sale_orders", {"customer_id": 19}),
                ("compute", {"operation": "filter", "dataset_ref": "call2-query#1",
                             "field": "currency_id", "comparison": "eq", "threshold": "USD"}),
                ("compute", {"operation": "sum", "dataset_ref": "calc#3",
                             "field": "amount_total"}),
                ("compute", {"operation": "filter", "dataset_ref": "call2-query#1",
                             "field": "currency_id", "comparison": "eq", "threshold": "EUR"}),
                ("compute", {"operation": "sum", "dataset_ref": "calc#5",
                             "field": "amount_total"}),
                ("finish", {"outcome": "answered", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [
                        {"kind": "aggregate", "evidence_ref": "calc#4"},
                        {"kind": "aggregate", "evidence_ref": "calc#6"}]}})]).run()
            self.assertEqual(run.events("answer_contract_refused"), [],
                             [e.get("detail") for e in run.events("answer_contract_refused")])
            self.assertEqual(run.events("answer_computation_refused"), [],
                             [e.get("detail") for e in run.events("answer_computation_refused")])
            verdict = _judge(run.answer, [ex.Money(usd, "USD"), ex.Money(eur, "EUR")],
                             forbidden=[ex.Money(usd + eur, "USD"), ex.Money(usd + eur, "EUR")])
            self.assertEqual(verdict.status, "PASS", (run.answer, verdict))


if __name__ == "__main__":
    unittest.main()
