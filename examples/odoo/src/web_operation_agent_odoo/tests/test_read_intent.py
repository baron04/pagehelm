"""A question that names records by a state is not a request to change them (P7.5 CR-06).

"未确认报价单" -- quotations not yet confirmed -- contains the "确认报价单" trigger of the
confirm Skill. The session took the question for an explicit request to confirm one and
refused every read-only answer the model gave, so "当前有哪些未确认报价单？" ended with "not
enough information" although the read had returned all six. The same class as the P7.4b d10
defect ("已确认销售订单"), which covered only the markers of a completed action.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from web_operation_agent.business.runtime import trigger_requests_action
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent_odoo.adapter import (_WRITE_SKILLS, OdooApplicationAdapter,
                                              _packaged_skill)
from web_operation_agent_odoo.tests.test_adapter import (
    BASE, ReadOnlyControl, _FakeOdoo, _key_file, _ProductRoutingBrowser, _ProductRoutingChat,
    _quotations)


class EveryPartOfTheQuestionHasSomethingToReadIt(unittest.TestCase):
    """A question about two things gets a Skill for each (P7.5 CR-33).

    Triggers named entities only, so "Azure Interior 的电话是多少？它有哪些销售订单？" matched
    the sales-order Skill and nothing else: a trigger did match, so the empty fallback never
    ran, and the half about the phone had no tool that could read it -- for any account.
    """

    def _select(self, task: str):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)))
            return adapter.business_tools.select(task, allow_mutations=False)

    def _candidates(self, task: str) -> set[str]:
        return set(self._select(task).candidate_ids)

    def test_a_two_part_question_offers_a_read_for_each_part(self):
        self.assertEqual(
            self._candidates("Azure Interior 的电话是多少？它有哪些销售订单？"),
            {"odoo.sales.sale-orders-query", "odoo.sales.customers-query"})

    def test_a_customer_field_names_the_customer_read_without_the_fallback(self):
        """Matched by its own trigger, not by the empty fallback.

        These questions reach the customer read either way today, because nothing else
        matches them and the fallback then offers every read-only Skill. That stops being
        true as soon as another Skill's trigger appears in the same sentence, which is
        exactly the two-part case above.
        """
        for task in ("Azure Interior 的电话是多少？", "这家公司的邮箱是什么？",
                     "Acme 的税号是多少？", "What is Acme's phone number?"):
            with self.subTest(task=task):
                selection = self._select(task)
                self.assertIn("odoo.sales.customers-query", selection.trigger_matches)
                self.assertFalse(selection.fallback_used)


class AStateIsNotARequest(unittest.TestCase):
    def test_state_words_before_the_verb_describe_records(self):
        for task in ("当前有哪些未确认报价单？列出单号与金额。",
                     "没有确认的报价单有几张？",
                     "没确认的报价单一共多少钱？",
                     "待确认的报价单有哪些？",
                     "尚未确认的报价单金额最高的是哪张？",
                     "已确认报价单有几张？",
                     "已经确认报价单了吗？",
                     "被确认报价单的客户是谁？",
                     "不要确认报价单 S00007"):
            with self.subTest(task=task):
                self.assertFalse(trigger_requests_action("确认报价单", task))

    def test_every_shipped_write_trigger_reads_a_state_as_a_state(self):
        """Every Han trigger of every write Skill the Odoo distribution ships, not just the
        one the dry run happened to ask about: "未开票的订单" is not a request to invoice."""
        triggers = [trigger for name in _WRITE_SKILLS
                    for trigger in _packaged_skill(name).triggers
                    if any("\u4e00" <= character <= "\u9fff" for character in trigger)]
        self.assertTrue(triggers)
        for trigger in triggers:
            with self.subTest(trigger=trigger):
                for task in (f"未{trigger}的记录有哪些？", f"没有{trigger}的记录有几条？",
                             f"待{trigger}的记录有哪些？"):
                    self.assertFalse(trigger_requests_action(trigger, task), task)
                self.assertTrue(trigger_requests_action(trigger, f"请{trigger}"))

    def test_the_imperative_is_still_a_request(self):
        for task in ("确认报价单 S00007", "请确认报价单 S00007。", "帮我确认报价单 S00007"):
            with self.subTest(task=task):
                self.assertTrue(trigger_requests_action("确认报价单", task))


class TheUnconfirmedQuotationsQuestionIsAnswered(unittest.TestCase):
    """The tool sequence the P7.5 dry run observed, through the real session."""

    def test_the_list_the_read_returned_is_accepted_as_the_answer(self):
        prompt = "当前有哪些未确认报价单？列出单号与金额。"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(root), allow_mutations=True,
                mutation_journal=MemoryMutationJournal())
            adapter.business_tools._executor = _FakeOdoo({"sale.order": _quotations(
                [("S00002", "draft"), ("S00003", "draft"), ("S00006", "sent")])},
                allow_mutations=True)
            now = int(time.time() * 1000)
            principal = PrincipalContext(
                schema_version=1, application_id="odoo", tenant_id="trial",
                workspace_id="7", actor_id="42", delegation_id="p75-cr06",
                issued_at=now - 1_000, expires_at=now + 300_000,
                scopes=tuple(sorted(adapter.ports.identity.required_scopes)))
            chat = _ProductRoutingChat([
                ("load_skill", {"skill_id": "odoo.sales.sale-orders-confirm"}),
                ("query_quotations", {"limit": 50}),
                ("finish", {"outcome": "answered", "answer": {
                    "version": 1, "coverage": "complete", "presentation": "table",
                    "conclusions": [{"kind": "selection", "evidence_ref": "call2-query#1",
                                     "fields": ["name", "amount_total", "state"]}]}}),
            ])
            browser = _ProductRoutingBrowser()
            ledger = root / "turn.jsonl"
            session = ServerAgentSession(
                task=TaskSpec(task_ref="cr06", prompt=prompt, entry_path="/odoo/customers",
                              supported_scope="Read and change current Odoo records."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(ledger, "cr06"),
                business_tools=adapter.business_tools,
                business_mutation_approval=lambda *_a, **_k: None,
                application_routes=adapter.ports.routes, principal=principal,
                session_id="cr06-session", turn_id="cr06-turn", allow_clarification=True,
                disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
            session.entry_observation = browser.observe()
            session.plan()

            self.assertFalse(session._explicit_mutation_intent)
            result = session.run()

            refused = [row for row in (json.loads(line) for line in
                                       ledger.read_text().splitlines())
                       if row.get("event") == "answer_contract_refused"]
            self.assertEqual(refused, [])
            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.verified_answer["conclusions"][0]["members"],
                             ("sale.order/2", "sale.order/3", "sale.order/4"))
            self.assertEqual(browser.actions, [])


if __name__ == "__main__":
    unittest.main()


class AReaderHearsWhichKindOfNoItIs(unittest.TestCase):
    """The catalog a reader can run omits the write Skills, and a refusal must not read as
    "this deployment cannot" when the truth is "you were not granted it" (P7.5 CR-37).

    The adapter builds its catalog per grant, so for a reader the create Skill is not in it
    at all -- the capability search that decides the sentence would have found nothing to
    withhold, and every refusal would have blamed the deployment.
    """

    def _runtime(self, *, allow_mutations: bool):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=allow_mutations,
                mutation_journal=MemoryMutationJournal() if allow_mutations else None)
            return adapter.business_tools

    def test_a_reader_asking_to_create_is_told_it_is_their_permission(self):
        runtime = self._runtime(allow_mutations=False)
        self.assertEqual(runtime.resolve_capability("创建客户 Ivydell Works", allow_mutations=False), ())
        self.assertEqual(runtime.withheld_capability("创建客户 Ivydell Works"),
                         ("create_customer",))

    def test_a_capability_nobody_installed_is_withheld_by_nobody(self):
        runtime = self._runtime(allow_mutations=False)
        self.assertEqual(runtime.withheld_capability("删除客户 Ivydell Works"), ())

    def test_a_writer_withholds_nothing(self):
        runtime = self._runtime(allow_mutations=True)
        self.assertEqual(runtime.withheld_capability("创建客户 Ivydell Works"), ())
        self.assertEqual(runtime.resolve_capability("创建客户 Ivydell Works", allow_mutations=True),
                         ("create_customer",))
