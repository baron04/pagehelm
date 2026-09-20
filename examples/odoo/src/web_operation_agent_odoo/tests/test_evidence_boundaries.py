"""The evidence rules, exercised through a real product turn instead of in isolation.

P7.5 5.5: B01–B18 of the functional matrix were `UNIT_ONLY`. The rules they pin are
server-side, but a rule that holds in a unit and not in an assembled turn protects nobody,
and assembly is where this product has repeatedly gone wrong. These drive a real
`ServerAgentSession` over the shipped Odoo Skills and tool surface, with a scripted model,
and check the same propositions at that level.

Matrix cases covered here: B01, B03 (as the assembly meets it), B05, B08, B10, B11, B12,
B13, B15, B16, B17, B18. B02 is not reachable from a turn and stays `UNIT_ONLY`: evidence
identifiers are minted by the session itself (`call2-query#1`), so a batch cannot collide
with another, and inducing one would mean reaching past the assembly this file exists to
test.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent_odoo.adapter import OdooApplicationAdapter
from web_operation_agent_odoo.tests.test_adapter import (BASE, _FakeOdoo, _key_file,
                                                         _ProductRoutingBrowser,
                                                         _ProductRoutingChat, _customers,
                                                         _quotations)

READ_SCOPE = ("Read current Odoo records through the typed API. "
              "Do not modify records or submit page forms.")


def _rows(pairs, **extra):
    rows = _customers(pairs)
    for row in rows:
        row.update(extra)
    return rows


class _Turn:
    """One assembled product turn over the shipped Odoo surface."""

    def __init__(self, case, script, *, tables=None, prompt="列出客户", executor=None,
                 deterministic=True, skill="odoo.sales.customers-query"):
        self.case = case
        # A real turn loads the Skill that names the tool before calling it; these scripts
        # stand in for a model, so they do what a model has to do.
        self.script = [("load_skill", {"skill_id": skill})] + list(script)
        self.tables = tables or {"res.partner": _rows([
            ("Acme Corporation", "acme@example.test"),
            ("Azure Interior", "azure@example.test"),
        ])}
        self.prompt = prompt
        self.executor = executor
        #: The product opens a uniquely read record itself, without asking the model again.
        #: A case about what the *model* claims after acting has to switch that off, or the
        #: claim is never made.
        self.deterministic = deterministic

    def run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(root),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())
            adapter.business_tools._executor = (
                self.executor if self.executor is not None else _FakeOdoo(self.tables))
            chat = _ProductRoutingChat(self.script)
            browser = _ProductRoutingBrowser()
            ledger_path = root / f"{self.case}.jsonl"
            session = ServerAgentSession(
                task=TaskSpec(task_ref=self.case, prompt=self.prompt,
                              entry_path="/odoo/customers", supported_scope=READ_SCOPE),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(ledger_path, self.case),
                business_tools=adapter.business_tools,
                business_mutation_approval=lambda *_a, **_k: None,
                application_routes=adapter.ports.routes,
                deterministic_read_navigation=self.deterministic,
            )
            session.entry_observation = browser.observe()
            session.plan()
            result = session.run()
            rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        return result, rows, browser


def _refusals(rows):
    return [row for row in rows if row.get("event") == "answer_contract_refused"]


class EvidenceKeepsItsBatch(unittest.TestCase):
    def test_b01_two_reads_in_one_turn_do_not_share_references(self):
        """B01: the second read must not answer with the first read's values."""
        result, _rows, _browser = _Turn("B01", [
            ("query_customers", {"name_contains": "Acme"}),
            ("query_customers", {"name_contains": "Azure"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "selection", "evidence_ref": "call2-query#1",
                                 "fields": ["name"]}]}}),
        ], prompt="先看客户 Acme，再看客户 Azure").run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        # `call2-query#1` is the first read. Citing it answers with Acme, whatever the
        # second read returned.
        self.assertIn("Acme Corporation", result.agent_said)
        self.assertNotIn("Azure Interior", result.agent_said)

    def test_b03_a_value_too_long_to_show_is_never_shown_as_if_it_were_whole(self):
        """B03, as the assembled product meets it.

        The unit rule -- a batch past the turn's evidence bound is refused whole -- is not
        reachable through the Odoo reads, because the adapter bounds each value before it
        becomes evidence. What the assembly did instead was cut a long value to its first
        300 characters in silence and let the answer present that prefix as the field's
        value. A shortened value now says it is shortened. The turn-level byte bound itself
        stays `UNIT_ONLY`.
        """
        huge = "x" * 40_000
        result, _ledger, _browser = _Turn("B03", [
            ("query_customers", {"limit": 5}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "selection", "evidence_ref": "call2-query#1",
                                 "fields": ["name", "city"]}]}}),
        ], tables={"res.partner": _rows([("Acme Corporation", "acme@example.test")],
                                        city=huge)}).run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertNotIn(huge[:301], result.agent_said)
        self.assertIn("x" * 300 + "…", result.agent_said)


class ComputationStaysInsideWhatWasRead(unittest.TestCase):
    def test_b05_filtering_again_cannot_bring_back_what_the_first_filter_removed(self):
        result, _rows, _browser = _Turn("B05", [
            ("query_customers", {"limit": 5}),
            ("compute", {"operation": "filter", "dataset_ref": "call2-query#1",
                         "field": "name", "comparison": "eq",
                         "threshold": "Acme Corporation"}),
            ("compute", {"operation": "filter", "dataset_ref": "calc#3",
                         "field": "name", "comparison": "ne",
                         "threshold": "Acme Corporation"}),
            ("compute", {"operation": "count", "dataset_ref": "calc#4"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "aggregate", "evidence_ref": "calc#5"}]}}),
        ], prompt="客户里先筛出 Acme，再在结果里筛掉 Acme，还剩几条？").run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        # Nothing can come back: the second filter runs inside the first one's result.
        self.assertIn("0", result.agent_said)
        self.assertNotIn("Azure Interior", result.agent_said)

    def test_b08_a_scan_that_did_not_finish_gives_a_subtotal_not_a_total(self):
        """B08: what was not read is not added up as if it had been."""
        class Vanishing(_FakeOdoo):
            """Odoo counts twelve; the scan comes back with three, then nothing."""

            def call(self, model, method, args, kwargs=None):
                rows = super().call(model, method, args, kwargs)
                if method == "search_count":
                    return 12
                if method == "search_read":
                    return rows[:3] if (kwargs or {}).get("offset", 0) == 0 else []
                return rows

        quotations = _quotations([(f"S{index:05d}", "draft") for index in range(12)])
        result, rows, _browser = _Turn("B08", [
            ("query_quotations", {"limit": 50}),
            ("compute", {"operation": "filter", "dataset_ref": "call2-query#1",
                         "field": "amount_total", "comparison": "gt", "threshold": "501"}),
            ("compute", {"operation": "sum", "dataset_ref": "calc#3",
                         "field": "amount_total"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "partial",
                "conclusions": [{"kind": "aggregate", "evidence_ref": "calc#4"}]}}),
        ], executor=Vanishing({"sale.order": quotations}),
            skill="odoo.sales.sale-orders-confirm",
            prompt="未确认报价单里金额大于 501 的合计多少钱？").run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        computed = [row for row in rows if row.get("event") == "answer_computed"]
        self.assertEqual([row["covers_whole_scope"] for row in computed], [False, False])
        # It is called a subtotal, and the answer says it rests on part of the scope.
        self.assertIn("小计", result.agent_said)
        self.assertIn("未覆盖全部范围", result.agent_said)
        self.assertNotIn("**合计", result.agent_said)

    def test_b10_a_field_that_was_not_read_is_not_a_zero(self):
        class WithoutAmounts(_FakeOdoo):
            def call(self, model, method, args, kwargs=None):
                rows = super().call(model, method, args, kwargs)
                if method == "search_read":
                    for row in rows:
                        row.pop("city", None)
                return rows

        executor = WithoutAmounts({"res.partner": _rows([
            ("Acme Corporation", "acme@example.test"),
            ("Azure Interior", "azure@example.test")])})
        result, rows, _browser = _Turn("B10", [
            ("query_customers", {"limit": 5}),
            ("compute", {"operation": "sum", "dataset_ref": "call2-query#1",
                         "field": "city"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "aggregate", "evidence_ref": "calc#3"}]}}),
        ] + [("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "aggregate", "evidence_ref": "calc#3"}]}})] * 3,
            executor=executor, prompt="把客户的城市字段加起来").run()

        # The sum over a field nobody read is never computed, so there is nothing to cite:
        # the turn ends saying it cannot answer, and no number reaches the reader.
        self.assertFalse([row for row in rows if row.get("event") == "answer_computed"])
        self.assertEqual(result.terminal_reason, "agent_refused")
        self.assertNotRegex(result.agent_said, r"\d")


class TheModelCannotSupplyFacts(unittest.TestCase):
    def test_b11_an_invented_value_in_finish_is_refused(self):
        result, rows, _browser = _Turn("B11", [
            ("query_customers", {"limit": 5}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call2-record#1",
                                 "value": "Acme Corporation 的电话是 +1 000 000 0000"}]}}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call2-record#1"}]}}),
        ], prompt="客户 Acme 的名称是什么？").run()

        # The first finish is refused for carrying a value of its own; the turn goes on and
        # the corrected one answers from evidence.
        self.assertTrue(_refusals(rows))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertNotIn("+1 000 000 0000", result.agent_said)

    def test_b12_an_unknown_reference_can_be_corrected_once(self):
        result, rows, _browser = _Turn("B12", [
            ("query_customers", {"limit": 5}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "record#999"}]}}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call2-record#1"}]}}),
        ], prompt="客户 Acme 的名称是什么？").run()

        self.assertTrue(_refusals(rows))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("Acme Corporation", result.agent_said)

    def test_b13_three_invalid_answers_end_the_turn_without_a_stray_table(self):
        bad = ("finish", {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete",
            "conclusions": [{"kind": "field_value", "evidence_ref": "record#999"}]}})
        result, rows, _browser = _Turn("B13", [
            ("query_customers", {"limit": 5}), bad, bad, bad, bad,
        ], prompt="客户 Acme 的名称是什么？").run()

        self.assertGreaterEqual(len(_refusals(rows)), 3)
        self.assertEqual(result.terminal_reason, "agent_refused")
        # The last read's table is not what a refused turn shows instead of an answer.
        self.assertNotIn("| 名称 |", result.agent_said)


class PageEvidenceDiesWithItsPage(unittest.TestCase):
    def test_b15_navigating_invalidates_page_facts_and_keeps_api_facts(self):
        result, rows, browser = _Turn("B15", [
            ("query_customers", {"record_id": 10}),
            ("observe", {"answer_evidence": True}),
            ("goto", {"url": "/odoo/customers/10"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "page_observation", "evidence_ref": "obs3-page#1"}]}}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call2-record#1"}]}}),
        ], prompt="打开客户 Acme 并告诉我它的名称").run()

        self.assertIn("goto", browser.actions)
        # The page fact from before the navigation is gone; the typed read survives it.
        self.assertTrue(_refusals(rows))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("Acme Corporation", result.agent_said)


class ActingIsNotEvidence(unittest.TestCase):
    def test_b16_claiming_an_action_without_a_receipt_is_refused(self):
        result, rows, browser = _Turn("B16", [
            ("query_customers", {"limit": 5}),
            ("finish", {"outcome": "acted", "answer": {
                "version": 1, "coverage": "complete", "conclusions": []}}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call2-record#1"}]}}),
        ], prompt="打开客户 Acme").run()

        self.assertNotIn("goto", browser.actions)
        self.assertTrue(_refusals(rows))
        self.assertEqual(result.terminal_reason, "agent_finished")

    def test_b17_a_successful_action_does_not_excuse_a_forged_field(self):
        result, rows, browser = _Turn("B17", [
            ("query_customers", {"record_id": 10}),
            ("goto", {"url": "/odoo/customers/10"}),
            ("finish", {"outcome": "acted", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call9-record#7"}]}}),
            ("finish", {"outcome": "acted", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "field_value", "evidence_ref": "call2-record#1"}]}}),
        ], prompt="打开客户 Acme", deterministic=False).run()

        self.assertIn("goto", browser.actions)
        self.assertTrue(_refusals(rows))
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("Acme Corporation", result.agent_said)


class SourcesFollowOnlyWhatWasCited(unittest.TestCase):
    def test_b18_an_unreferenced_record_leaves_no_trace_in_the_answer(self):
        result, _rows, _browser = _Turn("B18", [
            ("query_customers", {"limit": 5}),
            ("compute", {"operation": "filter", "dataset_ref": "call2-query#1",
                         "field": "name", "comparison": "eq",
                         "threshold": "Acme Corporation"}),
            ("compute", {"operation": "count", "dataset_ref": "calc#3"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete",
                "conclusions": [{"kind": "aggregate", "evidence_ref": "calc#4"}]}}),
        ], prompt="名字是 Acme Corporation 的客户有几个？").run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIn("1", result.agent_said)
        # The other customer was read but not cited: it is not in the answer, and neither
        # are the application's internal identifiers.
        self.assertNotIn("Azure Interior", result.agent_said)
        self.assertNotIn("res.partner/", result.agent_said)


if __name__ == "__main__":
    unittest.main()
