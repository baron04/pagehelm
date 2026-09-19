"""A read this account may not do no longer takes the rest of the answer with it (CR-26).

J4.03 asks a limited account "Azure Interior 的电话是多少？它有哪些销售订单？". The orders
read is refused by Odoo's own permission system, and the turn ended there: the phone, which
that account may read, was never answered and the person had to ask again. The refusal now
comes back to the model, which answers what it read; the page stops being an accepted source
of facts so the refused scope cannot be read off the screen instead, and the server says
what was not read whether or not the model does (owner ruling D13).
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from web_operation_agent.business import BusinessToolPermissionError
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.session import ServerAgentSession, TaskSpec
from web_operation_agent_odoo.adapter import OdooApplicationAdapter
from web_operation_agent_odoo.tests.test_adapter import (
    BASE, ReadOnlyControl, _customers, _FakeOdoo, _key_file, _orders,
    _ProductRoutingBrowser, _ProductRoutingChat)

PROMPT = "Azure Interior 的电话是多少？它有哪些销售订单？"


class _RefusesOrders(_FakeOdoo):
    """The application refuses this actor's read of sales orders, as Odoo's ACL does."""

    def call(self, model, method, args, kwargs=None, *, mutation=False):
        if model == "sale.order":
            raise BusinessToolPermissionError(
                "the application refused this operation for the current user",
                layer="application")
        return super().call(model, method, args, kwargs, mutation=mutation)


def _session(script: list, *, page_answer_evidence: bool = False):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    adapter = OdooApplicationAdapter(base_url=BASE, api_key_path=_key_file(root))
    customers = _customers([("Azure Interior", "azure@example.test")])
    customers[0]["phone"] = "+1 510 555 0102"
    adapter.business_tools._executor = _RefusesOrders(
        {"res.partner": customers, "sale.order": _orders(2)})
    now = int(time.time() * 1000)
    principal = PrincipalContext(
        schema_version=1, application_id="odoo", tenant_id="trial", workspace_id="7",
        actor_id="10", delegation_id="p75-cr26", issued_at=now - 1_000,
        expires_at=now + 300_000,
        scopes=tuple(sorted(adapter.ports.identity.required_scopes)))
    browser = _ProductRoutingBrowser()
    ledger = root / "turn.jsonl"
    session = ServerAgentSession(
        task=TaskSpec(task_ref="cr26", prompt=PROMPT, entry_path="/odoo/customers",
                      supported_scope="Read current Odoo records."),
        chat=_ProductRoutingChat(script), browser=browser, control=ReadOnlyControl(),
        ledger=SessionLedger(ledger, "cr26"), business_tools=adapter.business_tools,
        application_routes=adapter.ports.routes, principal=principal,
        session_id="cr26-session", turn_id="cr26-turn",
        page_answer_evidence=page_answer_evidence,
        disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
    session.entry_observation = browser.observe()
    session.plan()
    result = session.run()
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    tmp.cleanup()
    return result, rows


#: The phone is the third field the customer read reports (name, email, phone, ...).
PHONE_REF = "call2-record#3"
_READ_THEN_REFUSED = [
    ("load_skill", {"skill_id": "odoo.sales.customers-query"}),
    ("query_customers", {"name_contains": "Azure Interior", "limit": 5}),
    ("load_skill", {"skill_id": "odoo.sales.sale-orders-query"}),
    ("query_sale_orders", {"limit": 50}),
]


class TheAnswerKeepsWhatThisAccountMayRead(unittest.TestCase):
    def test_the_part_that_was_read_is_answered_and_the_refusal_is_stated(self):
        result, rows = _session(_READ_THEN_REFUSED + [
            ("finish", {"outcome": "partial", "answer": {
                "version": 1, "coverage": "partial",
                "conclusions": [{"kind": "field_value", "evidence_ref": PHONE_REF}]}})])

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("+1 510 555 0102", result.agent_said)
        self.assertIn("当前账号没有读取销售订单的权限，这一部分没有完成。", result.agent_said)
        self.assertEqual(result.verified_answer["coverage"], "partial")
        refusals = [row for row in rows
                    if row.get("event") == "business_tool_permission_denied"]
        self.assertEqual([row["turn_continued"] for row in refusals], [True])
        self.assertEqual(refusals[0]["layer"], "application")

    def test_an_answer_that_ignores_the_refusal_still_cannot_claim_complete(self):
        result, _rows = _session(_READ_THEN_REFUSED + [
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": PHONE_REF}]}})])

        self.assertEqual(result.verified_answer["coverage"], "partial")
        self.assertIn("当前账号没有读取销售订单的权限", result.agent_said)

    def test_the_page_is_no_longer_an_accepted_source_of_facts(self):
        result, rows = _session(_READ_THEN_REFUSED + [
            ("observe", {"answer_evidence": True}),
            ("finish", {"outcome": "partial", "answer": {
                "version": 1, "coverage": "partial",
                "conclusions": [{"kind": "field_value", "evidence_ref": PHONE_REF}]}})],
            page_answer_evidence=True)

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual([row for row in rows if row.get("event") == "page_answer_evidence"], [])
        self.assertIn("当前账号没有读取销售订单的权限", result.agent_said)


class ARefusalWithNothingReadStillRefuses(unittest.TestCase):
    """The turn reached nothing it may answer from, so it ends as the refusal it hit.

    An answer needs a conclusion and a conclusion needs evidence, so a turn whose only read
    was refused has no way to state the gap as an answer. It must not end on "not enough
    information": the person has to learn which operation their account was refused.
    """

    def test_a_turn_that_read_nothing_ends_by_naming_the_refused_operation(self):
        give_up = ("finish", {"outcome": "unable", "answer": {
            "version": 1, "coverage": "undetermined", "conclusions": []}})
        result, _rows = _session([
            ("load_skill", {"skill_id": "odoo.sales.sale-orders-query"}),
            ("query_sale_orders", {"limit": 50}),
        ] + [give_up] * 4)

        self.assertEqual(result.terminal_reason, "permission_denied")
        self.assertEqual(result.refused_operation, "读取销售订单")
        self.assertNotIn("+1 510 555 0102", result.agent_said)


if __name__ == "__main__":
    unittest.main()
