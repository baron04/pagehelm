"""Deterministic source-rule lookup and release-bound answer evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.answer import answer_protocol
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.answer.conclusion import ConclusionRefused
from web_operation_agent.answer.evidence import (Origin, QueryEvidence, SourceEvidence,
                                                  SourceHistoryEvidence, TurnEvidence)
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.knowledge_tools import lookup_source, lookup_source_history
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.planning.source_facts import (CompiledSourceLookup, SourceFact,
                                                       SourceFactIndex, SourceHit,
                                                       SourceLookup, _fit, fact_index,
                                                       fact_text, terms)
from web_operation_agent.planning.source_history import SourceHistoryHit
from web_operation_agent.schemas import EvidenceRef, SourceMap, SourceRelation
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs
from web_operation_agent.tests.support.planning import _map
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT


def _with_guard(source_map: SourceMap) -> SourceMap:
    evidence = next(relation.evidence[0] for relation in source_map.relations
                    if relation.kind == "service_parameter")
    guard = SourceRelation(
        kind="service_guard", subject="sproutSeedling",
        object="A tray that is already sprouting cannot be sprouted again.",
        attrs={"form": "raise", "exception": "RefusalError",
               "condition": "tray.stage == 'sprouting'", "loop": "tray in self",
               "decorators": []},
        evidence=(evidence,))
    return replace(source_map, relations=[*source_map.relations, guard])


def _source_evidence(ref: str = "source#1",
                     excerpt: str = "if tray.stage == 'sprouting':\n    raise X"):
    return SourceEvidence(
        evidence_id=ref,
        origin=Origin(kind="source", reference="lookup_source:q", observed_at="1",
                      scope="the deployed release v0"),
        path="apps/greenhouse/rules.py", start_line=3, end_line=4, release="v0",
        sha256="a" * 64, excerpt=excerpt, summary="rule")


class _FixedLookup:
    def __init__(self, release: str, evidence: EvidenceRef):
        self.release = release
        self.evidence = evidence

    def search(self, query, *, limit=8, route=None):
        return (SourceHit(evidence=self.evidence,
                          text="def sprout(tray): raise Refusal"),)


class _FixedHistory:
    def __init__(self, release: str):
        self.release = release
        self.deployed_commit = "b" * 40

    def search(self, kind, **kwargs):
        del kwargs
        payload = dict(repository_id="fixture", release=self.release,
                       deployed_commit=self.deployed_commit, kind=kind,
                       statement="rules.py is attributed to Fixture Author in aaaaaaaaaaaa",
                       digest="c" * 64, path="rules.py", start_line=3, end_line=3,
                       commit="a" * 40)
        return (SourceHistoryHit(**payload),)


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source_map = _with_guard(_map(Path(self.tmp.name)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_identifiers_are_split_into_reader_terms(self):
        self.assertEqual(terms("invoice_status invoiceStatus"),
                         ["invoice", "status", "invoice", "status"])
        self.assertEqual(terms("the orders"), ["order"])

    def test_a_refusal_is_found_by_message_and_condition(self):
        facts = SourceFactIndex(self.source_map).search("already sprouting tray", limit=3)
        self.assertTrue(facts)
        self.assertEqual(facts[0].relation.kind, "service_guard")

    def test_only_rule_stating_relations_are_indexed(self):
        index = SourceFactIndex(self.source_map)
        self.assertEqual(len(index), sum(
            relation.kind in {"service_guard", "service_entity", "service_parameter",
                              "request_handler", "form_target"}
            for relation in self.source_map.relations))

    def test_unshared_words_find_nothing(self):
        self.assertEqual(SourceFactIndex(self.source_map).search("销售订单锁定"), ())
        self.assertEqual(SourceFactIndex(replace(self.source_map, relations=[])).search("tray"), ())

    def test_hits_are_cited_and_bounded(self):
        evidence = self.source_map.relations[0].evidence[0]
        line = SourceHit(evidence=evidence, text="x " * 400).line
        self.assertTrue(line.startswith(
            f"[{evidence.path}:{evidence.start_line}-{evidence.end_line}] "))
        self.assertLessEqual(len(line), 400)

    def test_compiled_lookup_renders_the_refusal(self):
        lookup = CompiledSourceLookup(self.source_map)
        self.assertIsInstance(lookup, SourceLookup)
        hit = lookup.search("already sprouting", limit=2)[0]
        self.assertIn("refuses (RefusalError)", hit.text)
        self.assertIn("when tray.stage == 'sprouting' (for tray in self)", hit.text)

    def test_one_map_object_is_indexed_once(self):
        self.assertIs(fact_index(self.source_map), fact_index(self.source_map))
        self.assertIsNot(fact_index(self.source_map), fact_index(replace(self.source_map)))

    def test_a_long_fact_keeps_the_rarest_query_term(self):
        evidence = self.source_map.relations[0].evidence[0]
        text = ("method trays._stage(): Possible stages: "
                + "a tray tray tray stage. " * 30
                + "- dormant: only for a tray kept below ten degrees.")
        fitted = _fit(text, {"tray": 0.1, "stage": 0.2, "dormant": 5.0}, evidence)
        self.assertIn("dormant: only for a tray kept below ten degrees.", fitted)
        self.assertTrue(fitted.startswith("method trays._stage(): …"))

    def test_undocumented_method_uses_the_cited_body(self):
        evidence = replace(self.source_map.relations[0].evidence[0],
                           excerpt="def cancel(self):\n    drafts.cancel()\n")
        relation = SourceRelation(kind="service_parameter", subject="trays.cancel", object="",
                                  attrs={"doc": "", "decorators": []}, evidence=(evidence,))
        self.assertEqual(fact_text(SourceFact(relation, 1.0)),
                         "method trays.cancel(): drafts.cancel()")


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source_map = _with_guard(_map(Path(self.tmp.name)))
        self.capability = SourceCapability(source_map=self.source_map, runtime_source=True,
                                           source_affordance_mode="auto")

    def tearDown(self):
        self.tmp.cleanup()

    def test_control_keeps_release_and_settings_but_empties_compiled_facts(self):
        control = self.capability.control()
        self.assertEqual(control.source_map.release, self.source_map.release)
        self.assertEqual(control.source_map.relations, [])
        self.assertEqual((control.runtime_source, control.source_affordance_mode,
                          control.source_contract), (True, "auto", False))

    def test_alternative_lookup_must_share_the_release(self):
        evidence = self.source_map.relations[0].evidence[0]
        with self.assertRaisesRegex(ValueError, "same release|own release"):
            self.capability.control(lookup=_FixedLookup("another", evidence))
        with self.assertRaisesRegex(ValueError, "runtime source lookup"):
            SourceCapability(source_map=self.source_map, source_affordance_mode="auto",
                             lookup=_FixedLookup(self.source_map.release, evidence))
        with self.assertRaises(TypeError):
            SourceCapability(source_map=self.source_map, runtime_source=True, lookup=object())


class SourceAnswerTests(unittest.TestCase):
    def test_source_conclusion_is_only_exposed_for_source_enabled_sessions(self):
        self.assertEqual(answer_protocol.finish_answer_tool(),
                         answer_protocol.FINISH_ANSWER_TOOL)
        self.assertNotIn("source_excerpt", json.dumps(answer_protocol.FINISH_ANSWER_TOOL))
        self.assertIn("source_excerpt",
                      json.dumps(answer_protocol.finish_answer_tool(source=True)))
        self.assertIn("source_history",
                      json.dumps(answer_protocol.finish_answer_tool(source=True)))

    def test_source_evidence_is_minted_by_the_session_not_an_adapter(self):
        turn = TurnEvidence()
        with self.assertRaises(TypeError):
            turn.capture((_source_evidence(),), namespace="call1")
        turn.record(_source_evidence())
        self.assertFalse(turn.has_application_evidence())
        with self.assertRaisesRegex(ValueError, "source lookup"):
            SourceEvidence(
                evidence_id="source#2",
                origin=Origin(kind="typed", reference="r", observed_at="1", scope="s"),
                path="p", start_line=1, end_line=1, release="v0", sha256="a" * 64,
                excerpt="x")

    def test_source_excerpt_must_cite_source_and_is_quoted_verbatim(self):
        turn = TurnEvidence()
        turn.record(_source_evidence(excerpt="if x:\n    raise Y(```odd```)"))
        turn.record(QueryEvidence(
            evidence_id="call1-q#1",
            origin=Origin(kind="typed", reference="r", observed_at="1", scope="trays"),
            collection="trays", conditions=(), matched_count=0,
            covers_whole_scope=True))
        with self.assertRaisesRegex(ConclusionRefused, "source_excerpt.*is a query"):
            answer_protocol.submit(turn, {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete", "conclusions": [
                    {"kind": "source_excerpt", "evidence_ref": "call1-q#1"}]}})
        payload = answer_protocol.submit(turn, {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "conclusions": [
                {"kind": "source_excerpt", "evidence_ref": "source#1"}]}})
        text = render_payload(payload, user_text="托盘为什么不能再发芽？", max_chars=2000)
        self.assertIn("源码规则", text)
        self.assertIn("apps/greenhouse/rules.py:3-4", text)
        self.assertIn("部署版本源码中的规则，不是当前数据", text)
        self.assertIn("lines sha256:" + "a" * 64, text)
        self.assertIn("````\nif x:\n    raise Y(```odd```)\n````", text)
        english = render_payload(payload, user_text="Why can it not sprout?", max_chars=2000)
        self.assertIn("**Rule in the source** (apps/greenhouse/rules.py:3-4):", english)
        self.assertNotIn("（", english)

    def test_source_evidence_refuses_an_unsafe_path_or_unbounded_excerpt(self):
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            SourceEvidence(
                evidence_id="source#2",
                origin=Origin(kind="source", reference="lookup_source:q", observed_at="1",
                              scope="the deployed release v0"),
                path="../secret", start_line=1, end_line=1, release="v0",
                sha256="a" * 64, excerpt="x")
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            _source_evidence(excerpt="x" * (16 * 1024 + 1))

    def test_history_has_its_own_verified_answer_kind_and_limit(self):
        turn = TurnEvidence()
        turn.record(SourceHistoryEvidence(
            evidence_id="history#1",
            origin=Origin(kind="source", reference="lookup_source_history:blame",
                          observed_at="1", scope="deployed commit " + "b" * 40),
            release="v0", deployed_commit="b" * 40, history_kind="blame",
            statement="rules.py:3 is attributed to Fixture Author in aaaaaaaaaaaa",
            sha256="c" * 64, path="rules.py", start_line=3, end_line=3,
            commit="a" * 40))
        self.assertFalse(turn.has_application_evidence())
        payload = answer_protocol.submit(turn, {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "conclusions": [
                {"kind": "source_history", "evidence_ref": "history#1"}]}})
        text = render_payload(payload, user_text="谁改了这行？", max_chars=2000)
        self.assertIn("部署版本的 Git 历史", text)
        self.assertIn("不代表当前组织责任人", text)
        self.assertIn("git:", text)

    def test_runtime_history_is_evidence_or_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_map = _with_guard(_map(root))
            base = dict(
                application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "Who changed the refusal?"),
                lab=LabApparatus(arm="P"), chat=_Chat([]), browser=_Browser([_obs(1)]),
                control=_Control([]), ledger=SessionLedger(root / "history.jsonl", "history"))
            session = ServerAgentSession(**base, source=SourceCapability(
                source_map=source_map, runtime_source=True,
                history=_FixedHistory(source_map.release)))
            result = lookup_source_history(
                session, "blame", path="rules.py", start_line=3, end_line=3)
            self.assertTrue(result.startswith("history#1 [blame]"))
            self.assertIsInstance(session.evidence.get("history#1"), SourceHistoryEvidence)

            unavailable = ServerAgentSession(**{**base,
                "ledger": SessionLedger(root / "unavailable.jsonl", "unavailable")},
                source=SourceCapability(source_map=source_map, runtime_source=True,
                                        history_unavailable_reason="no .git in image"))
            self.assertEqual(
                "Source history unavailable: no .git in image",
                lookup_source_history(unavailable, "commits", path="rules.py"))

    def test_runtime_lookup_mints_one_stable_reference_for_exact_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_map = _with_guard(_map(root))
            session = ServerAgentSession(
                application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "Why can a tray not be sprouted twice?"),
                lab=LabApparatus(arm="P"), chat=_Chat([]),
                browser=_Browser([_obs(1)]), control=_Control([]),
                ledger=SessionLedger(root / "source.jsonl", "source"),
                source=SourceCapability(source_map=source_map, runtime_source=True))
            session.served_webapps = {"glasshouse"}

            first = lookup_source(session, "already sprouting tray", _obs(1))
            first_ids = tuple(ref for ref in session.evidence.ids()
                              if ref.startswith("source#"))
            second = lookup_source(session, "already sprouting tray", _obs(1))

        self.assertTrue(first.lines[0].startswith("source#1 ["))
        self.assertTrue(second.lines[0].startswith("source#1 ["))
        self.assertEqual(tuple(ref for ref in session.evidence.ids()
                               if ref.startswith("source#")), first_ids)
        held = session.evidence.get("source#1")
        self.assertEqual(held.release, source_map.release)
        self.assertEqual(held.sha256, first.refs[held.citation].sha256)
        self.assertTrue(session._answers.answer_mode)
        self.assertGreaterEqual(first.stats["facts_shown"], 1)


if __name__ == "__main__":
    unittest.main()
