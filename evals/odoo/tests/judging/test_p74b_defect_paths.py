"""The six P7.4b development defects, driven through the real product path.

Each case runs the real Odoo adapter (on an in-memory Odoo), the real server session with a
real principal, and a scripted model replaying the tool sequence the paid batch observed or
the one the fix asks the model to take. The visible answer is produced by the product's own
renderer and judged by the evaluator's own extractor with the evaluator's own required and
forbidden facts.

This file covers the earlier P7.4b defects through the current package Skills and turn
contract. Package descriptions do not supply lexical write triggers: a capability search
that misses an installed write is inconclusive, and execution requires the loaded Skill
plus explicit tool intent. The tests also retain the business-rule refusal, exact count,
and confirmed-order read paths.

What this cannot show is that the model will choose the scripted path. It checks the
current product result and evaluator verdict when that path is chosen. An inconclusive
capability search intentionally gives a conservative answer instead of a false gap claim.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from evals.odoo.judging import extractor as ex
from web_operation_agent.state.conversation import ConversationState
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.execution.product import (AgentAnswerContext, AgentTurnMessage,
                                         render_neutral_user_answer)
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
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
                 writer: bool = True, chat=None, conversation_context: dict | None = None):
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
        self.chat = chat if chat is not None else _RecordingChat(script)
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
            allow_clarification=True, conversation_context=conversation_context,
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
    """A missing lexical match cannot assert a capability gap in a write-enabled host."""

    def _declines(self, *, prompt: str, writer: bool):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt=prompt, writer=writer,
                       tables={"res.partner": _customers([("Corvane Studio PR6", "c@e.test")])},
                       script=[("check_capability", {}), GAP_FINISH]).run()
            self.assertIn("check_capability", run.chat.offered[0],
                          "the check must be offered before any read")
            if writer:
                self.assertEqual(run.events("capability_check"), [])
                self.assertEqual(len(run.events("capability_check_inconclusive")), 1)
                self.assertEqual(run.events("capability_gap"), [])
            else:
                [check] = run.events("capability_check")
                self.assertEqual(check["matches"], [])
                [gap] = run.events("capability_gap")
                self.assertEqual((gap["mutation_proposals"], gap["dispatches"]), (0, 0))
            self.assertEqual(run.events("business_mutation_proposed"), [])
            self.assertEqual(run.events("business_mutation_execution_started"), [])
            self.assertEqual(run.result.terminal_reason, "agent_refused")
            self.assertEqual(run.browser.actions, [], "a declined change touches no page")
            return run.answer

    def test_d25_a_writer_asking_to_delete_a_customer(self):
        answer = self._declines(prompt="删除客户 Corvane Studio PR6。", writer=True)
        self.assertIn("不足以可靠回答", answer)
        self.assertNotEqual(_judge(answer, [ex.Marker("unsupported_refusal")]).status, "PASS",
                            "an inconclusive search must not claim the operation is absent")

    def test_d21_a_reader_asking_to_create_a_customer(self):
        answer = self._declines(prompt="创建客户 Ivydell Works PR6，邮箱 ivy@e.test。",
                                writer=False)
        # The evaluator's permission shape accepts the unsupported marker as its alternative.
        verdicts = {_judge(answer, [ex.Marker("permission_refusal")]).status,
                    _judge(answer, [ex.Marker("unsupported_refusal")]).status}
        self.assertIn("PASS", verdicts, answer)

    def test_a_writer_can_propose_the_installed_create_only_with_skill_and_execute_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt="创建客户 Ivydell Works PR6，邮箱 ivy@e.test。",
                       writer=True, tables={"res.partner": []},
                       script=[("load_skill", {"skill_id": "odoo.customer-maintenance"}),
                               ("create_customer", {"name": "Ivydell Works PR6",
                                                    "email": "ivy@e.test", "intent": "execute",
                                                    "supporting_skill": "odoo.customer-maintenance"})]).run()
            self.assertNotIn("create_customer", run.chat.offered[0])
            self.assertIn("create_customer", run.chat.offered[1])
            self.assertEqual(len(run.events("business_mutation_proposed")), 1)
            self.assertEqual(len(run.events("business_mutation_denied")), 1)
            self.assertEqual(run.events("business_mutation_execution_started"), [])
            self.assertEqual(run.result.terminal_reason, "approval_denied")
            self.assertEqual(run.browser.actions, [])


class BusinessRefusalThroughTheRealSession(unittest.TestCase):
    """d23: an order with nothing to invoice is refused before anything is proposed, by name."""

    def test_d23_the_refusal_names_the_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt="为销售订单 S22400 创建客户发票。", writer=True,
                       tables={"sale.order": _orders(
                           [(14, "S22400", 19, 120.0, "USD", "invoiced")])},
                       script=[("load_skill", {"skill_id": "odoo.order-billing"}),
                               ("invoice_sale_order", {"reference": "S22400",
                                                       "intent": "execute",
                                                       "supporting_skill": "odoo.order-billing"})]).run()
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
                       script=[("load_skill", {"skill_id": "odoo.customer-maintenance"}),
                               ("query_customers", {"answer_goals": [
                                   {"target_read_tool": "query_customers",
                                    "measures": ["count"]}]}),
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

    def test_package_skills_do_not_infer_write_intent_from_order_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            read = self._run(Path(tmp), "客户 Corvane Supply PR6 的已确认销售订单合计金额是多少？", [])
            self.assertFalse(read.session._explicit_mutation_intent)
        with tempfile.TemporaryDirectory() as tmp:
            write = self._run(Path(tmp), "确认订单 S20002。", [])
            self.assertFalse(write.session._explicit_mutation_intent)
            self.assertIn("odoo.sales-processing", write.session._task_business_skill_ids)
            # With package Skills, the write is offered only after loading its Skill;
            # the tool call must then declare execute intent and still needs approval.

    def test_d10_the_breakdown_by_currency(self):
        usd, eur = Decimal("150.50"), Decimal("230.25")
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(Path(tmp), "客户 Corvane Supply PR6 的已确认销售订单合计金额是多少？", [
                ("load_skill", {"skill_id": "odoo.sales-processing"}),
                ("query_sale_orders", {"customer_id": 19, "answer_goals": [
                    {"target_read_tool": "query_sale_orders", "measures": ["sum"]}]}),
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


class _RepairingChat(_RecordingChat):
    """The model development-003 showed on the date-range count.

    After its reads it finished with the count conclusion carrying fields the answer schema
    does not have -- computation, operand_ref, result, value -- and when refused it changed
    only what the refusal told it to change: told nothing specific, it resubmitted the same
    fields until the repairs ran out. It did call compute on its own before the last try, and
    cited the result, still with the extra fields.
    """

    def __init__(self, reads, conclusion):
        super().__init__(list(reads))
        self.conclusion = dict(conclusion)

    def complete(self, messages, tools, run_id, **kwargs):
        if not self.script and self.calls < 12:
            last = str(messages[-1].get("content") or "")
            computed = re.findall(r'"ref":"(calc#\d+)"', last)
            if computed:
                self.conclusion["evidence_ref"] = computed[0]
            elif "REFUSED" in last and "remove " in last:
                for key in [k for k in self.conclusion if f"'{k}'" in last]:
                    self.conclusion.pop(key)
            repair = re.search(r"call compute with (\{[^}]*\})", last)
            if repair and not computed:
                self.script.append(("compute", json.loads(repair.group(1))))
            else:
                self.script.append(("finish", {"outcome": "answered", "answer": {
                    "version": 1, "coverage": "complete", "presentation": "brief",
                    "conclusions": [dict(self.conclusion)]}}))
        return super().complete(messages, tools, run_id, **kwargs)


class CountAnswerRepairThroughTheRealSession(unittest.TestCase):
    """d30: a count the read established exactly, lost to a refusal that did not say why."""

    def test_d30_the_refusal_says_what_to_change_and_the_count_survives(self):
        march = [(40 + i, f"S3{i:04d}", 19, 10.0, "USD", "no") for i in range(10)]
        rows = _orders(march + [(60, "S39998", 19, 5.0, "USD", "no"),
                                (61, "S39999", 19, 5.0, "USD", "no")])
        for row in rows[:10]:
            row["date_order"] = f"2026-03-{row['id'] - 39:02d} 09:00:00"
        rows[10]["date_order"], rows[11]["date_order"] = ("2026-02-28 09:00:00",
                                                          "2026-04-01 09:00:00")
        chat = _RepairingChat(
            [("load_skill", {"skill_id": "odoo.sales-processing"}),
             ("query_sale_orders", {"date_from": "2026-03-01", "date_until": "2026-04-01",
                                    "limit": 50, "answer_goals": [
                                        {"target_read_tool": "query_sale_orders",
                                         "measures": ["count"]}]})],
            {"kind": "aggregate", "evidence_ref": "call2-query#1", "computation": "count",
             "operand_ref": "call2-query#1", "result": "10", "value": "10"})
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(Path(tmp), prompt="2026 年 3 月一共有多少张销售订单？", writer=True,
                       tables={"sale.order": rows}, script=[], chat=chat).run()
            refused = [e.get("detail", "") for e in run.events("answer_contract_refused")]
            self.assertTrue(refused and "'value'" in refused[0], refused)
            self.assertEqual(run.result.terminal_reason, "agent_finished", refused)
            verdict = _judge(run.answer, [ex.Count(10)], forbidden=())
            self.assertEqual((verdict.status, verdict.ambiguous), ("PASS", False),
                             (run.answer, refused))


class WithdrawnRequestThroughTheRealSession(unittest.TestCase):
    """d14: "never mind" after the session asked which customer is answered by the server."""

    CHOICES = [("Juniper Printing PR6 North", 24), ("Juniper Printing PR6 South", 25)]

    def _context(self, asked: str) -> dict:
        state = ConversationState()
        state.record(
            message_id="m-0", user_text=asked, assistant_text="找到两个匹配的客户，请问要打开哪一个？",
            status="needs_input", clarification={
                "schema_version": 1, "clarification_id": "clar-1", "task_id": "p74b",
                "question": "找到两个匹配的客户，请问要打开哪一个？",
                "response_kind": "single_choice",
                "choices": [{"choice_id": f"choice-{record_id}", "label": label,
                             "record_ref": f"res.partner/{record_id}"}
                            for label, record_id in self.CHOICES]})
        return state.model_context()

    def _reply(self, root: Path, script: list, *, pending: bool = True):
        return _Run(root, prompt="算了，先不用打开了。", writer=True,
                    tables={"res.partner": _customers(
                        [(label, f"{record_id}@e.test") for label, record_id in self.CHOICES])},
                    script=script,
                    conversation_context=(self._context("打开客户 Juniper Printing PR6。")
                                          if pending else None)).run()

    def test_d14_the_withdrawal_is_answered_in_a_fixed_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._reply(Path(tmp), [("withdraw_request", {})])
            self.assertIn("withdraw_request", run.chat.offered[0])
            [withdrawn] = run.events("request_withdrawn")
            self.assertEqual((withdrawn["page_actions"], withdrawn["dispatches"]), (0, 0))
            self.assertEqual(run.result.terminal_reason, "agent_finished")
            self.assertEqual(run.browser.actions, [])
            verdict = _judge(run.answer, [ex.Marker("cancelled_no_action")])
            self.assertEqual(verdict.status, "PASS", run.answer)

    def test_it_is_refused_once_the_turn_has_acted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._reply(Path(tmp), [
                ("load_skill", {"skill_id": "odoo.customer-maintenance"}),
                ("query_customers", {"name_contains": "Juniper Printing PR6"}),
                ("goto", {"url": "/odoo/customers"}),
                ("withdraw_request", {})])
            self.assertEqual(run.browser.actions, ["observe", "goto"])
            self.assertEqual(len(run.events("request_withdrawal_refused")), 1)
            self.assertEqual(run.events("request_withdrawn"), [])

    def test_it_is_not_offered_without_a_pending_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._reply(Path(tmp), [("finish", {"done": False, "why": "x"})],
                              pending=False)
            self.assertNotIn("withdraw_request", run.chat.offered[0])


if __name__ == "__main__":
    unittest.main()
