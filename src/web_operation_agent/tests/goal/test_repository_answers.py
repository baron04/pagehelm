"""Repository answers share a turn with typed reads, without a compiled source map."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.ports import Aggregate, Fact, Resolved
from web_operation_agent.goal.ports import PagePlan
from web_operation_agent.planning.source_facts import SourceHit
from web_operation_agent.schemas import EvidenceRef
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.goal.test_goal_turn import _Adapter, _Browser, _Port
from web_operation_agent.tests.support.business import _Chat


class Lookup:
    release = "synthetic-1"
    surface_digest = "reviewed"
    def __init__(self):
        self.queries = []
        text = "Interface engineering owner: Aurora team.\n"
        self.hit = SourceHit(EvidenceRef(
            repository_id="synthetic", release=self.release, path="docs/owners.md",
            start_line=1, end_line=1, excerpt=text,
            sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)

    def search(self, query, *, limit=8, route=None):
        self.queries.append(query)
        return (self.hit,)

    def verify(self, ref):
        return ref == self.hit.evidence


def flat_scope(parts):
    return ("declare_scope", {f"{key}_{i}": value for i, part in enumerate(parts)
                              for key, value in part.items()})


def scope(parts=(), *, history=False):
    return flat_scope([*parts, {"kind": "source",
        "about": "Interface engineering owner", "terms": ["interface owner"],
        "history": history}])


def source_reply(*, reference="source#1.1"):
    return ("answer_sources", {"complete_0": True,
        "statement_0": "Aurora team maintains the interface code.",
        "references_0": [reference]})



class RepositoryAnswerTests(unittest.TestCase):
    def test_focus_preserves_original_question_terms_within_the_same_span_budget(self):
        from web_operation_agent.goal.source_answers import focus_preview
        spans = [{"text": "archive implementation filler", "id": "s1"},
                 {"text": "archive implementation more filler", "id": "s2"},
                 {"text": "deadline cannot precede signing date", "id": "s3"}]
        preview = focus_preview({"question_id": 0, "question": "deadline signing date constraints",
            "evidence": [{"reference": "source#1", "path": "docs/constraints.md", "start_line": 1,
                          "end_line": 3, "supports": spans}]}, ["archive implementation"])
        self.assertIn("deadline cannot precede signing date", preview["matches"][0]["matched_text"])
        self.assertEqual(len(preview["matches"][0]["matched_text"].splitlines()), 2)

    def run_turn(self, scripts, resolved=(), *, disabled=frozenset(), repository_answers=True,
                 staged=True, history=None, lookup=None):
        with tempfile.TemporaryDirectory() as tmp:
            expanded, focused = [], False
            for name, payload in scripts:
                if name == "declare_source_queries":
                    focused = True
                if name == "answer_sources" and not focused:
                    expanded.append(("declare_source_queries", {k.replace("complete_", "queries_"): []
                        for k in payload if k.startswith("complete_")}))
                    focused = True
                expanded.append((name, payload))
            lookup, chat = lookup or Lookup(), _Chat(expanded)
            ledger = Path(tmp) / "audit.jsonl"
            exe = GoalTurnExecutor(channel=_Channel(page_url="http://127.0.0.1:8080/home"),
                config=AgentTurnConfig(supported_scope="read", ledger_path=ledger,
                    source=SourceCapability(runtime_source=True, lookup=lookup, history=history)),
                chat=chat, application=bind_application(_Adapter()), goal_port=_Port(list(resolved)),
                control=ReadOnlyControl(), run_id_factory=lambda _m: "repository-turn",
                readings=1, disabled_page_actions=disabled,
                repository_answers=repository_answers, staged=staged)
            with patch.object(goal_executor, "RemoteBrowserClient", _Browser):
                result = exe.execute({"message_id": "test", "page_url": "http://127.0.0.1:8080/home",
                    "text": "Count orders and explain who maintains the interface code."})
            return result, chat, lookup, [json.loads(l) for l in ledger.read_text().splitlines()]

    def test_source_only_uses_three_bounded_declarations_and_visible_exact_citation(self):
        result, chat, _, events = self.run_turn([scope(), source_reply()])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("Aurora team", result.user_answer)
        self.assertIn("docs/owners.md:1-1@synthetic-1 sha256:", result.user_answer)
        self.assertEqual(3, len(chat.messages))
        self.assertFalse(any(e["event"] == "goal_resolved" for e in events))
        self.assertEqual(0, result.agent.page_actions)

    def test_source_completeness_is_a_required_decision_not_a_prefilled_answer(self):
        result, chat, _, _ = self.run_turn([
            scope([{"kind": "aggregate", "entity": "order"}]),
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                                                 "metric": {"op": "count"}}]}), source_reply()],
            [Resolved(kind="aggregate", aggregate=Aggregate("All orders", 17))])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("17", result.user_answer)
        self.assertIn("Aurora team", result.user_answer)
        messages = next(m for m in chat.messages if m[0]["content"].startswith("Call answer_sources"))
        body = json.loads(messages[1]["content"])
        self.assertNotIn("required_completeness_fields", body)
        from web_operation_agent.goal.source_answers import answer_tool
        self.assertIn("complete_0", answer_tool(body["questions"])["function"]["parameters"]["required"])
        self.assertIn("questions", body)
        self.assertNotIn("complete", body["questions"][0])

    def test_source_aware_scope_does_not_assume_unlisted_records_are_resolved(self):
        from web_operation_agent.goal.understand import understand_scope
        for repository in (False, True):
            payload = ({"kind_0": "fact", "entity_0": "order"} if repository else
                       {"parts": [{"kind": "fact", "entity": "order"}]})
            chat = _Chat([("declare_scope", payload)])
            parts, *_ = understand_scope(chat, prompt="未列出的订单金额是多少？",
                summaries="order: Orders", run_id="unlisted", repository_source=repository)
            self.assertEqual(parts[0]["entity"], "order")
            system = chat.messages[0][0]["content"]
            self.assertIn("Only records explicitly listed under KNOWN REFERENCES", system)
            self.assertIn("cannot establish record nonexistence", system)

    def test_repository_answers_are_explicit_so_existing_single_goal_mode_still_works(self):
        result, chat, _, _ = self.run_turn([
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                                               "metric": {"op": "count"}}]})],
            [Resolved(kind="aggregate", aggregate=Aggregate("All orders", 17))],
            repository_answers=False, staged=False)
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertEqual(1, len(chat.messages))

    def test_flat_scope_preserves_source_and_every_business_deliverable(self):
        from web_operation_agent.goal.source_answers import split_source_scope
        business, sources = split_source_scope(scope([
            {"kind": "aggregate", "entity": "order"},
            {"kind": "page", "entity": "order", "about": "Order 10"}])[1])
        self.assertEqual(["aggregate", "page"], [p["kind"] for p in business["parts"]])
        self.assertEqual(1, len(sources))
        for malformed in ({"parts": []}, {"kind_0": "none", "about_0": "silently lost"},
                          {"kind_0": "none", "kind_1": "source"}):
            with self.assertRaises(ValueError):
                split_source_scope(malformed)

    def test_disabled_page_action_is_not_dispatched_or_reported_reached(self):
        result, _, _, events = self.run_turn([
            flat_scope([{"kind": "page", "entity": "order"}]),
            ("declare_goal", {"deliverables": [{"kind": "page", "entity": "order"}]})],
            [Resolved(kind="page", page=PagePlan("/orders", "Orders"))],
            disabled=frozenset({"goto"}))
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertFalse(any(e["event"] == "tool_call" and e["tool"] == "goto" for e in events))

    def test_mixed_source_and_count_preserve_both_results(self):
        result, _, _, _ = self.run_turn([
            scope([{"kind": "aggregate", "entity": "order"}]),
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                                               "metric": {"op": "count"}}]}), source_reply()],
            [Resolved(kind="aggregate", aggregate=Aggregate("All orders", 17))])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("17", result.user_answer)
        self.assertIn("Aurora team", result.user_answer)

    def test_mixed_source_receives_only_observed_choice_code_after_business_read(self):
        result, chat, _, events = self.run_turn([
            scope([{"kind": "fact", "entity": "order"}]),
            ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "order",
                "record": "SO1", "fields": ["Status"]}]}), source_reply()],
            [Resolved(kind="fact", facts=(Fact("SO1", "Status", "In review", "state", "1"),))])
        body = json.loads(next(m for m in chat.messages
            if m[0]["content"].startswith("Call answer_sources"))[1]["content"])
        self.assertEqual(body["CURRENT_FACTS"], [{"subject": "SO1", "label": "Status",
            "value": "In review", "field_key": "state", "native_value": "1"}])
        order = [e["event"] for e in events]
        self.assertLess(order.index("goal_resolved"), order.index("goal_source_retrieved"))
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("In review", result.user_answer)
        self.assertEqual(result.agent.page_actions, 0)

    def test_wrong_or_nonliteral_support_has_only_one_repair_and_still_fails_closed(self):
        for reply in (source_reply(reference="source#99"), source_reply(reference="source#1.999")):
            result, chat, _, _ = self.run_turn([scope(), reply, reply])
            self.assertEqual(AgentTurnStatus.REFUSED, result.status)
            self.assertNotIn("Aurora team", result.user_answer)
            self.assertEqual(4, len(chat.messages))

    def test_invalid_support_can_be_repaired_once_without_accepting_the_bad_claim(self):
        result, chat, _, events = self.run_turn([
            scope(), source_reply(reference="source#1.999"), source_reply()])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertEqual(4, len(chat.messages))
        self.assertEqual(1, sum(e["event"] == "goal_source_answer_invalid" for e in events))

    def test_stringified_arrays_keep_the_same_validation_and_bounds(self):
        declared = scope()
        declared[1]["terms_0"] = json.dumps(declared[1]["terms_0"])
        replied = source_reply()
        replied[1]["references_0"] = json.dumps(replied[1]["references_0"])
        result, _, _, _ = self.run_turn([declared, replied])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)

    def test_small_focus_retrieves_primary_source_before_answering(self):
        result, chat, lookup, events = self.run_turn([scope(),
            ("declare_source_queries", {"queries_0": ["Aurora engineering"]}), source_reply()])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertEqual(3, len(chat.messages))
        self.assertIn("Aurora engineering", lookup.queries)
        self.assertEqual(2, sum(e["event"] == "goal_source_retrieved" for e in events))
        self.assertIn("matched_text", chat.messages[1][-1]["content"])
        self.assertNotIn('"supports"', chat.messages[1][-1]["content"])

    def test_one_shared_repair_cannot_be_used_to_accept_provisional_claims(self):
        invalid = ("declare_source_queries", {"queries_0": [], "statement_0": "Unverified assertion"})
        result, chat, _, events = self.run_turn([scope(), invalid,
            ("declare_source_queries", {"queries_0": []}), source_reply(reference="invented")])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertEqual(4, len(chat.messages))
        self.assertNotIn("assertion", result.user_answer)
        self.assertEqual(2, sum(e["event"] == "goal_source_answer_invalid" for e in events))

    def test_empty_refinement_preserves_the_original_verified_document(self):
        class SparseLookup(Lookup):
            def search(self, query, *, limit=8, route=None):
                return () if query == "unknown symbol" else super().search(query, limit=limit, route=route)
        result, _, _, events = self.run_turn([scope(),
            ("declare_source_queries", {"queries_0": ["unknown symbol"]}), source_reply()],
            lookup=SparseLookup())
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("Aurora team", result.user_answer)
        self.assertEqual(1, sum(e["event"] == "goal_source_refinement_empty" for e in events))

    def test_focus_keeps_only_selected_primary_matches_and_rejects_invented_ids(self):
        class MultipleLookup(Lookup):
            def verify(self, ref):
                from dataclasses import replace
                return replace(ref, path=self.hit.evidence.path) == self.hit.evidence

            def search(self, query, *, limit=8, route=None):
                from dataclasses import replace
                second = SourceHit(replace(self.hit.evidence, path="docs/examples.md"), text=self.hit.text)
                return (self.hit, second)
        result, chat, _, _ = self.run_turn([scope(),
            ("declare_source_queries", {"queries_0": [], "keep_0": ["source#1"]}), source_reply()],
            lookup=MultipleLookup())
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertNotIn("docs/examples.md", chat.messages[2][-1]["content"])
        bad = ("declare_source_queries", {"queries_0": [], "keep_0": ["source#13"]})
        result, _, _, _ = self.run_turn([scope(), bad, bad])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)

    def test_whole_source_is_split_into_bounded_numbered_spans_without_omission(self):
        from types import SimpleNamespace
        from web_operation_agent.goal.source_answers import _support_spans
        text = "guard " + "x" * 250 + "\nthrow " + "y" * 950
        spans = _support_spans(SimpleNamespace(evidence_id="source#1", start_line=3, excerpt=text))
        self.assertTrue(all(8 <= len(span["text"]) <= 400 for span in spans))
        self.assertEqual("".join(text.split()), "".join("".join(s["text"].split()) for s in spans))
        self.assertEqual(3, spans[0]["start"])
        self.assertEqual(4, spans[-1]["end"])
        self.assertEqual([f"source#1.{i}" for i in range(1, len(spans)+1)], [s["id"] for s in spans])

    def test_scope_repair_gets_typed_feedback_without_accepting_broken_json(self):
        bad = ("declare_scope", {"parts": "[{broken]"})
        result, chat, _, _ = self.run_turn([bad, scope(), source_reply()])
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertIn("Your declaration was invalid", chat.messages[1][-1]["content"])

    def test_git_history_does_not_search_code_or_infer_business_owners(self):
        result, chat, lookup, _ = self.run_turn([scope(history=True)])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertIn("Git", result.user_answer)
        self.assertEqual([], lookup.queries)
        self.assertEqual(1, len(chat.messages))

    def test_configured_git_provider_is_not_falsely_reported_as_absent(self):
        class History:
            release = Lookup.release
            deployed_commit = "a" * 40
            def search(self, *args, **kwargs):
                raise AssertionError("goal mode does not query Git")
        result, chat, lookup, _ = self.run_turn([scope(history=True)], history=History())
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertIn("does not support Git", result.user_answer)
        self.assertNotIn("deployment does not provide", result.user_answer)
        self.assertEqual([], lookup.queries)
        self.assertEqual(1, len(chat.messages))

    def test_support_ids_reject_non_strings_and_unknown_old_protocol_fields(self):
        for reference in (True, {}, ["source#1.1"]):
            bad = source_reply(reference=reference)
            result, _, _, _ = self.run_turn([scope(), bad, bad])
            self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        bad = source_reply()
        bad[1]["ends_0"] = []
        result, _, _, _ = self.run_turn([scope(), bad, bad])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)

    def test_support_is_bounded_by_characters_not_an_arbitrary_line_count(self):
        from dataclasses import replace
        lookup = Lookup()
        text = "Interface engineering owner:\n" + "\n" * 7 + "Aurora team.\n"
        lookup.hit = SourceHit(replace(lookup.hit.evidence, end_line=9, excerpt=text,
            sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)
        result, _, _, events = self.run_turn([scope(), source_reply()], lookup=lookup)
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        claim = next(e for e in events if e["event"] == "goal_source_answer_verified")["claims"][0]
        self.assertEqual([1, 9], claim["support_lines"])
        self.assertNotIn("support", claim)

    def test_long_source_is_selectable_through_literal_bounded_spans(self):
        from dataclasses import replace
        lookup = Lookup()
        text = "Interface engineering owner: Aurora team. " * 12
        lookup.hit = SourceHit(replace(lookup.hit.evidence, excerpt=text,
            sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)
        result, _, _, _ = self.run_turn([scope(), source_reply()], lookup=lookup)
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)

    def test_source_support_cannot_be_taken_from_another_question(self):
        declared = scope()
        declared[1].update(kind_1="source", about_1="UI engineering owner", terms_1=["UI owner"])
        bad = source_reply()
        bad[1].update({k.replace("_0", "_1"): v for k, v in list(bad[1].items())})
        result, chat, _, _ = self.run_turn([declared, bad, bad])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertEqual(4, len(chat.messages))

    def test_about_assistant_advertises_source_only_when_explicitly_enabled(self):
        from web_operation_agent.goal.executor import assistant_answer
        self.assertIn("工程职责", assistant_answer(frozenset(), chinese=True, repository_answers=True))
        self.assertNotIn("工程职责", assistant_answer(frozenset(), chinese=True))

    def test_unknown_source_part_does_not_mark_completed_business_as_whole_success(self):
        result, _, _, _ = self.run_turn([
            scope([{"kind": "aggregate", "entity": "order"}], history=True),
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                                               "metric": {"op": "count"}}]})],
            [Resolved(kind="aggregate", aggregate=Aggregate("All orders", 17))])
        self.assertEqual(AgentTurnStatus.REFUSED, result.status)
        self.assertIn("17", result.user_answer)
        self.assertIn("Git", result.user_answer)


if __name__ == "__main__":
    unittest.main()
