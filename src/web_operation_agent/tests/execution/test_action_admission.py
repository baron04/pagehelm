"""Effects bind exact actor/page/arguments and cannot authorize business writes."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from web_operation_agent.execution.action_admission import (
    ActionAdmission, ActionAdmissionRefused, ActionEffect, ActionPermit)
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.goal.test_data_read_tools import context
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT


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

    def verify(self, permit, intent, receipt, *, context, checkpoint):
        self.verifications.append(intent)
        return "Next" in receipt.observation.selected_controls


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

    def test_consumption_is_required_before_verification(self):
        intent, permit = self.prepare()
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            self.runtime.verify(intent, permit, self.receipt(), context=self.context, checkpoint=lambda: None)

    def test_policy_requires_actor_and_replaces_legacy_click_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(supported_scope="Read.", ledger_path=Path(tmp) / "audit", action_policy=self.policy)
            with self.assertRaisesRegex(ValueError, "requires identity"):
                AgentTurnConfig(**config)


class GuardedBrowser(_Browser):
    def execute(self, action, args=None, expect_navigation=False):
        receipt = super().execute(action, args, expect_navigation=expect_navigation)
        receipt.write_guard_held = True
        receipt.url_after = receipt.observation.url
        return receipt


class EffectLoopTests(unittest.TestCase):
    def execute(self, policy, calls, after=None, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        browser = GuardedBrowser([page(), after or page(2, selected=True), page(3, selected=True)])
        chat = _Chat(calls)
        session = ServerAgentSession(task=TaskSpec("task", "Use the page controls"), chat=chat,
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
