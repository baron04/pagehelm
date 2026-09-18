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

from web_operation_agent import answer_protocol
from web_operation_agent.answer_view import capability_gap_answer
from web_operation_agent.business import BusinessSkill, BusinessToolRuntime
from web_operation_agent.computation import ComputationRefused, ComputationRequest, run
from web_operation_agent.conclusion import ConclusionRefused
from web_operation_agent.evidence import (CapabilityEvidence, Origin, QueryEvidence,
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

    def test_the_non_typed_surfaces_are_part_of_the_search(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime(root, ())
            self.assertEqual(
                runtime.resolve_capability("删除客户 X", allow_mutations=True,
                                           extra=("form_handoff",)),
                ("form_handoff",))

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


class TheSentenceIsFixedAndIsAboutThisSession(unittest.TestCase):
    def test_chinese_and_english(self):
        self.assertEqual(capability_gap_answer("删除客户 Ivydell Works"),
                         "当前会话不支持这项操作，本次没有执行任何更改。")
        self.assertEqual(
            capability_gap_answer("Delete customer Ivydell Works"),
            "That operation is not supported in this session, so it was not executed.")

    def test_it_is_scoped_to_this_session_not_to_the_product_forever(self):
        """Installing the capability is a configuration away, so the sentence cannot be
        absolute; it says what is true of this session."""
        self.assertIn("当前会话", capability_gap_answer("删除客户 X"))
        self.assertIn("in this session", capability_gap_answer("Delete customer X"))

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

    def test_a_count_over_a_partial_read_is_refused_and_names_the_remedy(self):
        with self.assertRaises(ComputationRefused) as caught:
            self._count(self._query(covers_whole_scope=False, matched=239))
        message = str(caught.exception)
        self.assertIn("covered only part", message)
        self.assertIn("drop any limit", message)
        self.assertNotIn("239", message)
