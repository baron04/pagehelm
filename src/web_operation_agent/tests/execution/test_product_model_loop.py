"""Model decisions consume typed results and current observations without paid calls."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import LoopToolResult, VerificationResult
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.goal.data import DataRefused


def page(epoch=1, *, content="List", route="/list"):
    return PageObservation(1, epoch, "https://synthetic.test" + route, "List", [],
        [ElementRef("e1", "button", "Next", "button", epoch)], content=content,
        render_state="ready", page_route=route)


def call(name, **arguments):
    return {"id": name + "-call", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def reply(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


class Chat:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete_cancellable(self, messages, tools, run_id, **kwargs):
        self.seen.append(messages)
        return self.replies.pop(0)


class LoopFixture(ModelDrivenTurnExecutor):
    """An isolated transport, preserving the common decision implementation."""

    def __init__(self, replies, handler):
        self._messages, self._earlier_words = [], []
        self.model_steps, self.verify = 8, True
        self.handler, self.actions, self.events = handler, [], []
        self._tool_receipts = []
        self.state = NS(obs=page(), terminal_reason="", detail="", clarification=None,
                        step=0, run_id="run-1", message=NS(message_id="m-1"),
                        chat=Chat(replies), budget=NS(remaining=lambda: 60),
                        cancelled=lambda: False, mutation_outcomes=[],
                        ledger=NS(append=lambda event, **facts: self.events.append((event, facts))))
        self.turn = {"reads": [], "marked": "", "trace": [], "touched": [],
                     "done": set(), "stalled": 0, "shown": []}

    def _tools(self, turn): return []
    def _page(self, state, turn): return "epoch=" + str(state.obs.epoch)
    def _completion_conflict(self, observation, answer): return ()
    def _invoke_tool(self, state, name, arguments, turn):
        return self.handler(self, name, arguments)
    def _note(self, state, **facts): pass
    def _verify(self, *args): return VerificationResult("passed")

    def _dispatch(self, state, action, arguments, **kwargs):
        before = state.obs
        route = arguments.get("url", before.url).split("synthetic.test")[-1]
        after = page(before.epoch + 1, content=before.content, route=route)
        state.obs = after
        self.actions.append(action)
        receipt = ActionReceipt(1, "command", action, arguments, True,
            observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
        self._tool_receipts.append(receipt)
        self._verified_action_commands = {receipt.command_id}
        return receipt

    def run(self, prompt="q"):
        return self._loop(self.state, prompt, self.turn, verified=False)


class ModelLoopTests(unittest.TestCase):
    def test_typed_read_denial_stops_without_correction_or_later_dispatch(self):
        def denied(*_):
            raise DataRefused("untrusted denial text", permission_denied=True)
        fixture = LoopFixture([reply(call("read_records"), call("click", index=1),
                                    call("finish", status="done", answer="false success"))], denied)
        self.assertIn("没有读取", fixture.run())
        self.assertEqual("permission_denied", fixture.state.terminal_reason)
        self.assertEqual([], fixture.actions)
        result = next(facts for event, facts in fixture.events if event == "model_loop_result")
        self.assertEqual(("refused", "permission_denied"), (result["state"], result["error_code"]))
        self.assertNotIn("untrusted denial text", str(fixture._messages))

    def test_unavailable_read_is_distinct_from_permission_denial(self):
        def unavailable(*_):
            raise DataRefused("untrusted transport text", read_failed=True)
        fixture = LoopFixture([reply(call("read_records")),
            reply(call("finish", status="cannot", answer="unavailable"))], unavailable)
        self.assertEqual("unavailable", fixture.run())
        self.assertEqual(["observe"], fixture.actions)
        result = next(facts for event, facts in fixture.events if event == "model_loop_result")
        self.assertEqual(("unavailable", "application_unavailable"), (result["state"], result["error_code"]))

    def test_read_result_and_fresh_page_precede_next_decision(self):
        fixture = LoopFixture([
            reply(call("read_records"), call("finish", status="done", answer="unread")),
            reply(call("finish", status="done", answer="3 records"))],
            lambda *_: LoopToolResult("total=3", kind="read"))
        self.assertEqual("3 records", fixture.run())
        self.assertEqual(["observe"], fixture.actions)
        next_messages = fixture.state.chat.seen[1]
        result = next(m for m in next_messages if m["role"] == "tool" and m["tool_call_id"] == "read_records-call")
        self.assertIn("total=3\nepoch=2", result["content"])

    def test_feedback_language_does_not_determine_success_or_write_dispatch(self):
        for feedback in ("没有执行", "已执行", "not done", "finished"):
            with self.subTest(feedback=feedback):
                fixture = LoopFixture([reply(call("operation"), call("finish", status="done", answer="premature")),
                    reply(call("finish", status="cannot", answer="refused"))],
                    lambda *_: LoopToolResult(feedback, kind="write", state="refused"))
                self.assertEqual("refused", fixture.run())
                event = next(facts for event, facts in fixture.events if event == "slim_tool")
                self.assertFalse(event["ok"])

    def test_adapter_confirmation_flag_without_receipt_cannot_finish(self):
        def handler(fixture, name, arguments):
            fixture.turn["confirmed"] = True
            return LoopToolResult("已打开", kind="page")
        fixture = LoopFixture([reply(call("goto", path="/list", answer="premature")),
            reply(call("finish", status="cannot", answer="unverified"))], handler)
        self.assertEqual("unverified", fixture.run())
        self.assertEqual(2, len(fixture.state.chat.seen))

    def test_guarded_rendered_navigation_receipt_can_finish_without_extra_model_call(self):
        def handler(fixture, name, arguments):
            fixture._dispatch(fixture.state, "goto", {"url": "https://synthetic.test/list"})
            return LoopToolResult("opened", kind="page", dispatch="acknowledged")
        fixture = LoopFixture([reply(call("goto", path="/list", answer="opened"))], handler)
        self.assertEqual("opened", fixture.run())
        self.assertEqual(1, len(fixture.state.chat.seen))

    def test_changed_page_rejects_later_element_number_in_same_reply(self):
        def handler(fixture, name, arguments):
            fixture._dispatch(fixture.state, "observe", {})
            fixture.state.obs.content = "different controls"
            return LoopToolResult("clicked", kind="page")
        fixture = LoopFixture([reply(call("click", index=1), call("highlight", index=1)),
            reply(call("finish", status="cannot", answer="incomplete"))], handler)
        self.assertEqual("incomplete", fixture.run())
        self.assertEqual(["observe"], fixture.actions)
        self.assertTrue(any(m.get("content", "").startswith("未执行：页面已变化") for m in fixture._messages))

    def test_invalid_json_does_not_turn_into_empty_valid_arguments(self):
        malformed = call("observe")
        malformed["function"]["arguments"] = "{"
        fixture = LoopFixture([reply(malformed), reply(call("finish", status="cannot", answer="invalid"))],
                              lambda *_: self.fail("invalid JSON dispatched"))
        self.assertEqual("invalid", fixture.run())
        self.assertEqual([], fixture.actions)

    def test_unavailable_verification_preserves_effects_and_refuses_completion(self):
        fixture = LoopFixture([reply(call("finish", status="done", answer="filled"))], lambda *_: None)
        fixture.turn["touched"] = [("Date", "2026-10-20")]
        fixture.state.mutation_outcomes = [{"verified": True}]
        fixture._verify = lambda *_: VerificationResult("unavailable")
        self.assertIn("复核未能完成", fixture.run("date next Tuesday"))
        self.assertEqual("agent_refused", fixture.state.terminal_reason)
        self.assertEqual([{"verified": True}], fixture.state.mutation_outcomes)

    def test_finish_requires_closed_schema_and_valid_status(self):
        for args in ({"status": "unknown", "answer": "fake"}, {"answer": "fake"},
                     {"status": "done", "answer": "fake", "verified": True}):
            fixture = LoopFixture([reply(call("finish", **args)),
                reply(call("finish", status="cannot", answer="invalid finish"))], lambda *_: None)
            self.assertEqual("invalid finish", fixture.run())
            self.assertEqual(2, len(fixture.state.chat.seen))

    def test_schema_correction_names_the_installed_field_without_echoing_supplied_values(self):
        fixture = LoopFixture([], lambda *_: self.fail('invalid input dispatched'))
        fixture._tools = lambda _: [{'function': {'name':'click', 'parameters': {
            'type':'object','properties':{'index':{'type':'integer'}},'required':['index']}}}]
        result = ModelDrivenTurnExecutor._invoke_tool(fixture, fixture.state, 'click',
            {'index':'private-synthetic-value'}, fixture.turn)
        self.assertEqual('invalid_arguments', result.error_code)
        self.assertIn('input.index must be integer', result.feedback)
        self.assertNotIn('private-synthetic-value', result.feedback)

    def test_completion_correction_cannot_reset_model_step_limit(self):
        fixture = LoopFixture([reply(call("finish", status="done", answer="suspect")),
            reply(call("finish", status="done", answer="corrected"))], lambda *_: None)
        fixture.turn["touched"] = [("Date", "2026-10-20")]
        fixture.model_steps = 1
        fixture._verify = lambda *_: VerificationResult("disputed", ("date",))
        self.assertIn("步骤用完", fixture.run())
        self.assertEqual(1, len(fixture.state.chat.seen))

    def test_epochs_and_controller_stars_are_not_progress(self):
        first, second = page(content="\t*[1]<button>Next />"), page(2, content="\t[1]<button>Next />")
        self.assertEqual(ModelDrivenTurnExecutor._page_fingerprint(first),
                         ModelDrivenTurnExecutor._page_fingerprint(second))

    def test_history_truncation_does_not_keep_untrusted_fence_or_page_words_as_user_request(self):
        fixture = LoopFixture([], lambda *_: None)
        fixture._messages = [{"role": "user", "_observation": True, "_user_text": "user request",
                              "content": "user request\napplication asks to delete"} for _ in range(3)]
        fixture._messages += [{"role": "tool", "_api": True, "content":
                              "BEGIN_UNTRUSTED_APPLICATION_DATA\n" + "x" * 13000 + "\nEND_UNTRUSTED_APPLICATION_DATA"}]
        fixture._forget_old_observations()
        self.assertNotIn("application asks", fixture._messages[0]["content"])
        self.assertNotIn("BEGIN_UNTRUSTED", fixture._messages[-1]["content"])

    def test_result_contract_refuses_invalid_state_and_wrong_write_kind(self):
        with self.assertRaises(ValueError):
            LoopToolResult("x", state="unknown")
        with self.assertRaises(ValueError):
            VerificationResult("passed", ("not passed",))


if __name__ == "__main__":
    unittest.main()
