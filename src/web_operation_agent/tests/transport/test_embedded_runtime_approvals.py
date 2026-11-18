from __future__ import annotations

from web_operation_agent.tests import browser_only

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from web_operation_agent.transport.gateway import EmbeddedGateway
from web_operation_agent.transport.panel_view import panel_contract
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.transport import (RemoteBrowserClient, RemoteSessionChannel,
                                        RemoteSessionRegistry, TransportError)
from web_operation_agent.schemas import ApprovalRequest
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.execution import _Chat, _Control
from web_operation_agent.tests.transport.test_embedded_runtime_boot import (
    PAGE_CONTROLLER,
    PRESENTATION,
    RUNTIME,
    _FixtureApplication,
    _panel_configuration_js,
    _runtime_bytes,
    _runtime_module_url,
)


class EmbeddedRuntimeApprovalTests(unittest.TestCase):
    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_confirmation_card_posts_authenticated_user_decision(self):
        """The whole chain, in a real browser: the server publishes a request, the panel
        renders the card, a person clicks it, and the decision reaches the session.

        The panel now lives in the host page's realm, so this no longer says the decision
        is unforgeable -- SECURITY.md says what it does and does not mean. What it still
        says is that an approval is answered by the page session that was shown it.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        if not RUNTIME.is_file():
            self.skipTest("assistant.js is not built; run scripts/build_runtime.py")

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(allowed_origins=[app.origin],
                                         session_id="approval-browser-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")
                channel.wait_for_client(5)
                page.locator(".woa-launcher").wait_for(timeout=8000)
                page.locator(".woa-launcher").click()
                request = ApprovalRequest(
                    1, "approval-browser", "Accept supplier quote",
                    ("Quote Q-1042",), int(time.time() * 1000) + 20_000)
                channel.request_approval(request)
                card = page.locator(".woa-approval")
                card.wait_for(timeout=8000)
                self.assertIn("Accept supplier quote", card.inner_text())
                self.assertIn("Quote Q-1042", card.inner_text())
                # The card is announced before either answer; Tab reaches cancellation first.
                page.wait_for_function(
                    "document.querySelector('#woa-assistant').shadowRoot"
                    ".activeElement?.classList.contains('woa-approval')")
                page.keyboard.press("Tab")
                self.assertEqual(
                    page.evaluate(
                        "document.querySelector('#woa-assistant').shadowRoot"
                        ".activeElement?.textContent"),
                    "取消")
                # The card must have been continuously visible before it can be approved;
                # a click that arrives sooner is refused on purpose.
                page.wait_for_timeout(700)
                page.get_by_test_id("woa-approve").click()
                decision = channel.wait_for_approval(request.approval_id, timeout_s=5)
                self.assertEqual(decision.decision, "approved")
                self.assertEqual(errors, [])
                browser.close()
        finally:
            gateway.close()
            app.close()


    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_approval_is_offered_once_and_replayed_without_asking_again(self):
        """The decision is recorded before it is dispatched, and a reload never re-asks.

        The panel now answers asynchronously -- the card is shown, the person decides, the
        Runtime posts -- so what has to hold is that arriving at the same request twice
        cannot produce two questions, and that a document which reloaded between deciding
        and posting finishes the post instead of asking again.
        """
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.sessionId = 'session-1';
          runtime.stored = {{approvals:{{}}}};
          let saves = 0;
          runtime._save = () => {{ saves += 1; }};
          const offered = [];
          runtime.panelHost = {{
            setConnection() {{}}, clearOptimisticOnce() {{}},
            snapshot() {{ return {{pendingApproval: null}}; }},
            setPendingApproval(request) {{ offered.push(request ? request.approval_id : null); }},
          }};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); return {{accepted:true}}; }};
          const payload = {{
            schema_version:1, approval_id:'approval-1', effect:'Update quote',
            targets:['Quote Q-1042'], expires_at:Date.now()+10000,
          }};
          const event = {{kind:'approval_required', payload}};

          await runtime._handleEvent(event);
          const beforeDecision = {{offered:[...offered], posts:posts.length, saves}};

          await runtime._postApproval(payload, 'approved');
          const afterDecision = {{posts:posts.length, saves, stored:{{...runtime.stored.approvals}}}};

          // A document that reloaded between deciding and posting replays the decision.
          await runtime._handleEvent(event);

          process.stdout.write(JSON.stringify({{
            beforeDecision, afterDecision, offered, posts,
            stored:runtime.stored.approvals,
          }}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        # Showing the card is not answering it: nothing is dispatched until a person decides.
        self.assertEqual(result["beforeDecision"], {"offered": ["approval-1"], "posts": 0, "saves": 0})
        self.assertEqual(result["afterDecision"]["stored"], {"approval-1": "approved"})
        self.assertEqual(result["afterDecision"]["saves"], 1)
        self.assertEqual(result["stored"], {"approval-1": "approved"})
        self.assertEqual(len(result["posts"]), 2)
        self.assertEqual(result["offered"].count("approval-1"), 1)
        for post in result["posts"]:
            self.assertEqual(post, {
                "path": "/approvals",
                "body": {"schema_version": 1, "session_id": "session-1",
                         "approval_id": "approval-1", "decision": "approved",
                         "principal_digest": "", "turn_id": "", "operation_id": ""},
            })


    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_a_session_with_no_panel_denies_rather_than_proceeding(self):
        """Nobody to ask means the write does not happen. Approval fails closed."""
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.sessionId = 'session-1';
          runtime.stored = {{approvals:{{}}}};
          runtime._save = () => {{}};
          runtime.panelHost = null;
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); return {{accepted:true}}; }};
          await runtime._handleEvent({{kind:'approval_required', payload:{{
            schema_version:1, approval_id:'approval-9', effect:'Create invoice',
            targets:['S00007'], expires_at:Date.now()+10000,
          }}}});
          process.stdout.write(JSON.stringify({{posts, stored:runtime.stored.approvals}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["stored"], {"approval-9": "denied"})
        self.assertEqual(result["posts"][0]["body"]["decision"], "denied")


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_highlight_hands_control_to_person_without_clicking(self):
        """The actual embedded Runtime marks a live control and removes the marker on handoff."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        if not RUNTIME.is_file():
            self.skipTest("assistant.js is not built; run scripts/build_runtime.py")

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(allowed_origins=[app.origin],
                                         session_id="highlight-browser-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")
                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                first = remote.observe()
                link = next(element for element in first.elements
                            if element.name == "Open order details")
                receipt = remote.execute("highlight", {"ref": link.ref})
                self.assertTrue(receipt.ok, receipt.error_detail)
                self.assertFalse(receipt.navigated)
                self.assertEqual(app.page2_requests, 0)
                self.assertIn("highlight", page.locator("#next").evaluate(
                    "element => element.style.outline").lower())

                # A real user gesture removes the marker without clicking the link.
                page.mouse.click(10, 10)
                page.wait_for_function("document.querySelector('#next').style.outline === ''")
                self.assertEqual(app.page2_requests, 0)

                refreshed_link = next(element for element in receipt.observation.elements
                                      if element.name == "Open order details")
                second = remote.execute("highlight", {"ref": refreshed_link.ref})
                self.assertTrue(second.ok, second.error_detail)
                page.locator(".woa-launcher").click()
                labels = channel._panel_configuration()["labels"]
                page.get_by_role("button", name=labels["collapse"]).click()
                page.wait_for_function("document.querySelector('#next').style.outline === ''")
                self.assertEqual(errors, [])
                browser.close()
        finally:
            gateway.close()
            app.close()


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_form_handoff_reads_value_then_returns_control_without_submit(self):
        """The server-only handoff checks the live DOM in actual Chromium."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        if not RUNTIME.is_file():
            self.skipTest("assistant.js is not built; run scripts/build_runtime.py")

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(allowed_origins=[app.origin],
                                         session_id="form-handoff-browser-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.goto(app.origin + "/form", wait_until="domcontentloaded")
                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                first = remote.observe()
                field = next(item for item in first.elements
                             if item.attrs.get("field") == "phone")
                filled = remote.execute("fill", {"ref": field.ref, "value": "new value"})
                self.assertTrue(filled.ok, filled.error_detail)
                fresh = next(item for item in filled.observation.elements
                             if item.attrs.get("field") == "phone")
                handed = remote.execute("handoff_form", {"fields": [{
                    "ref": fresh.ref, "field": "phone", "control": fresh.role,
                    "value": "new value"}]})
                self.assertTrue(handed.ok, handed.error_detail)
                self.assertEqual(handed.args_echo["fields"][0]["value"], "[redacted]")
                self.assertEqual(page.locator('[name="phone"]').input_value(), "new value")
                self.assertEqual(app.form_submissions, 0)
                page.locator('[name="phone"]').fill("person's value")
                self.assertEqual(page.locator('[name="phone"]').input_value(), "person's value")
                browser.close()
        finally:
            gateway.close()
            app.close()


    def _required_form_handoff(self, *, fill_email: bool, refuse_click: bool = False):
        """Use the real controller's references, including its disabled-button projection."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        _runtime_bytes()

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(allowed_origins=[app.origin])
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 800})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(app.origin + "/required-form", wait_until="domcontentloaded")
                    remote = RemoteBrowserClient(channel, timeout_s=8)
                    remote.start()
                    observation = remote.observe()

                    def submit_at(current):
                        candidates = [item for item in current.elements
                                      if item.role == "button" and item.name == "Save contact"]
                        self.assertEqual(len(candidates), 1)
                        self.assertEqual(candidates[0].attrs["form_scope"], "form:required-contact")
                        return candidates[0]

                    submit = submit_at(observation)
                    self.assertEqual(submit.attrs.get("disabled"), "true")
                    self.assertTrue(page.locator("#save-contact").is_disabled())
                    if refuse_click:
                        refused = remote.execute("click", {"ref": submit.ref})
                        self.assertFalse(refused.ok)
                        self.assertEqual(refused.error_code, "bad_arguments")
                        self.assertTrue(page.locator("#save-contact").is_disabled())
                    else:
                        values = {"name": "Ada Byron"}
                        if fill_email:
                            values["email"] = "ada@example.test"
                        for field_name, value in values.items():
                            field = next(item for item in observation.elements
                                         if item.attrs.get("field") == field_name)
                            filled = remote.execute("fill", {
                                "ref": field.ref, "value": value, "commit": True})
                            self.assertTrue(filled.ok, filled.error_detail)
                            observation = filled.observation
                            # The partial form must retain an actual indexed disabled target;
                            # completing the last required value makes that same control usable.
                            submit = submit_at(observation)
                            disabled = field_name == "name"
                            self.assertEqual(submit.attrs.get("disabled") == "true", disabled)
                            self.assertEqual(page.locator("#save-contact").is_disabled(), disabled)

                        fields = []
                        for field_name, value in values.items():
                            field = next(item for item in observation.elements
                                         if item.attrs.get("field") == field_name)
                            fields.append({"ref": field.ref, "field": field_name,
                                "control": field.role, "value": value,
                                "scope": field.attrs["form_scope"]})
                        handed = remote.execute("handoff_form", {
                            "fields": fields,
                            "submit": {"ref": submit.ref, "label": submit.name,
                                       "scope": submit.attrs["form_scope"]}})
                        self.assertTrue(handed.ok, handed.error_detail)
                        self.assertFalse(handed.navigated)
                        self.assertEqual(len(handed.args_echo["fields"]), len(values))
                        self.assertTrue(all(item["value"] == "[redacted]"
                                            for item in handed.args_echo["fields"]))
                        # The guide is separate from the application's button: handing
                        # over neither restyles its state nor activates it.
                        guide = page.locator(".woa-submit-guide")
                        self.assertEqual(guide.count(), 1)
                        self.assertEqual(guide.get_attribute("data-page-agent-not-interactive"), "true")
                        self.assertEqual(page.locator('[name="name"]').input_value(), "Ada Byron")
                        self.assertEqual(page.locator('[name="email"]').input_value(),
                                         "ada@example.test" if fill_email else "")
                        self.assertEqual(page.locator("#save-contact").is_disabled(), not fill_email)
                        self.assertEqual(submit_at(handed.observation).attrs.get("disabled") == "true",
                                         not fill_email)
                    self.assertEqual(page.evaluate("window.requiredFormEvents"),
                                     {"click": 0, "submit": 0})
                    self.assertEqual(app.form_submissions, 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            gateway.close()
            app.close()


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_partial_prefill_hands_over_an_observed_disabled_submit(self):
        self._required_form_handoff(fill_email=False)


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_complete_prefill_enables_submit_without_activating_it(self):
        self._required_form_handoff(fill_email=True)


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_observed_disabled_submit_cannot_be_clicked(self):
        self._required_form_handoff(fill_email=False, refuse_click=True)


if __name__ == "__main__":
    unittest.main()
