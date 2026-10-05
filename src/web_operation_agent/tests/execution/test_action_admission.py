"""Effects bind exact actor/page/arguments and cannot authorize business writes."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from web_operation_agent.execution.action_admission import (
    ActionAdmission, ActionAdmissionRefused, ActionEffect, ActionPermit, ActionResultPending)
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.goal.test_data_read_tools import context
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT
from web_operation_agent.transport.protocol import _remote_args_echo


def page(epoch=1, path="/list", selected=False):
    return PageObservation(1, epoch, "http://app" + path, "List", [], [
        ElementRef("e1", "button", "Next", "button", epoch),
        ElementRef("e2", "textbox", "Search", "input", epoch)],
        render_state="ready", page_route=path, selected_controls=["Next"] if selected else [])


class Policy:
    application_id = "fixture.app"
    required_scope = "records:read"
    actions = frozenset({"click", "goto", "fill", "select"})

    def __init__(self, effect=ActionEffect.READ_ONLY):
        self.effect = effect
        self.admissions = []
        self.verifications = []

    def admit(self, intent, *, context, checkpoint):
        self.admissions.append(intent)
        return ActionPermit(intent.binding_digest, self.effect, "reviewed-handler-v1", "selected-next")

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        self.verifications.append(intent)
        return "Next" in observation.selected_controls


class ActionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.context = context(application="fixture.app")
        self.policy = Policy()
        self.runtime = ActionAdmission(self.policy, "fixture.app")
        self.before = page()

    def prepare(self, action="click", args=None):
        return self.runtime.prepare(action, args or {"ref": "e1"}, self.before,
            context=self.context, checkpoint=lambda: None)

    def receipt(self, action="click", args=None, **changes):
        return ActionReceipt(1, "command", action, args or {"ref": "e1", "epoch": 1}, True,
            url_before=self.before.url, url_after=page(2).url, observation=page(2, selected=True),
            write_guard_held=True, **changes)

    def test_exact_action_has_one_application_result_check(self):
        intent, permit = self.prepare()
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        self.runtime.verify(intent, permit, self.receipt(), context=self.context, checkpoint=lambda: None)
        self.assertEqual(1, len(self.policy.verifications))
        with self.assertRaisesRegex(ActionAdmissionRefused, "already_consumed"):
            self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)

    def test_application_effects_never_enable_raw_drafts_writes_or_unknowns(self):
        for effect in (ActionEffect.TRANSIENT_DRAFT, ActionEffect.PERSISTENT, ActionEffect.UNKNOWN):
            self.policy.effect = effect
            with self.subTest(effect=effect), self.assertRaisesRegex(ActionAdmissionRefused, "unsafe_effect"):
                self.prepare("fill", {"ref": "e2", "value": "Synthetic"})

    def test_changed_actor_page_target_or_permit_is_not_consumed(self):
        intent, permit = self.prepare()
        for changed_page, changed_context, changed_permit in (
                (page(2), self.context, permit),
                (replace(self.before, content="changed"), self.context, permit),
                (self.before, replace(self.context, principal=replace(self.context.principal,
                    actor_id="another-actor")), permit),
                (self.before, self.context, replace(permit, postcondition="something-else"))):
            with self.subTest(page=changed_page.epoch), self.assertRaises(ActionAdmissionRefused):
                self.runtime.consume(intent, changed_permit, changed_page,
                    context=changed_context, checkpoint=lambda: None)
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)

    def test_adapter_cannot_mutate_the_admission_snapshot(self):
        intent, _ = self.prepare()
        intent.observation.elements[0].attrs["handler"] = "save"
        intent.arguments["ref"] = "another"
        self.assertEqual("e1", intent.arguments["ref"])
        self.assertEqual({}, intent.observation.elements[0].attrs)

    def test_malformed_extra_arguments_and_foreign_navigation_fail_before_policy(self):
        for action, args in (("submit", {"ref": "e1"}), ("click", {"ref": "absent"}),
                ("click", {"ref": "e1", "epoch": True}),
                ("fill", {"ref": "e2", "value": "x", "submit": True}),
                ("goto", {"url": "http://elsewhere/list"})):
            with self.subTest(action=action), self.assertRaises(ActionAdmissionRefused):
                self.prepare(action, args)
        self.assertEqual([], self.policy.admissions)

    def test_guard_or_dom_change_alone_cannot_prove_the_result(self):
        intent, permit = self.prepare()
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        good = self.receipt()
        variants = (replace(good, write_guard_held=False),
                    replace(good, observation=page(2)),
                    replace(good, observation=page(1, selected=True)),
                    replace(good, args_echo={"ref": "e2", "epoch": 1}),
                    replace(good, args_echo={"ref": "e1", "epoch": 2}),
                    replace(good, url_after="http://elsewhere/list"))
        for bad in variants:
            with self.subTest(receipt=bad.command_id), self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
                self.runtime.verify(intent, permit, bad, context=self.context, checkpoint=lambda: None)

    def test_redacted_input_echo_is_bound_to_the_command_and_requires_exact_application_value(self):
        value = "Synthetic filter"
        self.policy.verify = lambda permit, intent, receipt, *, observation, **kw: (
            observation.elements[1].attrs.get("value") == intent.arguments["value"])
        intent, permit = self.prepare("fill", {"ref": "e2", "value": value})
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        after = page(2)
        after.elements[1].attrs["value"] = value
        receipt = self.receipt("fill", _remote_args_echo({"ref": "e2", "epoch": 1, "value": value}))
        receipt.observation = after
        self.runtime.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        for bad in (replace(receipt, args_echo={**receipt.args_echo, "ref": "e1"}),
                    replace(receipt, args_echo={**receipt.args_echo, "value": value}),
                    replace(receipt, observation=page(3))):
            with self.subTest(echo=bad.args_echo), self.assertRaises(ActionAdmissionRefused):
                self.runtime.verify(intent, permit, bad, context=self.context, checkpoint=lambda: None)

    def test_consumption_is_required_before_verification(self):
        intent, permit = self.prepare()
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.runtime.verify(intent, permit, self.receipt(), context=self.context, checkpoint=lambda: None)

    def test_async_settlement_preserves_original_receipt_and_checks_fresh_guard(self):
        intent, permit = self.prepare()
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        original = replace(self.receipt(), observation=replace(page(2), render_state="pending"))
        original_json = original.to_json()
        with self.assertRaises(ActionResultPending):
            self.runtime.verify(intent, permit, original, context=self.context, checkpoint=lambda: None)
        self.assertEqual([], self.policy.verifications)
        after = page(3, selected=True)
        settled = ActionReceipt(1, "settlement", "observe", {}, True,
            url_before=original.url_after, url_after=after.url,
            observation=after, write_guard_held=True)
        for unsafe in (replace(settled, write_guard_held=False),
                       replace(settled, action="click"),
                       replace(settled, observation=page(2, selected=True)),
                       replace(settled, url_before="http://elsewhere/list"),
                       replace(settled, observation=replace(after, render_state="blocked"))):
            with self.subTest(action=unsafe.action), self.assertRaises(ActionAdmissionRefused):
                self.runtime.verify(intent, permit, original, context=self.context,
                                    checkpoint=lambda: None, settlement=unsafe)
        self.runtime.verify(intent, permit, original, context=self.context,
                            checkpoint=lambda: None, settlement=settled)
        self.assertEqual(original_json, original.to_json())
        self.assertEqual(1, len(self.policy.verifications))
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.runtime.verify(intent, permit, original, context=self.context,
                checkpoint=lambda: None, settlement=settled, minimum_epoch=3)

    def test_pending_result_does_not_hide_wrong_echo_or_actor(self):
        intent, permit = self.prepare()
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        original = replace(self.receipt(), observation=replace(page(2), render_state="pending"))
        for receipt, ctx in ((replace(original, args_echo={"ref": "e2", "epoch": 1}), self.context),
                (original, replace(self.context, principal=replace(self.context.principal,
                    actor_id="another-actor")))):
            with self.assertRaises(ActionAdmissionRefused):
                self.runtime.verify(intent, permit, receipt, context=ctx, checkpoint=lambda: None)

    def test_policy_requires_actor_and_replaces_legacy_click_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(supported_scope="Read.", ledger_path=Path(tmp) / "audit", action_policy=self.policy)
            with self.assertRaisesRegex(ValueError, "requires identity"):
                AgentTurnConfig(**config)


class GuardedBrowser(_Browser):
    navigated = True

    def execute(self, action, args=None, expect_navigation=False):
        receipt = super().execute(action, args, expect_navigation=expect_navigation)
        receipt.write_guard_held = True
        receipt.url_after = receipt.observation.url
        receipt.navigated = self.navigated
        return receipt


class EffectLoopTests(unittest.TestCase):
    def execute(self, policy, calls, after=None, navigated=True, before=None,
                prompt="Use the page controls", **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        browser = GuardedBrowser([before or page(), after or page(2, selected=True), page(3, selected=True)])
        browser.navigated = navigated
        chat = _Chat(calls)
        session = ServerAgentSession(task=TaskSpec("task", prompt), chat=chat,
            browser=browser, control=_Control([[] for _ in range(8)]),
            ledger=SessionLedger(Path(directory.name) / "audit", "run"),
            disabled_actions=frozenset(), action_policy=policy, page_write_guard=True,
            principal=context(application="fixture.app").principal,
            session_id="session", turn_id="turn", application_routes=FIXTURE_ROUTE_PORT,
            **kwargs)
        session.entry_observation = browser.pages[0]
        result = session.run()
        return result, session, browser, chat

    def test_newly_admitted_disabled_action_returns_to_same_agent(self):
        result, session, browser, chat = self.execute(Policy(), [
            ("click", {"ref": "e1"}), ("finish", {"done": False, "why": "stop"})])
        self.assertEqual(["click"], [n for n, _ in browser.done])
        self.assertEqual(1, len(session.ledger.of_kind("action_effect_verified")))
        self.assertEqual(2, len(chat.seen))
        tools = {t["function"]["name"] for t in session.tool_schemas()}
        self.assertIn("fill", tools)
        self.assertNotIn("submit", tools)

    def test_unsafe_effect_is_refused_without_browser_dispatch(self):
        _, session, browser, _ = self.execute(Policy(ActionEffect.PERSISTENT), [
            ("fill", {"ref": "e2", "value": "private synthetic"}),
            ("finish", {"done": False, "why": "stop"})])
        self.assertEqual(["observe"], [n for n, _ in browser.done])
        self.assertEqual("unsafe_effect", session.ledger.of_kind("action_effect_refused")[0]["error_code"])

    def test_verified_identical_rows_still_record_a_completed_read_interaction(self):
        policy = Policy()
        policy.verify = lambda permit, intent, receipt, *, observation, **kw: bool(
            observation.page_read and observation.page_read["revision"] == 2)
        after = replace(page(2), page_read={"path": "/api/records", "query_digest": "a" * 64,
            "query_fields": {}, "revision": 2, "page": 2, "page_size": 10, "rows": 10, "total": 20})
        _, session, browser, _ = self.execute(policy, [
            ("click", {"ref": "e1"}), ("finish", {"done": False, "why": "stop"})], after=after, navigated=False)
        self.assertEqual(["click"], [n for n, _ in browser.done])
        self.assertEqual({"kind": "page_interaction", "action": "click", "url": after.url},
                         session._answer_action_receipt)
        self.assertEqual(1, len(session.ledger.of_kind("action_effect_verified")))

    def test_unverified_acknowledged_action_is_not_repeated_or_reported_complete(self):
        result, session, browser, chat = self.execute(Policy(), [
            ("click", {"ref": "e1"}), ("click", {"ref": "e1"})], after=page(2))
        self.assertEqual("agent_refused", result.terminal_reason)
        self.assertEqual(["click"], [n for n, _ in browser.done])
        self.assertEqual(1, len(chat.seen))
        self.assertTrue(session.ledger.of_kind("action_effect_refused")[0]["dispatched"])

    def test_cancel_during_admission_prevents_dispatch_and_next_model_call(self):
        stopped = [False]
        policy = Policy()
        original = policy.admit
        def admit(*args, **kwargs):
            result = original(*args, **kwargs)
            stopped[0] = True
            return result
        policy.admit = admit
        result, _, browser, chat = self.execute(policy, [("click", {"ref": "e1"})],
                                                cancelled=lambda: stopped[0])
        self.assertEqual("user_cancelled", result.terminal_reason)
        self.assertEqual([], browser.done)
        self.assertEqual(1, len(chat.seen))

    def test_pending_result_returns_to_same_agent_without_repeating_the_action(self):
        result, session, browser, chat = self.execute(Policy(), [
            ("click", {"ref": "e1"}), ("click", {"ref": "e1"}),
            ("finish", {"done": True}), ("observe", {}),
            ("finish", {"done": True})], after=replace(page(2), render_state="pending"))
        self.assertEqual("agent_finished", result.terminal_reason)
        self.assertEqual(["click", "observe"], [n for n, _ in browser.done])
        self.assertEqual(1, len(session.ledger.of_kind("action_effect_pending")))
        self.assertEqual(1, len(session.ledger.of_kind("action_effect_verified")))
        self.assertEqual(2, len(session.ledger.of_kind("pending_action_tool_refused")))
        self.assertIsNone(session._pending_action)
        self.assertEqual(5, len(chat.seen))

    def test_pending_result_can_end_unable_and_cannot_claim_success(self):
        result, session, browser, _ = self.execute(Policy(), [
            ("click", {"ref": "e1"}), ("finish", {"done": False, "why": "cannot verify"})],
            after=replace(page(2), render_state="pending"))
        self.assertEqual("agent_refused", result.terminal_reason)
        self.assertEqual(["click"], [n for n, _ in browser.done])
        self.assertEqual([], session.ledger.of_kind("action_effect_verified"))

    def test_failed_async_postcondition_ends_without_another_click(self):
        policy = Policy()
        policy.verify = lambda *a, **k: False
        result, session, browser, chat = self.execute(policy, [
            ("click", {"ref": "e1"}), ("observe", {}), ("click", {"ref": "e1"})],
            after=replace(page(2), render_state="pending"))
        self.assertEqual("agent_refused", result.terminal_reason)
        self.assertEqual(["click", "observe"], [n for n, _ in browser.done])
        self.assertEqual(2, len(chat.seen))
        self.assertEqual([], session.ledger.of_kind("action_effect_verified"))

    def test_application_pending_on_ready_page_cannot_record_an_arrival(self):
        policy = Policy()
        def pending(*args, **kwargs):
            raise ActionResultPending()
        policy.verify = pending
        result, session, browser, _ = self.execute(policy, [
            ("goto", {"url": "/list"}), ("finish", {"done": True}),
            ("finish", {"done": False, "why": "cannot verify"})])
        self.assertEqual("agent_refused", result.terminal_reason)
        self.assertIsNone(session._answer_action_receipt)
        self.assertEqual(["goto"], [n for n, _ in browser.done])
        self.assertEqual(1, len(session.ledger.of_kind("pending_action_tool_refused")))
