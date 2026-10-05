"""Second application structure with actual REST identity, plus embedded browser."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from testapps.contacts.app import ACTORS_BY_ID, DEMO_SESSION_COOKIE, MinimalContactsApplication
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.read_actions import ContactsReadActions
from web_operation_agent.execution.action_admission import ActionAdmission, ActionAdmissionRefused
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalError


def bind(application, actor_id="ada-lovelace"):
    actor = ACTORS_BY_ID[actor_id]
    adapter = MinimalContactsAdapter(application.origin, api_token=actor.token)
    identity = adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}")
    principal = adapter.identity.bind(identity, delegation_id="actions-test")
    return adapter, OperationContext.create(principal, session_id="session", turn_id="turn",
        operation_id="action", deadline_ms=int(time.time()*1000)+60_000)


class ContactsReadActionTests(unittest.TestCase):
    def test_plain_html_routes_use_same_core_without_source_or_orm(self):
        with MinimalContactsApplication() as app:
            adapter, context = bind(app)
            runtime = ActionAdmission(ContactsReadActions(adapter), adapter.descriptor.application_id)
            before = PageObservation(1, 1, app.origin + "/contacts", "Contacts", [], [
                ElementRef("e1", "link", "Help", "a", 1, {"href": "/contacts/help"})],
                render_state="ready", page_route="/contacts")
            intent, permit = runtime.prepare("click", {"ref": "e1"}, before,
                context=context, checkpoint=lambda: None)
            runtime.consume(intent, permit, before, context=context, checkpoint=lambda: None)
            after = replace(before, epoch=2, url=app.origin+"/contacts/help", elements=[], page_route="/contacts/help")
            receipt = ActionReceipt(1, "command", "click", {"ref": "e1", "epoch": 1}, True,
                url_before=before.url, url_after=after.url, observation=after, write_guard_held=True)
            runtime.verify(intent, permit, receipt, context=context, checkpoint=lambda: None)
            self.assertEqual(0, app.write_requests)
            _, foreign = bind(app, "rosalind-franklin")
            with self.assertRaises(PrincipalError):
                runtime.prepare("goto", {"url": "/contacts/help"}, before,
                    context=foreign, checkpoint=lambda: None)
            with self.assertRaises(ActionAdmissionRefused):
                runtime.prepare("goto", {"url": "/contacts/adversarial"}, before,
                    context=context, checkpoint=lambda: None)

    def test_native_host_without_an_effect_guard_refuses_before_model_or_browser_action(self):
        from playwright.sync_api import sync_playwright
        from testapps.contacts.demo import ScriptedDemoChat
        from testapps.contacts.server import NativeContactsHost, NativeServerConfig
        from testapps.contacts.tests.test_native_server import _CONNECTED, _SUBMIT, _chromium_launches
        if not _chromium_launches():
            if os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1":
                raise RuntimeError("Chromium is required for action admission qualification")
            self.skipTest("Chromium is unavailable")
        chat = ScriptedDemoChat((("set_target", {"path": "/contacts/help"}),
                                 ("goto", {"url": "/contacts/help"}),
                                 ("finish", {"outcome": "acted"})))
        with tempfile.TemporaryDirectory() as tmp, patch("testapps.contacts.server._chat", return_value=(chat, None)), \
                NativeContactsHost(NativeServerConfig(Path(tmp), port=0, scripted=False)) as host, \
                sync_playwright() as playwright:
            adapter, _ = bind(host.application)
            def config(**kwargs):
                return AgentTurnConfig(**{**kwargs, "click_policy": None,
                    "action_policy": ContactsReadActions(adapter)})
            with patch("testapps.contacts.server.AgentTurnConfig", side_effect=config):
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(host.origin + "/demo/login/ada-lovelace", wait_until="domcontentloaded")
                    page.evaluate(_CONNECTED)
                    page.get_by_role("link", name="New contact").wait_for(state="visible")
                    session = next(iter(host._sessions.values()))[0]
                    page.evaluate(_SUBMIT, "Open the workspace help page")
                    outcome = session.runner.next_outcome(timeout_s=10)
                    self.assertIsNotNone(outcome)
                    self.assertIn("requires a guarded principal", outcome.error)
                    self.assertIsNone(outcome.result)
                    self.assertEqual([], chat.seen)
                    self.assertEqual(host.origin + "/contacts", page.url)
                    self.assertEqual((0, 0), (host.application.write_requests, host.application.records_created))
                finally:
                    browser.close()
