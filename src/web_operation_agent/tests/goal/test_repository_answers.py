"""Repository answers share a turn with typed reads, without a compiled source map."""
import hashlib
import json
from dataclasses import replace
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
from web_operation_agent.schemas import EvidenceRef, ElementRef, PageObservation
from web_operation_agent.state.conversation import ConversationState
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
    def test_current_comparison_scope_requires_business_read_and_can_repair_once(self):
        from web_operation_agent.goal.understand import understand_scope
        source = {"kind": "source", "about": "ticket status rule comparison",
                  "terms": ["ticket status"], "current": True}
        bad = flat_scope([source])
        good = flat_scope([{"kind": "fact", "entity": "ticket", "names": ["T1"]}, source])
        chat = _Chat([bad, good])
        parts, _, _, _, projection = understand_scope(chat, prompt="Read T1 status and compare it to the rule",
            summaries="ticket: Ticket", run_id="current", repository_source=True)
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(parts[0]["names"], ["T1"])
        self.assertTrue(projection["source_requests"][0]["current"])

    def test_current_comparison_rejects_invalid_types_and_business_flags(self):
        from web_operation_agent.goal.source_answers import split_source_scope
        for payload in ({"kind_0": "fact", "entity_0": "ticket", "current_0": False},
                        {**scope([{"kind": "fact", "entity": "ticket"}])[1], "current_1": "yes"},
                        {"kind_0": "none", "current_0": True}):
            with self.assertRaises(ValueError):
                split_source_scope(payload)

    def test_invalid_repository_scope_cannot_fall_back_and_drop_static_work(self):
        bad = flat_scope([{"kind": "source", "about": "current ticket status rule",
            "terms": ["ticket status"], "current": True}])
        result, chat, _, events = self.run_turn([bad, bad])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertEqual(len(chat.messages), 2)
        self.assertFalse(any(e['event'] in ('goal_resolved', 'goal_source_retrieved') for e in events))

    def test_comparison_renders_explicit_inference_bound_to_observed_facts(self):
        class RuleLookup(Lookup):
            def __init__(self):
                super().__init__()
                text = "Signing date must be present.\n"
                self.hit = SourceHit(EvidenceRef(repository_id="synthetic", release=self.release,
                    path="docs/rules.md", start_line=1, end_line=1, excerpt=text,
                    sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)
        declared = flat_scope([{"kind": "fact", "entity": "order", "names": ["SO1"]},
            {"kind": "source", "entity": "order", "about": "signing date rule comparison", "terms": ["signing date"], "current": True}])
        reply = ("answer_sources", {"complete_0": True, "statement_0": "The observed signing date is empty.",
            "references_0": ["source#1.1"], "assessment_0": "does_not_meet", "fact_indices_0": [0]})
        result, chat, _, events = self.run_turn([declared,
            ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "order", "record": "SO1",
                "fields": ["Signing date"]}]}), reply],
            [Resolved(kind="fact", facts=(Fact("SO1", "Signing date", "(empty)"),),
                empty=("Signing date",), repair="Other populated fields may answer the request")], lookup=RuleLookup())
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("Does not meet the requested rule dimensions", result.user_answer)
        event = next(e for e in events if e['event'] == 'goal_source_answer_verified')
        self.assertEqual(event['fact_indices'], [0])
        self.assertEqual(event['assessment'], 'does_not_meet')
        self.assertEqual(len(chat.messages), 4)
        self.assertFalse(any(e['event'] == 'goal_repair_requested' for e in events))

    def test_empty_comparison_observation_never_suppresses_missing_field_repair(self):
        from web_operation_agent.goal.executor import _repairable
        from web_operation_agent.goal.spec import GoalSpec, Deliverable
        spec = GoalSpec((Deliverable("fact", entity="ticket", fields=("Status",)),))
        empty = Resolved(kind="fact", facts=(Fact("T1", "Status", "(empty)"),),
            empty=("Status",), repair="Consider another field")
        self.assertEqual(_repairable(spec, goal_executor.Resolution((empty,))), ("Consider another field",))
        self.assertEqual(_repairable(spec, goal_executor.Resolution((empty,)),
            observed_empty_entities=frozenset({"ticket"})), ())
        from dataclasses import replace
        missing = replace(empty, problem="Missing field", missing_fields=("Owner",))
        self.assertEqual(_repairable(spec, goal_executor.Resolution((missing,)),
            observed_empty_entities=frozenset({"ticket"})), ("Consider another field",))

    def test_complete_comparison_cannot_bind_invented_or_missing_observations(self):
        from web_operation_agent.goal.source_answers import _verify_answers
        q = {"question_id": 0, "current": True, "evidence": []}
        base = {"complete_0": True, "statement_0": "A rule", "references_0": [], "assessment_0": "meets"}
        for indices, facts in (([], ({"value": "0"},)), ([0], ()), ([1], ({"value": "0"},)),
                               ([True], ({"value": "0"},)), ([0.0], ({"value": "0"},))):
            with self.assertRaises(ValueError):
                _verify_answers({**base, "fact_indices_0": indices}, [q], None, facts)

    def test_source_basis_is_typed_and_preserved_without_an_extra_model_stage(self):
        from web_operation_agent.goal.source_answers import split_source_scope
        for basis in ("repository", "implementation"):
            payload = scope()[1]
            payload["basis_0"] = basis
            _, requests = split_source_scope(payload)
            self.assertEqual(requests[0]["basis"], basis)
            result, chat, _, events = self.run_turn([("declare_scope", payload), source_reply()])
            self.assertEqual(len(chat.messages), 3)
            for messages in chat.messages[1:]:
                body = json.loads(messages[1]["content"])
                self.assertEqual(body['questions'][0]['basis'], basis)
            self.assertEqual(next(e for e in events if e['event'] == 'goal_source_retrieved')['basis'], basis)

    def test_invalid_source_basis_and_basis_on_business_or_empty_slots_are_refused(self):
        from web_operation_agent.goal.source_answers import split_source_scope
        for malformed in ({**scope()[1], "basis_0": "execution"},
                          {"kind_0": "fact", "entity_0": "order", "basis_0": "repository"},
                          {"kind_0": "none", "basis_0": "repository"}):
            with self.assertRaises(ValueError):
                split_source_scope(malformed)

    def test_implementation_incompleteness_cannot_display_a_provisional_document_claim(self):
        payload = scope()[1]
        payload['basis_0'] = 'implementation'
        result, _, _, _ = self.run_turn([("declare_scope", payload),
            ("answer_sources", {"complete_0": False,
                "statement_0": "Documentation proves this code executed.",
                "references_0": ["source#1.1"]})])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("Documentation proves", result.user_answer)

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
                 staged=True, history=None, lookup=None, conversation=None,
                 prompt="Count orders and explain who maintains the interface code.", browser=_Browser):
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
                repository_answers=repository_answers, staged=staged,
                conversation_state=conversation)
            with patch.object(goal_executor, "RemoteBrowserClient", browser):
                result = exe.execute({"message_id": "test", "page_url": "http://127.0.0.1:8080/home",
                    "text": prompt})
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

    def test_flat_scope_preserves_an_entityless_page_alongside_source_work(self):
        from web_operation_agent.goal.source_answers import split_source_scope, source_scope_tool
        from web_operation_agent.goal.spec import DECLARE_SCOPE_TOOL
        business, sources = split_source_scope(scope([{"kind": "page", "page": "help"}])[1])
        self.assertEqual(business["parts"], [{"kind": "page", "page": "help"}])
        self.assertEqual(len(sources), 1)
        from copy import deepcopy
        tool = source_scope_tool(deepcopy(DECLARE_SCOPE_TOOL))
        self.assertEqual(tool["function"]["parameters"]["properties"]["page_0"]["maxLength"], 200)
        for payload in ({"kind_0": "source", "page_0": "help"},
                        {"kind_0": "fact", "page_0": "help"},
                        {"kind_0": "none", "page_0": "help"},
                        {"kind_0": "page", "page_0": True},
                        {"kind_0": "page", "page_0": "p" * 201}):
            with self.assertRaises(ValueError):
                split_source_scope(payload)

    def test_disabled_page_action_is_not_dispatched_or_reported_reached(self):
        result, _, _, events = self.run_turn([
            flat_scope([{"kind": "page", "entity": "order"}]),
            ("declare_goal", {"deliverables": [{"kind": "page", "entity": "order"}]})],
            [Resolved(kind="page", page=PagePlan("/orders", "Orders"))],
            disabled=frozenset({"goto"}))
        self.assertEqual(AgentTurnStatus.DECLINED, result.status)
        self.assertEqual("policy", result.reason_category)
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

    def test_source_followup_retains_ordinary_dialogue_and_current_request_scope(self):
        state = ConversationState()
        quoted = 'Who maintains the "Batch 7" interface, documented 2026-10-01 for synthetic-1?'
        cited = "Aurora owns Batch 7. docs/owners.md:1-1@synthetic-1 sha256:" + "a" * 64
        state.record(message_id="before", user_text=quoted, assistant_text=cited, status="finished")
        prompt = "Does that documented ownership prove my permission?"
        result, chat, _, events = self.run_turn([scope(), source_reply()],
            conversation=state, prompt=prompt)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(len(chat.messages), 3)
        for messages in chat.messages[1:]:
            body = json.loads(messages[1]["content"])
            self.assertEqual(body["untrusted_conversation"]["prior_turns"],
                [{"user": quoted, "assistant": cited}])
            self.assertEqual(body.get("REQUEST", body.get("QUESTION_TO_RESEARCH")), prompt)
            self.assertNotIn("CURRENT_FACTS", body)
            self.assertIn("untrusted", messages[0]["content"])
        self.assertEqual(sum(e["event"] == "goal_source_context_projected" for e in events), 2)

    def test_each_source_decision_receives_fresh_visible_page_text_only(self):
        class VisibleBrowser(_Browser):
            def _observation(self):
                obs = super()._observation()
                secret = ElementRef(ref="secret", role="textbox", name='Archive "Batch 7"',
                    tag="input", epoch=obs.epoch, attrs={"password": "never-send", "value": "private"})
                return replace(obs, title='Archive "Batch 7"', landmarks=["Batch 7"],
                    messages=["Current visible warning"], content=f"Visible archive button, epoch {obs.epoch}",
                    elements=[secret])
        result, chat, _, _ = self.run_turn([scope(), source_reply()],
            prompt="What does this archive button require?", browser=VisibleBrowser)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        first = json.loads(chat.messages[1][1]["content"])["untrusted_page"]
        second = json.loads(chat.messages[2][1]["content"])["untrusted_page"]
        self.assertGreater(second["epoch"], first["epoch"])
        self.assertEqual(second["content"], f"Visible archive button, epoch {second['epoch']}")
        self.assertEqual(second["title"], 'Archive "Batch 7"')
        self.assertEqual(second["control_names"], ['Archive "Batch 7"'])
        self.assertEqual(second["messages"], ["Current visible warning"])
        self.assertNotIn("never-send", json.dumps(second))
        self.assertNotIn("private", json.dumps(second))
        self.assertNotIn("attrs", json.dumps(second))

    def test_source_context_is_redacted_bounded_and_excludes_prior_facts(self):
        from web_operation_agent.goal.source_answers import conversation_context, page_context
        from web_operation_agent.privacy import (SessionHandoffPolicy,
            has_unredacted_sensitive_assignment)
        handoffs = SessionHandoffPolicy.extending({"launchKey"})
        secret = "password=generic-secret externalLoginKey=session-secret launchKey=custom-secret"
        context = {"prior_turns": [{"user": secret + "漢" * 2_000,
            "assistant": "token=assistant-secret " + "語" * 20_000,
            "prior_business_facts": {"Status": "STALE-FACT"}, "hidden": "NOT-CONTEXT"} for _ in range(10)],
            "pending_clarification": {"hidden": "NOT-CONTEXT"}}
        earlier = conversation_context(context, handoffs=handoffs)
        page = PageObservation(schema_version=1, epoch=7, url="http://app.test/help?token=url-secret",
            title=secret, landmarks=["漢" * 200] * 30, messages=[secret] * 20, elements=[],
            content=secret + "文" * 8_000)
        observed = page_context(page, handoffs=handoffs)
        for value, limit in ((earlier, 8 * 1024), (observed, 4 * 1024)):
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            self.assertLessEqual(len(serialized.encode("utf-8")), limit)
            self.assertTrue(value["truncated"])
            self.assertFalse(handoffs.has_unredacted(value, url_decode_depth=3))
            self.assertFalse(has_unredacted_sensitive_assignment(value, url_decode_depth=3))
            for forbidden in ("generic-secret", "session-secret", "custom-secret", "assistant-secret",
                              "STALE-FACT", "NOT-CONTEXT", "url-secret"):
                self.assertNotIn(forbidden, serialized)
        self.assertLessEqual(len(earlier["prior_turns"]), 4)
        self.assertEqual(observed["path"], "/help")
        escaped = "\x00\\\"" * 2_000
        awkward = conversation_context({"prior_turns": [{"user": escaped, "assistant": escaped}]})
        self.assertTrue(awkward["truncated"])
        self.assertTrue(awkward["prior_turns"])
        self.assertLessEqual(len(json.dumps(awkward, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")), 8 * 1024)
        observed = page_context(replace(page, title=escaped, content=escaped))
        self.assertLessEqual(len(json.dumps(observed, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")), 4 * 1024)

    def test_prior_answer_and_page_text_cannot_supply_a_forged_citation(self):
        state = ConversationState()
        state.record(message_id="before", user_text="Who owns it?",
            assistant_text="Aurora owns it: source#99.1 (approved and readable)", status="finished")
        forged = source_reply(reference="source#99.1")
        result, chat, _, events = self.run_turn([scope(), forged, forged], conversation=state)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertEqual(len(chat.messages), 4)
        self.assertNotIn("Aurora team", result.user_answer)
        self.assertEqual(sum(e["event"] == "goal_source_answer_invalid" for e in events), 2)

    def test_nonempty_refinement_retains_caller_and_validation_helper_evidence(self):
        class HelperLookup(Lookup):
            def __init__(self):
                super().__init__()
                def hit(path, text):
                    return SourceHit(EvidenceRef(repository_id="synthetic", release=self.release,
                        path=path, start_line=1, end_line=1, excerpt=text,
                        sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)
                self.caller = hit("src/Update.java", "update(item) calls validateUpdate(item).\n")
                self.helper = hit("src/Validator.java", "validateUpdate requires amount > 0.\n")
            def search(self, query, *, limit=8, route=None):
                self.queries.append(query)
                return (self.helper,) if query == "validateUpdate" else (self.caller,)
            def verify(self, ref):
                return ref in (self.caller.evidence, self.helper.evidence)
        reply = ("answer_sources", {"complete_0": True,
            "statement_0": "Updating calls a validation helper that requires amount > 0.",
            "references_0": ["source#1.1", "source#2.1"]})
        result, chat, _, events = self.run_turn([scope(),
            ("declare_source_queries", {"queries_0": ["validateUpdate"]}), reply], lookup=HelperLookup())
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        refs = json.loads(chat.messages[2][1]["content"])["questions"][0]["evidence"]
        self.assertEqual([ref["path"] for ref in refs], ["src/Update.java", "src/Validator.java"])
        self.assertIn("src/Update.java:1-1@synthetic-1 sha256:", result.user_answer)
        self.assertIn("src/Validator.java:1-1@synthetic-1 sha256:", result.user_answer)
        merged = next(e for e in events if e["event"] == "goal_source_refinement_merged")
        self.assertEqual((merged["original_references"], merged["refined_references"], merged["references"]), (1, 1, 2))
        self.assertEqual(len(chat.messages), 3)

    def test_refinement_dedup_count_and_byte_limits_keep_both_searches_question_local(self):
        from web_operation_agent.goal.source_answers import retrieve, _merge_refinement, _verify_answers
        class Ledger:
            def __init__(self):
                self.events = []
            def append(self, event, **fields):
                self.events.append({"event": event, **fields})
        class DenseLookup(Lookup):
            def __init__(self, padding):
                super().__init__()
                def hit(kind, number):
                    text = f"{kind} validation rule {number}. " + "x" * padding
                    return SourceHit(EvidenceRef(repository_id="synthetic", release=self.release,
                        path=f"src/{kind}{number}.java", start_line=1, end_line=1, excerpt=text,
                        sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)
                self.caller = tuple(hit("Caller", i) for i in range(12))
                self.helper = (self.caller[0], *tuple(hit("Helper", i) for i in range(11)))
            def search(self, query, *, limit=8, route=None):
                values = self.helper if query.startswith("helper") else self.caller
                return values[6:] if query.endswith("b") else values[:6]
            def verify(self, ref):
                return any(ref == hit.evidence for hit in (*self.caller, *self.helper))
        for padding, expected_count in ((0, 12), (10_000, 6)):
            source = SourceCapability(runtime_source=True, lookup=DenseLookup(padding))
            requests = ({"question": "update validation", "queries": ("caller-a", "caller-b"),
                         "history": False},) * 2
            refined = tuple({**request, "queries": ("helper-a", "helper-b")} for request in requests)
            ledger = Ledger()
            previous, original = retrieve(source, requests, ledger)
            retrieved, helpers = retrieve(source, refined, ledger)
            merged, evidence = _merge_refinement(previous, original, retrieved, helpers, ledger)
            for qid, question in enumerate(merged["questions"]):
                refs = question["evidence"]
                self.assertEqual(len(refs), expected_count)
                self.assertTrue(question["evidence_truncated"])
                self.assertTrue(any("Caller" in ref["path"] for ref in refs))
                self.assertTrue(any("Helper" in ref["path"] for ref in refs))
                self.assertEqual(len({ref["path"] for ref in refs}), len(refs))
                self.assertEqual([ref["reference"] for ref in refs],
                    [f"source#{qid * 12 + i + 1}" for i in range(len(refs))])
                self.assertLessEqual(sum(len(evidence.get(ref["reference"]).excerpt.encode("utf-8"))
                    for ref in refs), 64 * 1024)
            # The valid caller/helper citation of question 0 cannot be borrowed by 1.
            forged = {"complete_0": False, "statement_0": "", "references_0": [],
                "complete_1": True, "statement_1": "The update has a rule.",
                "references_1": [merged["questions"][0]["evidence"][2]["supports"][0]["id"]]}
            with self.assertRaisesRegex(ValueError, "outside its question"):
                _verify_answers(forged, merged["questions"], evidence)

    def test_one_question_refinement_cannot_rebind_another_questions_kept_support(self):
        class ChangingRankLookup(Lookup):
            def __init__(self):
                super().__init__()
                self.ui_searches = 0
                def hit(path):
                    text = "Primary ownership document: " + path
                    return SourceHit(EvidenceRef(repository_id="synthetic", release=self.release,
                        path=path, start_line=1, end_line=1, excerpt=text,
                        sha256=hashlib.sha256(text.encode()).hexdigest()), text=text)
                self.first, self.selected, self.changed, self.helper = (
                    hit(path) for path in ("docs/first.md", "docs/selected.md",
                                          "docs/new-first.md", "src/Helper.java"))
            def search(self, query, *, limit=8, route=None):
                if query == "UI owner":
                    self.ui_searches += 1
                    return (self.first, self.selected) if self.ui_searches == 1 else (self.changed, self.first)
                return (self.helper,) if query == "helper" else (self.hit,)
            def verify(self, ref):
                return any(ref == hit.evidence for hit in (
                    self.hit, self.first, self.selected, self.changed, self.helper))
        declared = scope()
        declared[1].update(kind_1="source", about_1="UI owner", terms_1=["UI owner"])
        reply = source_reply()
        reply[1].update(complete_1=True, statement_1="The selected primary document identifies the UI owner.",
                        references_1=["source#14.1"])
        result, chat, lookup, events = self.run_turn([declared,
            ("declare_source_queries", {"queries_0": ["helper"], "queries_1": [],
                                        "keep_1": ["source#14"]}), reply], lookup=ChangingRankLookup())
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        refs = json.loads(chat.messages[2][1]["content"])["questions"][1]["evidence"]
        self.assertEqual([(ref["reference"], ref["path"]) for ref in refs],
                         [("source#14", "docs/selected.md")])
        self.assertNotIn("docs/new-first.md", result.user_answer)
        self.assertIn("docs/selected.md:1-1@synthetic-1 sha256:", result.user_answer)
        retained = next(event for event in events if event["event"] == "goal_source_unrefined_retained")
        selected = next(ref for ref in retained["references"] if ref["id"] == "source#14")
        self.assertEqual(selected["sha256"], lookup.selected.evidence.sha256)
        self.assertEqual(selected["release"], lookup.release)
        self.assertEqual(selected["path"], "docs/selected.md")

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
