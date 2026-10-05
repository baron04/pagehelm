"""Repository/API turns and later page tools share one lazily bound agent lifecycle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor
from web_operation_agent.execution import turn
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.source_history import SourceHistoryHit
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser, _message
from web_operation_agent.tests.support.business import _Chat, _Executor, _skill


class UnavailableBrowser(_RemoteBrowser):
    def start(self):
        raise AssertionError("a repository/API answer must not start browser transport")

    def observe(self):
        raise AssertionError("a repository/API answer must not observe a page")

    def execute(self, *args, **kwargs):
        raise AssertionError("a repository/API answer must not dispatch a page action")


class UnavailablePageControl(ReadOnlyControl):
    def begin_run(self, *args):
        raise AssertionError("no browser operation needs a page control lease")

    def outcome(self):
        raise AssertionError("no page control was acquired")

    def since(self, *args):
        raise AssertionError("no page trace exists")


class History:
    release = "fixture-1"
    deployed_commit = "a" * 40

    def search(self, kind, **kwargs):
        statement = "Order refund validation changed in commit " + self.deployed_commit
        return (SourceHistoryHit("fixture/repository", self.release, self.deployed_commit,
            kind, statement, hashlib.sha256(statement.encode()).hexdigest(),
            path="src/policy.py", commit=self.deployed_commit),)


class ProductDeferredContextTests(unittest.TestCase):
    def source(self, root, *, history=False, path="src/policy.py", text="batch_limit = 125\n"):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        lookup = RepositorySourceLookup(SourceTree({
            "repository_id": "fixture/repository", "release": "fixture-1",
            "allow_globs": ["src/**"], "deny_globs": ["**/.env*"],
        }, root))
        return SourceCapability(runtime_source=True, lookup=lookup,
                                history=History() if history else None)

    def answer(self, *, ref="source#1", kind="source_excerpt",
               statement="The declared batch limit is 125.", support="batch_limit = 125"):
        return {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "conclusions": [{
                "kind": kind, "evidence_ref": ref, "statement": statement, "support": support,
            }]}}

    def execute(self, root, calls, *, source=None, channel=None, browser=UnavailableBrowser,
                control=None, tools=None, steps=8):
        chat = _Chat(calls)
        channel = channel or _Channel(observation=False)
        executor = AgentTurnExecutor(channel=channel, chat=chat,
            control=control or UnavailablePageControl(), business_tools=tools,
            run_id_factory=lambda _m: "deferred-context",
            config=AgentTurnConfig(supported_scope="Read data and guide the user; never submit.",
                ledger_path=root / "audit.jsonl", defer_page_context=True, step_budget=steps,
                source=source or SourceCapability.none(), skill_first=False,
                plain_business_answer_min_page_actions=0))
        message = {**_message(), "text": "What batch limit does the code define?"}
        with patch.object(turn, "RemoteBrowserClient", browser):
            result = executor.execute(message)
        events = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
        return result, events, chat

    def test_source_answers_in_two_shapes_need_no_runtime_or_page_control(self):
        for path, text, query, statement, support in (
            ("src/policy.py", "batch_limit = 125\n", "batch limit",
             "The declared batch limit is 125.", "batch_limit = 125"),
            ("src/CustomerDetails.vue", '<el-tab-pane label="回款" />\n', "回款",
             "客户详情提供回款页签。", '<el-tab-pane label="回款" />'),
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result, events, chat = self.execute(root, [
                    ("lookup_source", {"query": query}),
                    ("finish", self.answer(statement=statement, support=support)),
                ], source=self.source(root, path=path, text=text))
                self.assertEqual("agent_finished", result.terminal_reason)
                self.assertIn(statement, result.user_answer)
                self.assertEqual(2, result.agent.model_receipt["model_requests"])
                self.assertEqual({}, result.agent.control)
                self.assertEqual([], [e for e in events if e["event"] == "action_receipt"])
                self.assertIn("No page has been observed", chat.messages[0][1]["content"])
                self.assertNotIn("CURRENT PAGE:", chat.messages[1][-1]["content"])

    def test_source_question_does_not_call_an_unavailable_business_backend(self):
        class UnavailableData(_Executor):
            def execute(self, *args, **kwargs):
                raise AssertionError("a static answer must not read business data")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = BusinessToolRuntime((_skill(root / "skill.json"),), UnavailableData())
            result, _, _ = self.execute(root, [
                ("lookup_source", {"query": "batch limit"}), ("finish", self.answer()),
            ], source=self.source(root), tools=tools)
            self.assertEqual("agent_finished", result.terminal_reason)

    def test_git_metadata_is_answered_and_audited_without_a_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = "Order refund validation changed in commit " + "a" * 40
            result, events, chat = self.execute(root, [
                ("lookup_source_history", {"kind": "commits", "path": "src/policy.py"}),
                ("finish", self.answer(kind="source_history", ref="history#1",
                    statement=statement, support=statement)),
            ], source=self.source(root, history=True))
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertIn(statement, result.user_answer)
            self.assertTrue(any(e["event"] == "runtime_source_history_retrieved" for e in events))
            self.assertIn("history#1", chat.messages[1][-1]["content"])

    def test_pure_business_read_needs_no_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = BusinessToolRuntime((_skill(root / "skill.json"),), _Executor())
            result, events, chat = self.execute(root, [
                ("query_companies", {}), (None, {"content": "Acme is available."}),
            ], tools=tools)
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertIn("Acme", result.user_answer)
            self.assertTrue(any(e["event"] == "business_tool_result" for e in events))
            self.assertIn("Acme", chat.messages[1][-1]["content"])

    def test_source_to_page_transition_keeps_the_same_agent_and_budget(self):
        class ArrivingBrowser(_RemoteBrowser):
            def start(self):
                self.channel.latest_observation_envelope = self.channel.envelope(
                    1, "http://app.test/companies")
                return super().start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, events, chat = self.execute(root, [
                ("lookup_source", {"query": "batch limit"}),
                ("observe", {}), ("finish", self.answer()),
            ], source=self.source(root), browser=ArrivingBrowser, control=ReadOnlyControl())
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertEqual(3, result.agent.model_receipt["model_requests"])
            self.assertTrue(any(e["event"] == "page_context_acquired" for e in events))
            self.assertIn("source#1", json.dumps(chat.messages[2]))
            self.assertIn("CURRENT PAGE:", chat.messages[2][-1]["content"])

    def test_first_guessed_element_is_not_dispatched(self):
        class ArrivingBrowser(_RemoteBrowser):
            def start(self):
                self.channel.latest_observation_envelope = self.channel.envelope(
                    1, "http://app.test/companies")
                return super().start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, events, _ = self.execute(root, [
                ("highlight", {"ref": "guessed"}), ("finish", {"outcome": "unable"}),
            ], browser=ArrivingBrowser, control=ReadOnlyControl())
            self.assertEqual("agent_refused", result.terminal_reason)
            self.assertEqual([], ArrivingBrowser.instances[-1].actions)
            event = next(e for e in events if e["event"] == "page_context_before_dispatch")
            self.assertFalse(event["dispatched"])

    def test_unknown_tool_does_not_start_browser_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, events, _ = self.execute(Path(tmp), [
                ("nonexistent", {}), ("finish", {"outcome": "unable"}),
            ])
            self.assertEqual("agent_refused", result.terminal_reason)
            self.assertTrue(any(e["event"] == "unknown_tool_rejected" for e in events))

    def test_cancel_during_source_read_settles_before_any_next_model_or_page_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = _Channel(observation=False)
            source = self.source(root)
            original = source.lookup.search
            def cancelled_search(*args, **kwargs):
                hits = original(*args, **kwargs)
                channel.cancelled = True
                return hits
            with patch.object(source.lookup, "search", side_effect=cancelled_search):
                result, events, chat = self.execute(root, [
                    ("lookup_source", {"query": "batch limit"}),
                ], channel=channel, source=source)
            self.assertEqual("user_cancelled", result.terminal_reason)
            self.assertEqual(1, len(chat.messages))
            self.assertIsNone(result.entry_observation_id)
            self.assertTrue(any(e["event"] == "run_finished" for e in events))

    def test_page_changed_before_lazy_binding_is_not_dispatched(self):
        class ChangedBrowser(_RemoteBrowser):
            def start(self):
                self.channel.latest_observation_envelope = self.channel.envelope(
                    2, "http://app.test/another-page")
                return super().start()
        with tempfile.TemporaryDirectory() as tmp:
            result, events, _ = self.execute(Path(tmp), [("observe", {})],
                browser=ChangedBrowser, control=ReadOnlyControl())
            self.assertEqual("transport_error", result.terminal_reason)
            self.assertEqual([], ChangedBrowser.instances[-1].actions)
            self.assertTrue(any(e["event"] == "run_finished" for e in events))

    def test_disabled_page_tool_does_not_start_browser_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, events, _ = self.execute(Path(tmp), [
                ("click", {"ref": "guessed"}), ("finish", {"outcome": "unable"}),
            ])
            self.assertEqual("agent_refused", result.terminal_reason)
            self.assertTrue(any(e["event"] == "unknown_tool_rejected" for e in events))

    def test_budget_exhaustion_and_cancellation_settle_without_page_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, events, _ = self.execute(root, [
                ("lookup_source", {"query": "batch limit"}), ("finish", self.answer()),
            ], source=self.source(root), steps=1)
            self.assertEqual("step_budget", result.terminal_reason)
            self.assertEqual(1, result.agent.model_receipt["model_requests"])
            self.assertTrue(any(e["event"] == "run_finished" for e in events))
        with tempfile.TemporaryDirectory() as tmp:
            channel = _Channel(observation=False, cancelled=True)
            result, events, _ = self.execute(Path(tmp), [], channel=channel)
            self.assertEqual("user_cancelled", result.terminal_reason)
            self.assertEqual(0, result.agent.model_receipt["model_requests"])
            self.assertTrue(any(e["event"] == "run_finished" for e in events))

    def test_flag_rejects_non_boolean_policy(self):
        with self.assertRaises(ValueError):
            AgentTurnConfig(supported_scope="Read only.", ledger_path=Path("unused.jsonl"),
                            defer_page_context="yes")


if __name__ == "__main__":
    unittest.main()
