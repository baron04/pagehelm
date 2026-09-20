"""A declined change stands on a search of the whole catalog; a count stands on a whole read.

These tests are deliberately dynamic. The defect being fixed was a product that answered a
delete request with the customer's name and city, and the cheapest wrong fix is a hardcoded
"deleting customers is unsupported". So the cases below install and remove the capability at
runtime and require the answer to follow the catalog, not a literal.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from web_operation_agent.answer import answer_protocol
from web_operation_agent.answer.answer_view import capability_gap_answer
from web_operation_agent.business.runtime import BusinessSkill, BusinessToolRuntime
from web_operation_agent.answer.computation import ComputationRefused, ComputationRequest, run
from web_operation_agent.answer.conclusion import ConclusionRefused
from web_operation_agent.answer.evidence import (CapabilityEvidence, Origin, QueryEvidence,
                                          TurnEvidence)


def _skill(path: Path, *, skill_id: str, triggers: list[str], tools: list[str],
           read_only: bool) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1, "skill_id": skill_id, "title": skill_id,
        "intent": "Fixture capability.", "triggers": triggers,
        "instructions": [f"Call {tools[0]} and trust its current result."],
        "tool_names": tools, "provenance": ["src:fixture@v1:customers.json#c"],
        "read_only": read_only,
    }) + "\n")
    return BusinessSkill.load(path)


_EFFECTS = {"query_customers": "read", "delete_customer": "write"}


def _executor_for(skills: tuple[BusinessSkill, ...]):
    """An executor declaring exactly the tools those Skills own; nothing is dispatched."""
    names = sorted({name for skill in skills for name in skill.tool_names})

    class _Executor:
        @staticmethod
        def tool_schemas():
            return tuple({"type": "function", "function": {
                "name": name, "description": f"Fixture {name}.",
                "parameters": {"type": "object", "properties": {},
                               "additionalProperties": False}}} for name in names)

        @staticmethod
        def tool_effects():
            return {name: _EFFECTS[name] for name in names}

        @staticmethod
        def execute(name, args, *, context=None):  # pragma: no cover - never dispatched
            raise NotImplementedError

    return _Executor()


def _capability(matches: tuple[str, ...], *, goal: str = "删除客户 Ivydell Works") -> CapabilityEvidence:
    return CapabilityEvidence(
        evidence_id="capability#1",
        origin=Origin(kind="capability", reference="capability-check:run-1",
                      observed_at="1700000000.0", scope="customers"),
        application="odoo", application_version="sha256:abc", actor="digest:actor",
        catalog_digest="sha256:catalog", goal=goal, matches=matches)


class CapabilityResolutionFollowsTheCatalog(unittest.TestCase):
    """Requirement: the search covers the installed catalog, and it is live."""

    def _runtime(self, directory: Path, skills: tuple[BusinessSkill, ...]) -> BusinessToolRuntime:
        return BusinessToolRuntime(skills, _executor_for(skills))

    def test_a_delete_goal_matches_nothing_when_no_delete_capability_is_installed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "q.json", skill_id="fixture.customers-query",
                           triggers=["customers", "查询客户"], tools=["query_customers"],
                           read_only=True)
            runtime = self._runtime(root, (query,))
            self.assertEqual(
                runtime.resolve_capability("删除客户 Ivydell Works", allow_mutations=True), ())

    def test_installing_the_capability_at_runtime_stops_the_gap(self):
        """The same request, the same code: only the catalog changed."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "q.json", skill_id="fixture.customers-query",
                           triggers=["customers", "查询客户"], tools=["query_customers"],
                           read_only=True)
            before = self._runtime(root, (query,))
            self.assertEqual(before.resolve_capability("删除客户 X", allow_mutations=True), ())

            delete = _skill(root / "d.json", skill_id="fixture.customers-delete",
                            triggers=["delete customer", "删除客户"],
                            tools=["delete_customer"], read_only=False)
            after = self._runtime(root, (query, delete))
            self.assertEqual(
                after.resolve_capability("删除客户 X", allow_mutations=True),
                ("fixture.customers-delete",))

    def test_a_trigger_list_that_misses_does_not_retire_the_tool_it_owns(self):
        """A matcher miss is a matcher bug; it must not read as the application lacking it."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delete = _skill(root / "d.json", skill_id="fixture.customers-delete",
                            triggers=["obliterate the account"],  # matches nothing asked here
                            tools=["delete_customer"], read_only=False)
            runtime = self._runtime(root, (delete,))
            self.assertEqual(
                runtime.resolve_capability("please run delete_customer for Ivydell",
                                           allow_mutations=True),
                ("fixture.customers-delete",))

    def test_a_read_capability_for_the_same_entity_is_not_a_capability_to_change_it(self):
        """The bug this caught: `customers-query` triggers on the bare noun "客户", so it
        matched "删除客户" and reported a capability this session does not have. Looking at a
        record is not being able to change it."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "q.json", skill_id="fixture.customers-query",
                           triggers=["客户", "customers"], tools=["query_customers"],
                           read_only=True)
            runtime = self._runtime(root, (query,))
            self.assertEqual(
                runtime.resolve_capability("删除客户 Ivydell Works", allow_mutations=True), ())
            # It is still reachable when the question is what this session can do at all.
            self.assertEqual(
                runtime.resolve_capability("删除客户 Ivydell Works", allow_mutations=True,
                                           effect="any"),
                ("fixture.customers-query",))

    def test_a_read_only_turn_does_not_see_mutation_capabilities(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delete = _skill(root / "d.json", skill_id="fixture.customers-delete",
                            triggers=["删除客户"], tools=["delete_customer"], read_only=False)
            runtime = self._runtime(root, (delete,))
            self.assertEqual(runtime.resolve_capability("删除客户 X", allow_mutations=False), ())
            self.assertEqual(runtime.resolve_capability("删除客户 X", allow_mutations=True),
                             ("fixture.customers-delete",))

    def test_a_page_surface_that_can_make_the_change_is_part_of_the_search(self):
        """The session passes only surfaces that perform a change themselves -- enabled page
        writes. A form handoff is not one: the user submits it, and it answered every goal
        alike, which in the deployment that enables it made every gap impossible."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime(root, ())
            self.assertEqual(
                runtime.resolve_capability("删除客户 X", allow_mutations=True,
                                           extra=("page:submit",)),
                ("page:submit",))

    def test_an_unknown_effect_is_refused(self):
        with TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp), ())
            with self.assertRaises(ValueError):
                runtime.resolve_capability("删除客户 X", allow_mutations=True, effect="read")

    def test_a_search_needs_the_goal_it_searched_for(self):
        with TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp), ())
            with self.assertRaises(ValueError):
                runtime.resolve_capability("   ", allow_mutations=True)


class TheConclusionStandsOnAnEmptySearch(unittest.TestCase):
    def _turn(self, evidence: CapabilityEvidence) -> TurnEvidence:
        turn = TurnEvidence()
        turn.record(evidence)
        return turn

    def _submit(self, turn: TurnEvidence, conclusions: list[dict]):
        return answer_protocol.submit(turn, {
            "outcome": "unable",
            "answer": {"version": 1, "coverage": "undetermined", "conclusions": conclusions}})

    def test_an_empty_search_verifies(self):
        turn = self._turn(_capability(()))
        verified = self._submit(turn, [{"kind": "capability_gap",
                                        "evidence_ref": "capability#1"}])
        self.assertEqual(len(verified["conclusions"]), 1)
        item = verified["conclusions"][0]
        self.assertEqual(item["kind"], "capability_gap")
        self.assertEqual(item["matches"], [])
        self.assertEqual(item["catalog_digest"], "sha256:catalog")
        self.assertEqual(item["source_kind"], "capability")

    def test_a_search_that_matched_is_refused(self):
        """Holding the capability and declining anyway is the failure mode in the other direction."""
        turn = self._turn(_capability(("fixture.customers-delete",)))
        with self.assertRaises(ConclusionRefused) as caught:
            self._submit(turn, [{"kind": "capability_gap", "evidence_ref": "capability#1"}])
        self.assertIn("fixture.customers-delete", str(caught.exception))

    def test_a_gap_cannot_be_mixed_with_what_was_read_on_the_way(self):
        turn = self._turn(_capability(()))
        with self.assertRaises(ConclusionRefused):
            self._submit(turn, [{"kind": "capability_gap", "evidence_ref": "capability#1"},
                                {"kind": "capability_gap", "evidence_ref": "capability#1"}])

    def test_a_gap_must_cite_a_capability_search(self):
        turn = TurnEvidence()
        with self.assertRaises(Exception):
            self._submit(turn, [{"kind": "capability_gap", "evidence_ref": "nothing#1"}])

    def test_an_adapter_cannot_import_a_capability_claim(self):
        """`capture` is the adapter door; what this session can run is not an adapter's to say."""
        turn = TurnEvidence()
        with self.assertRaises(TypeError):
            turn.capture((_capability(()),), namespace="call1")


class TheSentenceSaysWhichKindOfGapItIs(unittest.TestCase):
    """Two gaps, two sentences, and neither sends the reader back to the assistant.

    Both used to read "当前会话不支持这项操作" -- which a person reads as "another session
    would", so they open a new one and meet the same wall. A capability nobody installed and
    one nobody granted this account are different problems with different people to ask
    (P7.5 CR-14).
    """

    def test_a_capability_this_deployment_does_not_have(self):
        self.assertEqual(capability_gap_answer("删除客户 Ivydell Works"),
                         "当前接入不支持这项操作，本次没有执行任何更改。")
        self.assertEqual(
            capability_gap_answer("Delete customer Ivydell Works"),
            "That operation is not supported by this integration, so it was not executed.")

    def test_a_capability_this_account_was_not_granted(self):
        chinese = capability_gap_answer("新建客户 Ivydell Works", withheld=True)
        self.assertEqual(chinese, "你的账号没有权限执行这项操作，本次没有执行任何更改。"
                                  "需要的话请联系管理员授予。")
        english = capability_gap_answer("Create customer Ivydell Works", withheld=True)
        self.assertIn("does not have permission", english)
        self.assertIn("Ask an administrator", english)

    def test_the_sentence_reads_as_a_permission_refusal_to_a_reader_and_a_scorer(self):
        """"没有权限" / "permission" is how both a person and the evaluator recognise it."""
        self.assertIn("没有权限", capability_gap_answer("新建客户 X", withheld=True))
        self.assertIn("permission", capability_gap_answer("Create customer X", withheld=True))

    def test_neither_sentence_suggests_another_session_would_work(self):
        for text in (capability_gap_answer("删除客户 X"),
                     capability_gap_answer("删除客户 X", withheld=True),
                     capability_gap_answer("Delete customer X"),
                     capability_gap_answer("Delete customer X", withheld=True)):
            with self.subTest(text=text):
                self.assertNotIn("当前会话", text)
                self.assertNotIn("this session", text)

    def test_it_never_reports_the_capability_as_not_found(self):
        """A missing capability is not an absent record, and the first draft said "没有找到",
        which reads as exactly that."""
        for text in ("删除客户 X", "Delete customer X"):
            answer = capability_gap_answer(text)
            self.assertNotIn("没有找到", answer)
            self.assertNotIn("未找到", answer)
            self.assertNotIn("not found", answer.lower())


if __name__ == "__main__":
    unittest.main()


class ACountOfAPartialReadIsRefused(unittest.TestCase):
    """d06: the product answered "how many customers are there" with 239 over a read it had
    limited to 50, labelled "within what was read". The label was honest and the number still
    answered nothing, so the count is refused and the remedy named instead."""

    def _query(self, *, covers_whole_scope: bool, matched: int) -> QueryEvidence:
        return QueryEvidence(
            evidence_id="q#1",
            origin=Origin(kind="typed", reference="query_customers", observed_at="1.0",
                          scope="customers visible to the delegated user"),
            collection="customers", conditions=(),
            matched_count=matched, records_scanned=matched,
            covers_whole_scope=covers_whole_scope)

    def _count(self, query: QueryEvidence):
        turn = TurnEvidence()
        turn.record(query)
        return run(turn, ComputationRequest(operation="count", over=("q#1",)),
                   evidence_id="calc#1", observed_at="2.0")

    def test_a_count_over_the_whole_scope_is_allowed(self):
        item = self._count(self._query(covers_whole_scope=True, matched=239))
        self.assertEqual((item.value, item.covers_whole_scope), ("239", True))

    def test_a_count_whose_scan_did_not_finish_is_refused_without_an_impossible_remedy(self):
        with self.assertRaises(ComputationRefused) as caught:
            self._count(self._query(covers_whole_scope=False, matched=239))
        message = str(caught.exception)
        self.assertIn("did not finish", message)
        # The earlier text sent the model to "drop any limit" over a tool that caps its rows
        # anyway; it looped, and one answer went from 12 seconds to 86.
        self.assertNotIn("limit", message)
        self.assertNotIn("239", message)

    def test_an_exact_count_over_a_capped_read_is_answered(self):
        """d06: twenty rows came back, the application counted 239, and 239 is the answer."""
        query = QueryEvidence(
            evidence_id="q#1",
            origin=Origin(kind="typed", reference="query_customers", observed_at="1.0",
                          scope="customers visible to the delegated user"),
            collection="customers", conditions=(), matched_count=239, records_scanned=239,
            covers_whole_scope=False, count_exact=True)
        item = self._count(query)
        self.assertEqual((item.value, item.covers_whole_scope), ("239", True))
