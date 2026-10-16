"""Model decisions consume typed results and current observations without paid calls."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import LoopToolResult, VerificationResult
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.execution.receipt import TurnReceipt
from web_operation_agent.execution.action_admission import ActionPermit, ActionEffect, ActionAdmissionRefused
from web_operation_agent.tests.goal.test_data_read_tools import context


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
        channel = getattr(self, "channel", None)
        latest = (channel.latest_observation_envelope
                  if channel is not None else None)
        if action == "observe" and latest is not None:
            after = latest.observation
        else:
            route = arguments.get("url", before.url).split("synthetic.test")[-1]
            after = page(before.epoch + 1, content=before.content, route=route)
        state.obs = after
        self.actions.append(action)
        receipt = ActionReceipt(1, "command", action, arguments, True,
            observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
        self._tool_receipts.append(receipt)
        self._verified_action_commands = {receipt.command_id: json.dumps(after.to_json())}
        if channel is not None:
            channel.latest_observation_envelope = NS(observation=after)
        return receipt

    def run(self, prompt="q"):
        return self._loop(self.state, prompt, self.turn, verified=False)


class DispatchDiagnosticsTests(unittest.TestCase):
    def dispatch(self, *, refused=False):
        clock, events = [0.0], []
        def advance(seconds):
            clock[0] += seconds
        class Policy:
            application_id, required_scope, actions = "sample", "records:read", frozenset({"click"})
            def admit(self, intent, **kwargs):
                advance(.25)
                if refused:
                    raise ActionAdmissionRefused("unreviewed")
                return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY, "synthetic-review", "selected")
            def verify(self, *args, **kwargs):
                advance(.125)
                return True
        operation = context()
        def confirm(*args, **kwargs):
            advance(.5)
            return "", ""
        executor = ModelDrivenTurnExecutor.__new__(ModelDrivenTurnExecutor)
        executor.config = NS(action_policy=Policy())
        executor.profile = NS(draft_steps=None)
        executor.application = NS(descriptor=NS(application_id="sample"))
        executor.channel = NS(latest_observation_envelope=None)
        before = page()
        before.elements[0] = replace(before.elements[0], name="password=private-label",
                                     attrs={"value":"private-field-value"})
        state = NS(obs=before, terminal_reason="", detail="", step=0, run_id="run",
            cancelled=lambda:False, budget=NS(check=lambda:None), receipt=TurnReceipt(clock=lambda:clock[0]),
            approvals=NS(confirm_operation=confirm, context=lambda *a, **kw:operation, operation_hold_ms=1000),
            ledger=NS(append=lambda event, **facts:events.append((event, facts))))
        after = page(2)
        receipt = ActionReceipt(1, "command", "click", {"ref":"e1", "epoch":1}, True,
            observation=after, url_before=before.url, url_after=after.url, write_guard_held=True)
        def dispatch(*args, **kwargs):
            advance(.75)
            state.obs = after
            return receipt
        with patch.object(GoalTurnExecutor, "_dispatch", side_effect=dispatch) as browser:
            executor._dispatch(state, "click", {"ref":"e1"})
            result = executor._page_action_outcome
        return result, state.receipt.report(NS(), "run"), events, browser.call_count

    def test_action_phases_measure_approval_admission_dispatch_and_verification(self):
        result, report, events, dispatched = self.dispatch()
        self.assertEqual(("completed", "acknowledged", ""),
                         (result.state, result.dispatch, result.error_code))
        result.receipt.observation.title = "changed by consumer"
        self.assertNotEqual("changed by consumer", result.receipt.observation.title)
        self.assertEqual(1, dispatched)
        for name, seconds in (("action_approval", .5), ("action_admission", .25),
                              ("action_dispatch", .75), ("action_verification", .125)):
            self.assertEqual(seconds, report["stages"][name]["occupied_seconds"])
        target = next(facts["target"] for event, facts in events if event=="action_effect_consumed")
        self.assertEqual(("e1", "button", "button", 1),
                         (target["ref"], target["tag"], target["role"], target["observation_epoch"]))
        self.assertEqual(64, len(target["name_digest"]))
        for private in ("private-label", "private-field-value", "password"):
            self.assertNotIn(private, str(events)+str(report))

    def test_refusal_retains_structural_target_and_closes_timing_without_dispatch(self):
        result, report, events, dispatched = self.dispatch(refused=True)
        self.assertEqual(("refused", "not_dispatched", "unreviewed"),
                         (result.state, result.dispatch, result.error_code))
        self.assertIsNone(result.receipt)
        self.assertEqual(0, dispatched)
        self.assertNotIn("action_dispatch", report["stages"])
        self.assertEqual(.25, report["stages"]["action_admission"]["occupied_seconds"])
        refusal = next(facts for event, facts in events if event=="model_action_refused")
        self.assertEqual("unreviewed", refusal["error_code"])
        self.assertEqual("button", refusal["target"]["tag"])


class ModelLoopTests(unittest.TestCase):
    def test_manual_handoff_copy_requires_a_current_verified_runtime_handoff(self):
        for mode in ("combined", "separate_finish", "refused", "invented", "changed", "need_input"):
            with self.subTest(mode=mode):
                def tool(fixture, name, arguments):
                    if name == "later_read":
                        fixture.state.obs = page(4, route="/other")
                        return LoopToolResult("other page", kind="read")
                    fixture._dispatch(fixture.state, "handoff_form", {})
                    return LoopToolResult("ready", kind="page", marked="Save",
                        completion_command="invented" if mode == "invented" else "command",
                        state="unverified" if mode == "refused" else "completed")
                combined = mode == "combined"
                replies = [reply(call("prepare_draft", **({"answer": "Fields ready."} if combined else {})))]
                if mode == "changed":
                    replies.append(reply(call("later_read")))
                if not combined:
                    replies.append(reply(call("finish", status="need_input" if mode == "need_input" else "done",
                                              answer="Fields ready.")))
                fixture = LoopFixture(replies, tool)
                answer = fixture.run()
                self.assertTrue(answer.startswith("Fields ready."))
                valid = mode in {"combined", "separate_finish"}
                self.assertEqual(valid, "请检查表单" in answer)
                self.assertEqual(valid, "「Save」提交；助手未提交。" in answer)
                self.assertEqual(len(replies), len(fixture.state.chat.seen))

    def test_unrelated_searches_do_not_restart_unreviewed_control_recovery(self):
        attempted = []
        def tool(fixture, name, arguments):
            attempted.append(name)
            if name == "click":
                return LoopToolResult("handler unreviewed", kind="page", state="refused", error_code="unreviewed")
            fixture.state.obs.content = "new list read " + str(len(attempted))
            return LoopToolResult("list refreshed", kind="read")
        fixture = LoopFixture([reply(call("click", index=1)), reply(call("search_records")),
            reply(call("click", index=1)), reply(call("search_records")), reply(call("click", index=1)),
            reply(call("finish", status="cannot", answer="控件未审核"))], tool)
        self.assertIn("控件未审核", fixture.run())
        self.assertEqual("agent_refused", fixture.state.terminal_reason)
        self.assertEqual(2, attempted.count("click"))
        self.assertTrue(any("error_code=unreviewed" in m["content"] for m in fixture._messages
                            if m["role"] == "tool"))

    def test_re_admission_can_succeed_after_a_new_observation(self):
        attempted = []
        def tool(fixture, name, arguments):
            attempted.append(name)
            if name == "click" and attempted.count("click") == 1:
                return LoopToolResult("handler unreviewed", kind="page", state="refused", error_code="unreviewed")
            fixture.state.obs.content = "new read " + str(len(attempted))
            return LoopToolResult("ok", kind="read")
        fixture = LoopFixture([reply(call("click", index=1)), reply(call("search_records")),
            reply(call("click", index=1)), reply(call("finish", status="done", answer="recovered"))], tool)
        self.assertEqual("recovered", fixture.run())
        self.assertEqual(2, attempted.count("click"))
        self.assertFalse(fixture.turn.get("blocked_failures"))

    def test_six_distinct_page_failures_force_a_bounded_summary(self):
        attempted = []
        def tool(fixture, name, arguments):
            attempted.append(name)
            return LoopToolResult("cannot fill", kind="page", state="refused", error_code="invalid_target")
        replies = [reply(call("input", index=1, value=str(value))) for value in range(6)]
        replies.append(reply(call("finish", status="cannot", answer="六个字段都未能填写")))
        fixture = LoopFixture(replies, tool)
        self.assertIn("六个字段都未能填写", fixture.run())
        self.assertEqual(6, attempted.count("input"))
        self.assertEqual(6, fixture.turn["page_failures"])
        self.assertTrue(any("页面动作已失败六次" in message["content"]
                            for message in fixture.state.chat.seen[-1]))

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

    def test_consecutive_reads_share_one_fresh_observation(self):
        fixture = LoopFixture([
            reply(call("search_records"), call("read_records")),
            reply(call("finish", status="done", answer="3 records"))],
            lambda *_: LoopToolResult("total=3", kind="read"))
        self.assertEqual("3 records", fixture.run())
        self.assertEqual(["observe"], fixture.actions)
        first = next(m for m in fixture.state.chat.seen[1]
                     if m.get("tool_call_id") == "search_records-call")
        second = next(m for m in fixture.state.chat.seen[1]
                      if m.get("tool_call_id") == "read_records-call")
        self.assertIn("total=3\nepoch=2", first["content"])
        self.assertIn("当前页面观察与本次响应上一条完整观察相同", second["content"])
        self.assertNotIn("epoch=2\n", second["content"])

    def test_reads_across_model_responses_share_one_fresh_observation(self):
        fixture = LoopFixture([
            reply(call("read_one")),
            reply(call("read_two")),
            reply(call("finish", status="done", answer="3 records"))],
            lambda *_: LoopToolResult("total=3", kind="read"))
        fixture.channel = NS(latest_observation_envelope=NS(observation=page()))
        self.assertEqual("3 records", fixture.run())
        self.assertEqual(["observe"], fixture.actions)
        second = next(m for m in fixture.state.chat.seen[2]
                      if m.get("tool_call_id") == "read_two-call")
        self.assertIn("当前页面观察与本次响应上一条完整观察相同", second["content"])

    def test_external_page_change_invalidates_cross_response_observation_cache(self):
        fixture = LoopFixture([
            reply(call("read_one")),
            reply(call("read_two")),
            reply(call("finish", status="done", answer="3 records"))],
            lambda *_: LoopToolResult("total=3", kind="read"))
        fixture.channel = NS(latest_observation_envelope=NS(observation=page()))
        original = fixture.state.chat.complete_cancellable
        calls = 0

        def complete(messages, tools, run_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                fixture.channel.latest_observation_envelope = NS(
                    observation=page(3, content="changed controls"))
            return original(messages, tools, run_id, **kwargs)

        fixture.state.chat.complete_cancellable = complete
        self.assertEqual("3 records", fixture.run())
        self.assertEqual(["observe", "observe"], fixture.actions)
        second = next(m for m in fixture.state.chat.seen[2]
                      if m.get("tool_call_id") == "read_two-call")
        self.assertIn("epoch=3", second["content"])
        self.assertNotIn("当前页面观察与本次响应上一条完整观察相同", second["content"])

    def test_page_action_invalidates_read_observation_reuse(self):
        def handler(fixture, name, arguments):
            if name == "goto":
                fixture._dispatch(fixture.state, "goto", {"url": "https://synthetic.test/list"})
                return LoopToolResult("opened", kind="page")
            return LoopToolResult("total=3", kind="read")

        fixture = LoopFixture([
            reply(call("search_records"), call("goto", path="/list"), call("read_records")),
            reply(call("finish", status="done", answer="3 records"))], handler)
        self.assertEqual("3 records", fixture.run())
        self.assertEqual(["observe", "goto"], fixture.actions)
        read = next(m for m in fixture.state.chat.seen[1]
                    if m.get("tool_call_id") == "read_records-call")
        self.assertIn("total=3\n当前页面观察与本次响应上一条完整观察相同", read["content"])

    def test_reused_observation_does_not_hide_the_full_page_from_history_trim(self):
        fixture = LoopFixture([
            reply(call("read_one"), call("read_two"), call("read_three")),
            reply(call("finish", status="done", answer="3 records"))],
            lambda *_: LoopToolResult("total=3", kind="read"))
        self.assertEqual("3 records", fixture.run())
        next_messages = fixture.state.chat.seen[1]
        full = next(m for m in next_messages if m.get("tool_call_id") == "read_one-call")
        self.assertIn("epoch=2", full["content"])
        self.assertNotIn("更早的页面观察已省略", full["content"])

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

    def test_selected_tab_completion_requires_exact_guarded_verified_target(self):
        for mode in ("valid", "settled_pending", "ordinary_button", "unselected", "replaced", "wrong_ref",
                     "unverified", "unguarded", "later_route"):
            with self.subTest(mode=mode):
                tab = ElementRef("e1", "button" if mode == "ordinary_button" else "tab", "Details", "button", 1,
                                 {"id": "details-tab", "aria-controls": "details-panel", "aria-selected": "false"})
                def handler(fixture, name, arguments):
                    after = page(3)
                    after.elements = [replace(tab, ref="e8", epoch=3, attrs={**tab.attrs,
                        "id": "replacement" if mode == "replaced" else "details-tab",
                        "aria-selected": "false" if mode == "unselected" else "true"})]
                    fixture.state.obs = after
                    initial = replace(after, epoch=2, elements=[replace(e, epoch=2) for e in after.elements],
                                      page_route="", render_state="pending") if mode == "settled_pending" else after
                    receipt = ActionReceipt(1, "tab-command", "click",
                        {"ref": "e2" if mode == "wrong_ref" else "e1", "epoch": 1}, True,
                        observation=initial, url_before=after.url, url_after=after.url,
                        write_guard_held=mode != "unguarded")
                    fixture._tool_receipts.append(receipt)
                    fixture._verified_action_commands = ({} if mode == "unverified" else
                        {receipt.command_id: json.dumps(after.to_json())})
                    if mode == "later_route":
                        fixture.state.obs = replace(after, url="https://synthetic.test/other", page_route="/other")
                    return LoopToolResult("Selected", kind="page", dispatch="acknowledged")
                fixture = LoopFixture([reply(call("click", index=1, answer="Details selected")),
                    reply(call("finish", status="cannot", answer="not verified"))], handler)
                fixture.state.obs.elements = [tab]
                valid = mode in {"valid", "settled_pending"}
                self.assertEqual("Details selected" if valid else "not verified", fixture.run())
                self.assertEqual(1 if valid else 2, len(fixture.state.chat.seen))

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
