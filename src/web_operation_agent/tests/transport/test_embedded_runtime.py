from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.transport.gateway import EmbeddedGateway
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.transport import (RemoteBrowserClient, RemoteSessionChannel,
                                        RemoteSessionRegistry)
from web_operation_agent.schemas import ApprovalRequest
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.execution import _Chat, _Control


RUNTIME = Path(__file__).resolve().parents[2] / "web_runtime" / "assistant.js"
#: What only the browser side reads -- the stacking order among it -- is the frontend's.
PRESENTATION = json.loads((Path(__file__).resolve().parents[4] / "frontend" / "src"
                           / "presentation.json").read_text(encoding="utf-8"))
_PAGE_CONTROLLER_PATH = os.environ.get("WOA_PAGE_CONTROLLER_MODULE", "")
PAGE_CONTROLLER = Path(_PAGE_CONTROLLER_PATH).expanduser() if _PAGE_CONTROLLER_PATH else None


def _runtime_bytes() -> bytes:
    """`assistant.js` as built. It is a build artifact: a checkout that has
    not built it skips with the prerequisite named, rather than failing on a missing file."""
    if not RUNTIME.is_file():
        raise unittest.SkipTest("assistant.js is not built; run scripts/build_runtime.py")
    return RUNTIME.read_bytes()


def _runtime_module_url() -> str:
    """`assistant.js` as a module URL Node can import: one self-contained module, panel included.

    A file URL, not a data URL: with the panel inside it, the module is large enough that
    Node's data-URL decoder overflows its stack reading it.
    """
    _runtime_bytes()
    return RUNTIME.as_uri()


def _panel_configuration_js() -> str:
    """The real contract, not a second copy of it written into a test."""
    return json.dumps(RemoteSessionChannel(
        "configuration-probe", ("https://app.test",))._panel_configuration())


class EmbeddedRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute DOM freshness tests")
    def test_action_fingerprint_rejects_target_or_route_changes_only(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const h = mod.runtimeInternals;
          const pageLocation = {{href:'https://app.test/companies', origin:'https://app.test'}};
          globalThis.location = pageLocation;
          const form = {{action:'https://app.test/companies', method:'post', target:''}};
          const target = {{tagName:'BUTTON', type:'submit', name:'create', id:'new-company',
            role:'button', textContent:'Create', value:'', checked:false, disabled:false,
            readOnly:false, href:'', target:'', form, isConnected:true,
            getAttribute(name) {{ return this[name] || ''; }}}};
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.currentObservation = {{observation:{{url:pageLocation.href}}}};
          runtime.observedUrl = pageLocation.href;
          runtime.controller = {{selectorMap:{{}}}};
          runtime.getElementByIndex = () => target;
          runtime.observationFingerprints = new Map([
            ['e0', h.actionElementFingerprint(target)],
          ]);
          const command = {{action:'click', args:{{ref:'e0'}}}};
          const initial = runtime._actionFreshness(command);
          // A presentational change must not make an action stale. Freshness is decided by
          // the route and the target's identity, type, state and associated form, none of
          // which a class name touches, so this holds by construction rather than by luck.
          target.className = 'is-highlighted';
          const unrelatedMutation = runtime._actionFreshness(command);
          target.disabled = true;
          const targetMutation = runtime._actionFreshness(command);
          target.disabled = false;
          form.action = 'https://app.test/companies/import';
          const formMutation = runtime._actionFreshness(command);
          form.action = 'https://app.test/companies';
          pageLocation.href = 'https://app.test/settings';
          const routeMutation = runtime._actionFreshness(command);

          pageLocation.href = 'https://app.test/companies';
          target.disabled = true;
          runtime.runtimeInstanceId = 'runtime-freshness';
          runtime.panel = null;
          runtime.stopped = false;
          runtime.stored = {{inFlight:null, cachedResult:null}};
          runtime._save = () => {{}};
          runtime.currentObservation = {{session_id:'s', observation_id:'o-1', page_epoch:1,
            observation:{{url:pageLocation.href, elements:[]}}}};
          runtime.observe = async actionId => ({{session_id:'s', observation_id:'o-2',
            page_epoch:2, caused_by_action_id:actionId,
            observation:{{url:pageLocation.href, elements:[]}}}});
          let executed = 0;
          runtime._execute = async () => {{ executed += 1; return {{success:true}}; }};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); return {{claimed:true}}; }};
          await runtime._handleAction({{schema_version:1, session_id:'s', turn_id:'t',
            action_id:'a-stale', based_on_observation_id:'o-1', page_epoch:1,
            deadline:Date.now()+1000, command_digest:'0'.repeat(64),
            command:{{command_id:'a-stale', action:'click', args:{{ref:'e0'}}}}}});
          const staleDispatch = {{executed, paths:posts.map(row => row.path),
            status:posts.at(-1)?.body?.status, code:posts.at(-1)?.body?.error_code}};

          process.stdout.write(JSON.stringify({{initial, unrelatedMutation, targetMutation,
            formMutation, routeMutation, staleDispatch}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertTrue(result["initial"]["fresh"])
        self.assertTrue(result["unrelatedMutation"]["fresh"])
        self.assertFalse(result["targetMutation"]["fresh"])
        self.assertFalse(result["formMutation"]["fresh"])
        self.assertFalse(result["routeMutation"]["fresh"])
        self.assertEqual(result["staleDispatch"], {
            "executed": 0,
            "paths": ["/results"],
            "status": "rejected",
            "code": "stale_observation",
        })

    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_the_timeline_survives_a_full_page_navigation(self):
        """F8a: after a document navigation the timeline is replayed from the server.

        The old panel accumulated its own entries in the document that produced them, so a
        full navigation left the user staring at an empty panel while a task was still
        running. Nothing is cached locally now -- the panel asks `/timeline` and renders
        what the session says happened.
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
                                         session_id="navigation-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                errors = []
                document_loads = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("domcontentloaded", lambda: document_loads.append(page.url))
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")
                channel.wait_for_client(5)
                page.locator(".woa-launcher").click()
                page.locator("#woa-composer-input").fill("列出所有待开票的销售订单")
                page.keyboard.press("Enter")
                message = channel.wait_for_message(timeout_s=5)
                channel.publish("progress", {"state": "agent_started",
                                             "message": "正在检查当前页面"},
                                turn_id=message["message_id"])
                thread = page.locator(".woa-thread")
                thread.get_by_text("列出所有待开票的销售订单").wait_for(timeout=8000)
                thread.get_by_text("正在检查当前页面").wait_for(timeout=8000)

                # Cancelling is a panel-to-server message, not application navigation.
                # Keep the exact DOM object and document load count across the real button
                # gesture and terminal projection; a detach/re-attach can retain React state
                # and still produce the visible blink this test exists to catch.
                page.evaluate(
                    "window.__woaRootBeforeCancel = document.querySelector('#woa-assistant-root')")
                route_before_cancel = page.url
                labels = channel._panel_configuration()["labels"]
                page.get_by_role("button", name=labels["stop"]).click()
                deadline = time.monotonic() + 3
                while not channel.message_cancelled(message["message_id"]):
                    if time.monotonic() >= deadline:
                        self.fail("the real stop control did not cancel its active message")
                    time.sleep(0.01)
                channel.publish("progress", {
                    "state": "cancelled", "message": "已停止。",
                }, turn_id=message["message_id"])
                page.locator("#woa-composer-input").wait_for(state="visible", timeout=3000)
                self.assertEqual(page.url, route_before_cancel)
                self.assertEqual(len(document_loads), 1)
                self.assertTrue(page.evaluate(
                    "window.__woaRootBeforeCancel === "
                    "document.querySelector('#woa-assistant-root') && "
                    "window.__woaRootBeforeCancel.isConnected"))

                # A first-party SPA may remove an integration-owned root. The Runtime
                # reattaches the same mounted panel without losing its input, timeline or
                # authenticated browser lease.
                page.evaluate("document.querySelector('#woa-assistant-root').remove()")
                page.locator("#woa-assistant-root").wait_for(state="attached", timeout=3000)
                page.locator(".woa-window").wait_for(timeout=3000)
                thread.get_by_text("列出所有待开票的销售订单").wait_for(timeout=3000)
                self.assertTrue(page.locator("#woa-composer-input").is_enabled())

                # The stable overlay is an HTML child because the application owns body.
                # It is explicitly fixed and zero-sized, so it cannot become a flex/grid
                # item that steals width from the business application.
                self.assertEqual(page.evaluate(
                    "document.querySelector('#woa-assistant-root').parentElement.tagName"),
                    "HTML")
                self.assertTrue(page.evaluate(
                    "Array.from(document.documentElement.children, node => node.id)"
                    ".includes('woa-assistant-root')"))
                root_layout = page.evaluate("""() => {
                  const root = document.querySelector('#woa-assistant-root')
                  const style = getComputedStyle(root)
                  return {position: style.position, width: style.width, height: style.height,
                          pointerEvents: style.pointerEvents, zIndex: style.zIndex}
                }""")
                self.assertEqual(root_layout, {
                    "position": "fixed", "width": "0px", "height": "0px",
                    "pointerEvents": "none",
                    "zIndex": str(PRESENTATION["layout"]["overlay_z_index"]),
                })
                remained_connected = page.evaluate("""() => {
                  const root = document.querySelector('#woa-assistant-root')
                  document.body.replaceChildren(document.createElement('main'))
                  return root.isConnected && root.parentElement === document.documentElement
                }""")
                self.assertTrue(remained_connected)
                page.get_by_role("button", name=labels["collapse"]).click()
                page.locator(".woa-launcher").wait_for(timeout=3000)
                page.locator(".woa-launcher").click()
                page.locator(".woa-window").wait_for(timeout=3000)

                # A real, full document navigation: new document, new Runtime, new panel. The
                # panel's open state is not carried across documents (owner, P7.5), so the new
                # page shows the launcher; the timeline is behind it, replayed from the server.
                page.goto(app.origin + "/page2?source=navigation", wait_until="domcontentloaded")
                page.locator(".woa-launcher").click(timeout=8000)
                page.locator(".woa-window").wait_for(timeout=8000)
                replayed = page.locator(".woa-thread")
                replayed.get_by_text("列出所有待开票的销售订单").wait_for(timeout=8000)
                self.assertTrue(page.locator("#woa-composer-input").is_enabled())
                # Nothing about the timeline is kept in the document's own storage.
                stored = page.evaluate(
                    "Object.keys(window.sessionStorage).map(key => window.sessionStorage[key])"
                    ".join(' ')")
                self.assertNotIn("列出所有待开票的销售订单", stored)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            gateway.close()
            app.close()

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
                # Deny holds the initial focus: the safe direction is the default one.
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

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the panel host")
    def test_transport_failures_classify_once_against_the_server_table(self):
        """Terminal or retryable is decided in one place, from the server's own codes.

        Every reconnect loop used to carry its own list of codes to stop on. A 401 or a 404
        was in none of them, so an expired credential looked like a network fault and
        retried forever -- which is what the `embedded.js` 401/404/409 carry-over names.
        """
        host = _runtime_module_url()
        script = f"""
          const mod = await import('{host}');
          const configuration = {_panel_configuration_js()};
          const rows = [
            ['session closed', mod.classifyTransportError({{code:'session_closed'}}, configuration)],
            ['unauthorized', mod.classifyTransportError({{status:401}}, configuration)],
            ['unknown endpoint', mod.classifyTransportError({{status:404}}, configuration)],
            ['replaced', mod.classifyTransportError({{status:409}}, configuration)],
            ['forbidden', mod.classifyTransportError({{status:403}}, configuration)],
            ['unavailable', mod.classifyTransportError({{status:503}}, configuration)],
            ['offline', mod.classifyTransportError({{}}, configuration)],
          ];
          const delays = [];
          let previous = 0;
          for (let index = 0; index < 8; index += 1) {{
            previous = mod.backoffAfter(previous);
            delays.push(previous);
          }}
          process.stdout.write(JSON.stringify({{rows, delays}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        verdicts = dict(result["rows"])
        self.assertEqual([verdicts[name]["terminal"] for name in
                          ("session closed", "unauthorized", "unknown endpoint",
                           "replaced", "forbidden")], [True] * 5)
        self.assertFalse(verdicts["unavailable"]["terminal"])
        self.assertFalse(verdicts["offline"]["terminal"])
        self.assertEqual(verdicts["session closed"]["state"], "ended")
        self.assertEqual(verdicts["unknown endpoint"]["state"], "ended")
        self.assertEqual(verdicts["replaced"]["state"], "replaced")
        self.assertEqual(verdicts["unauthorized"]["state"], "error")
        self.assertTrue(all(not verdicts[name]["retryable"] for name in
                            ("session closed", "unauthorized", "replaced")))
        # Bounded, monotonically approaching the ceiling, and jittered so reconnecting tabs
        # do not return in lockstep.
        self.assertTrue(all(200 <= delay <= 4800 for delay in result["delays"]))
        self.assertGreater(result["delays"][4], result["delays"][0])
        self.assertGreater(len(set(result["delays"][4:])), 1)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the panel host")
    def test_the_local_echo_is_retired_by_the_server_own_acceptance(self):
        """A message shown optimistically must stop being shown twice.

        The echo exists because submitting needs a fresh observation first and can fail.
        It stops existing the moment the session's own `message_accepted` arrives.
        """
        host = _runtime_module_url()
        script = f"""
          const mod = await import('{host}');
          const configuration = {_panel_configuration_js()};
          const noop = () => {{}};
          const host = new mod.PanelHost({{configuration, actions:{{
            submit:noop, cancel:noop, decide:noop, newConversation:noop, loadOlder:noop}}}});
          host.setOptimistic({{turnId:'turn-1', text:'列出订单', failed:false}});
          host.applyTimeline({{entries:[{{id:'e1', sequence:1, turn_id:'other', at:0,
            kind:'progress', tone:'neutral', text:'x'}}], cursor:'e1', has_more:false}});
          const whileWaiting = host.snapshot().optimistic;
          host.applyTimeline({{entries:[{{id:'e2', sequence:2, turn_id:'turn-1', at:0,
            kind:'goal', tone:'neutral', text:'列出订单'}}], cursor:'e2', has_more:false}});
          const afterAcceptance = host.snapshot().optimistic;
          process.stdout.write(JSON.stringify({{whileWaiting, afterAcceptance}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["whileWaiting"],
                         {"turnId": "turn-1", "text": "列出订单", "failed": False})
        self.assertIsNone(result["afterAcceptance"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the panel host")
    def test_panel_host_merges_by_event_id_and_holds_a_render_budget(self):
        """The panel's timeline is the server's projection, merged idempotently."""
        host = _runtime_module_url()
        script = f"""
          const mod = await import('{host}');
          const configuration = {_panel_configuration_js()};
          const noop = () => {{}};
          const host = new mod.PanelHost({{configuration, actions:{{
            submit:noop, cancel:noop, decide:noop, newConversation:noop, loadOlder:noop}}}});
          const page = id => ({{entries:[{{id, sequence:1, turn_id:'t', kind:'progress',
            tone:'neutral', text:id, at:0}}], cursor:id, has_more:false}});
          host.applyTimeline(page('e1'));
          host.applyTimeline(page('e1'));
          host.applyTimeline(page('e2'));
          const merged = host.snapshot().entries.map(entry => entry.id);
          let protocolRefused = '';
          try {{
            new mod.PanelHost({{configuration:{{...configuration, protocol:99}}, actions:{{
              submit:noop, cancel:noop, decide:noop, newConversation:noop, loadOlder:noop}}}});
          }} catch (error) {{ protocolRefused = error.message; }}
          let actionsRefused = '';
          try {{ new mod.PanelHost({{configuration, actions:{{submit:noop}}}}); }}
          catch (error) {{ actionsRefused = error.message; }}
          process.stdout.write(JSON.stringify({{merged, protocolRefused, actionsRefused}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["merged"], ["e1", "e2"])
        self.assertIn("panel protocol", result["protocolRefused"])
        # The contract is checked where it is assembled, not probed for at each call.
        self.assertIn("missing cancel()", result["actionsRefused"])

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

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_first_party_shell_can_submit_through_the_same_panel_api(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          globalThis.location = {{href:'https://app.test/home'}};
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.stored = {{taskInFlight:false}};
          runtime._save = () => {{}};
          const statuses = [];
          runtime.panelHost = {{
            setConnection(patch) {{ if ('state' in patch) statuses.push(patch.state); }},
            setOptimistic() {{}}, failOptimistic() {{}}, clearOptimisticOnce() {{}}, raise() {{}},
            snapshot() {{ return {{pendingApproval: null}}; }}, setPendingApproval() {{}},
          }};
          const sent = [];
          let epoch = 0;
          runtime.observe = async () => ({{schema_version:1, session_id:'session-1',
            observation_id:`client:${{++epoch}}`, page_epoch:epoch,
            observation:{{url:globalThis.location.href}}}});
          runtime._post = async (path, body) => {{ sent.push({{path, body}}); return {{accepted:true}}; }};
          runtime.panelConfiguration = {_panel_configuration_js()};
          await runtime.submitTask('  Open deployment details  ');
          let overlapRejected = false;
          try {{ await runtime.submitTask('Open another page'); }} catch {{ overlapRejected = true; }}
          await runtime._handleEvent({{kind:'progress', payload:{{state:'finished', message:'done'}}}});
          globalThis.location.href = 'https://app.test/objects/notes';
          await runtime.submitTask('Open another page');
          process.stdout.write(JSON.stringify({{sent, overlapRejected,
            taskInFlight:runtime.stored.taskInFlight, statuses}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["sent"][0]["path"], "/observations")
        self.assertEqual(result["sent"][1], {
            "path": "/messages",
            "body": {
                "message_id": result["sent"][1]["body"]["message_id"],
                "text": "Open deployment details",
                "page_url": "https://app.test/home",
            },
        })
        self.assertTrue(result["sent"][1]["body"]["message_id"])
        self.assertEqual(result["sent"][3]["body"]["text"], "Open another page")
        self.assertEqual(result["sent"][3]["body"]["page_url"],
                         "https://app.test/objects/notes")
        self.assertEqual([row["path"] for row in result["sent"]],
                         ["/observations", "/messages", "/observations", "/messages"])
        self.assertTrue(result["overlapRejected"])
        self.assertTrue(result["taskInFlight"])
        self.assertIn("success", result["statuses"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_a_failed_startup_step_leaves_a_panel_that_says_so(self):
        """CR-43: the panel takes the session before the steps that can fail, not after them.

        The user reported "AI 助手入口看不到了" several times with a different cause each
        time. They shared one shape: startup threw somewhere between the hello and the
        panel, and because the panel was mounted last, the page ended up with no assistant
        at all -- no entry, no reason, nothing to press. A panel that cannot connect can
        still say so. The panel itself is up from the moment the script runs now; what must
        still come first is the session binding to it, so a failure becomes its state.
        """
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          globalThis.location = {{href:'https://app.test/home'}};
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.stored = {{taskInFlight:false}};
          runtime._save = () => {{}};
          runtime.panelConfiguration = {_panel_configuration_js()};
          const order = [];
          const states = [];
          const notices = [];
          runtime._hello = async () => {{ order.push('hello'); }};
          runtime._attachPanel = () => {{
            order.push('attach');
            runtime.panelHost = {{
              setConnection(patch) {{ if ('state' in patch) states.push(patch.state); }},
              raise(code) {{ notices.push(code); }},
              snapshot() {{ return {{pendingApproval: null}}; }},
            }};
          }};
          runtime.observe = async () => {{
            order.push('observe');
            const error = new Error('the server no longer has this session');
            error.status = 404;
            throw error;
          }};
          let threw = false;
          try {{ await runtime._startConnectedRuntime(); }} catch {{ threw = true; }}
          process.stdout.write(JSON.stringify({{order, states, notices, threw,
            mounted: Boolean(runtime.panelHost)}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["order"], ["hello", "attach", "observe"])
        self.assertTrue(result["mounted"], "the panel must exist once the server has spoken")
        self.assertTrue(result["threw"], "the caller still learns that startup failed")
        # And it is the panel that says which way: a session the server no longer has is
        # over, so the panel ends rather than spinning on a connection that will not come.
        self.assertEqual(result["states"], ["ended"])
        self.assertEqual(result["notices"], ["not_found"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_a_runtime_that_stops_for_good_frees_the_page_and_only_its_own_claim(self):
        """P7.5 J5.05: a stopped Runtime kept the page claimed, so the next start got `null`.

        The claim was released only when *starting* failed. After an agent restart ended a
        running session, the page stayed claimed by a Runtime that would never run again,
        and the loader's reconnection was handed `null` -- which it then watched, throwing
        into Odoo's error handler every two seconds.
        """
        source = _runtime_module_url()
        script = f"""
          globalThis.window = {{}};
          const mod = await import('{source}');
          const make = () => {{
            const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
            runtime.stopped = false;
            return runtime;
          }};
          const first = make();
          const other = make();
          window.__WOA_EMBEDDED_RUNTIME_STARTED__ = first;
          other.stop();
          const keptByOther = window.__WOA_EMBEDDED_RUNTIME_STARTED__ === first;
          first.stop({{removePanel: false}});
          const keptWhilePanelStays = window.__WOA_EMBEDDED_RUNTIME_STARTED__ === first;
          first.stop();
          process.stdout.write(JSON.stringify({{keptByOther, keptWhilePanelStays,
            released: window.__WOA_EMBEDDED_RUNTIME_STARTED__ === false}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        # Another Runtime stopping does not free a claim that is not its own.
        self.assertTrue(result["keptByOther"])
        # A Runtime that keeps its panel up is still the page's Runtime.
        self.assertTrue(result["keptWhilePanelStays"])
        # Stopped for good, it gives the page back.
        self.assertTrue(result["released"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_product_turn_terminal_states_release_the_panel(self):
        """Each terminal status maps through the server's table, not a copy of it here."""
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const configuration = {_panel_configuration_js()};
          const rows = [];
          for (const [state, mapping] of Object.entries(configuration.progress_states)) {{
            if (!mapping.terminal) continue;
            const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
            runtime.stored = {{taskInFlight:true}};
            runtime._save = () => {{}};
            runtime.panelConfiguration = configuration;
            let busy = true;
            let status = '';
            let cleared = false;
            runtime.panelHost = {{
              setConnection(patch) {{
                if ('busy' in patch) busy = patch.busy;
                if ('state' in patch) status = patch.state;
              }},
              clearOptimisticOnce() {{ cleared = true; }},
              // The real host answers both; a double that did not hid a call the runtime
              // makes when a turn ends.
              snapshot() {{ return {{pendingApproval: null}}; }},
              setPendingApproval() {{}},
            }};
            await runtime._handleEvent({{kind:'progress', payload:{{state, message:'done'}}}});
            rows.push({{state, status, busy, cleared,
              taskInFlight:runtime.stored.taskInFlight}});
          }}
          process.stdout.write(JSON.stringify(rows));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        rows = json.loads(proc.stdout)
        contract = RemoteSessionChannel("expectation-probe", ("https://app.test",))
        table = contract._panel_configuration()["progress_states"]
        self.assertEqual([row["status"] for row in rows],
                         [table[row["state"]]["panel_state"] for row in rows])
        self.assertTrue(all(row["busy"] is False for row in rows))
        self.assertTrue(all(row["cleared"] for row in rows))
        self.assertTrue(all(row["taskInFlight"] is False for row in rows))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_stop_posts_the_exact_active_message_and_waits_for_terminal(self):
        """Stopping is a request, not an outcome: the turn stays in flight until the server
        says how it ended. A pending approval is denied by the server on cancellation, not
        dismissed locally, so the card clears only once that denial is recorded."""
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.sessionId = 'session-1';
          runtime.panelConfiguration = {_panel_configuration_js()};
          runtime.stored = {{taskInFlight:true, activeMessageId:'message-7',
            cancellationPending:false}};
          let saves = 0;
          runtime._save = () => {{ saves += 1; }};
          const statuses = [];
          let stopping = false;
          let busy = true;
          runtime.panelHost = {{
            setConnection(patch) {{
              if ('state' in patch) statuses.push(patch.state);
              if ('busy' in patch) busy = patch.busy;
              if ('stopping' in patch) stopping = patch.stopping;
            }},
            clearOptimisticOnce() {{}},
            raise() {{}},
            snapshot() {{ return {{pendingApproval: null}}; }},
            setPendingApproval() {{}},
          }};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); }};
          await runtime.cancelTask();
          await runtime.cancelTask();
          const pending = {{busy, stopping,
            taskInFlight:runtime.stored.taskInFlight,
            activeMessageId:runtime.stored.activeMessageId,
            cancellationPending:runtime.stored.cancellationPending}};
          await runtime._handleEvent({{kind:'progress', payload:{{
            state:'cancelled', message:'Stopped'}}}});
          process.stdout.write(JSON.stringify({{posts, pending,
            statuses, saves, busy, stopping, stored:runtime.stored}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        # Exactly one cancellation for two presses: stop is idempotent per message.
        self.assertEqual(result["posts"], [{
            "path": "/cancellations",
            "body": {"schema_version": 1, "session_id": "session-1",
                     "message_id": "message-7"},
        }])
        self.assertEqual(result["pending"], {
            "busy": True, "stopping": True, "taskInFlight": True,
            "activeMessageId": "message-7", "cancellationPending": True})
        self.assertEqual(result["statuses"], ["stopping", "cancelled"])
        self.assertFalse(result["busy"])
        self.assertFalse(result["stopping"])
        self.assertFalse(result["stored"]["taskInFlight"])
        self.assertEqual(result["stored"]["activeMessageId"], "")
        self.assertFalse(result["stored"]["cancellationPending"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_pending_stop_retries_idempotently_after_runtime_restart(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.sessionId = 'session-1';
          runtime.stored = {{taskInFlight:true, activeMessageId:'message-retry',
            cancellationPending:true}};
          runtime._save = () => {{}};
          runtime.panel = {{cancelApproval() {{}}, status() {{}}, setStopping() {{}}, add() {{}}}};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); }};
          await runtime.cancelTask({{retry:true}});
          process.stdout.write(JSON.stringify({{posts, stored:runtime.stored}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["posts"], [{
            "path": "/cancellations",
            "body": {"schema_version": 1, "session_id": "session-1",
                     "message_id": "message-retry"},
        }])
        self.assertTrue(result["stored"]["cancellationPending"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_hello_echoes_only_the_server_supplied_principal_binding(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          globalThis.location = {{origin:'https://app.example.test'}};
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.sessionId = 'session-1';
          runtime.applicationId = 'sample';
          runtime.principalBinding = '{'a' * 64}';
          runtime.runtimeInstanceId = 'document-1';
          runtime.stored = {{clientId:'client-1', lastEventId:''}};
          runtime.leaseId = '';
          let sent;
          runtime._post = async (path, body, options) => {{
            sent = {{path, body, options}};
            return {{lease_id:'lease-1', panel:{{protocol:1, limits:{{}}, labels:{{}}}}}};
          }};
          runtime._armExpiry = () => {{}};
          await runtime._hello();
          process.stdout.write(JSON.stringify({{sent, leaseId:runtime.leaseId}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["sent"]["path"], "/hello")
        self.assertEqual(result["sent"]["body"]["application_id"], "sample")
        self.assertEqual(result["sent"]["body"]["principal_digest"], "a" * 64)
        self.assertEqual(result["sent"]["options"], {"includeLease": False})
        self.assertEqual(result["leaseId"], "lease-1")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_idle_runtime_accepts_only_same_session_principal_renewal(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.endpoint = 'https://agent.test';
          runtime.sessionId = 'session-1';
          runtime.token = 'token-1';
          runtime.applicationId = 'odoo';
          runtime.principalBinding = 'old';
          runtime.config = {{endpoint:'https://agent.test', sessionId:'session-1',
            clientToken:'token-1', sessionGeneration:'generation-1', applicationId:'odoo'}};
          runtime.stored = {{taskInFlight:false, inFlight:null}};
          runtime.panelHost = {{snapshot:() => ({{pendingApproval:null}})}};
          runtime.panelConfiguration = {{principal_expires_at:1, session_expires_at:1}};
          let armed = 0;
          runtime._armExpiry = () => {{ armed += 1; }};
          const fresh = Date.now() + 60_000;
          runtime.renewSession({{...runtime.config, principalBinding:'new',
            principalExpiresAt:fresh, sessionExpiresAt:fresh}});
          let changed = '';
          try {{
            runtime.renewSession({{...runtime.config, sessionId:'other',
              principalBinding:'newer', principalExpiresAt:fresh, sessionExpiresAt:fresh}});
          }} catch (error) {{ changed = error.message; }}
          runtime.stored.taskInFlight = true;
          let busy = '';
          try {{ runtime.renewSession({{...runtime.config}}); }}
          catch (error) {{ busy = error.message; }}
          process.stdout.write(JSON.stringify({{binding:runtime.principalBinding, armed,
            principal:runtime.panelConfiguration.principal_expires_at, changed, busy}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["binding"], "new")
        self.assertEqual(result["armed"], 1)
        self.assertGreater(result["principal"], int(time.time() * 1000))
        self.assertIn("changed sessionId", result["changed"])
        self.assertIn("idle runtime", result["busy"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_goto_uses_opt_in_spa_navigation_and_keeps_document_default(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const pageLocation = {{href:'https://app.test/start', origin:'https://app.test',
            assign(value) {{ assigned = value; this.href = value; }}}};
          globalThis.location = pageLocation;
          globalThis.window = new EventTarget();
          window.location = pageLocation;
          let assigned = '';
          let pushed = '';
          let pops = 0;
          globalThis.history = {{state:null, pushState(state, _title, value) {{
            pushed = value; this.state = state; pageLocation.href = value;
          }}}};
          globalThis.PopStateEvent = class extends Event {{
            constructor(type, init) {{ super(type, init); this.state = init?.state ?? null; }}
          }};
          let popState = 'never-fired';
          window.addEventListener('popstate', (event) => {{
            pops += 1; popState = event.state;
          }});

          const spa = Object.create(mod.EmbeddedWebRuntime.prototype);
          spa.config = {{navigationMode:'spa'}};
          const spaResult = await spa._execute(
            {{action:'goto', args:{{url:'/object/company/company-1'}}}});

          pageLocation.href = 'https://app.test/start';
          const documentRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          documentRuntime.config = {{navigationMode:'document'}};
          const documentResult = await documentRuntime._execute(
            {{action:'goto', args:{{url:'/settings/profile'}}}});
          process.stdout.write(JSON.stringify({{pushed, pops, popState, assigned,
            spaResult, documentResult}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["pushed"],
                         "https://app.test/object/company/company-1")
        self.assertEqual(result["pops"], 1)
        # A router is entitled to treat a stateless popstate as an anchor click and ignore
        # it. Odoo's does, so an event with no state moved the address bar while the page
        # still showed the previous view -- right URL, wrong page, no error anywhere.
        self.assertEqual(result["popState"], {"woaNavigation": True})
        self.assertEqual(result["assigned"], "https://app.test/settings/profile")
        self.assertEqual(result["spaResult"]["message"], "SPA navigation requested")
        self.assertEqual(result["documentResult"]["message"], "navigation requested")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the browser module helpers")
    def test_redaction_and_pagecontroller_index_adapter(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const h = mod.runtimeInternals;
          h.configureSensitiveQueryKeys([
            'externalLoginKey', 'externaLoginKey', 'externLoginKey', 'jsessionid']);
          const content = [
            '[0]<input type=password name=PASSWORD value=my super secret />',
            '[4]<a href=/next?externalLoginKey=abc123>Next />',
            '[5]<input type=text name=externalLoginKey value=field-secret />',
            '[7]<input type=text name=note value=public />',
            '[8]<button>First line\\nSecond line />',
            '[9]<input type=submit name=find value=Find />'
          ].join('\\n');
          const clean = h.sanitizePageContent(content);
          const elements = h.elementsFrom(clean, 9);
          const hydrated = h.hydrateLinkFacts(
            [{{ref:'e4', role:'link', name:'Next', tag:'a', epoch:9,
              attrs:{{href:'/deployments...'}}}}], {{}}, () => ({{
                getAttribute: name => name === 'href'
                  ? '/catalog/deployments/edge-api'
                  : (name === 'target' ? '_self' : null),
                hasAttribute: name => ['href', 'target'].includes(name),
              }}));
          const secretHydrated = h.hydrateLinkFacts(
            [{{ref:'e5', role:'link', name:'Callback', tag:'a', epoch:9, attrs:{{}}}}],
            {{}}, () => ({{
              getAttribute: name => name === 'href'
                ? '/callback?access_token=TOPSECRET&next=1' : null,
              hasAttribute: name => name === 'href',
            }}));
          const formHydrated = h.hydrateFormFacts([
            {{ref:'e7', role:'combobox', name:'Customer', tag:'input', epoch:9, attrs:{{}}}},
            {{ref:'e8', role:'textbox', name:'Password', tag:'input', epoch:9, attrs:{{}}}},
          ], {{}}, (_map, index) => ({{
            closest: () => ({{
              hasAttribute: name => name === 'name',
              getAttribute: name => name === 'name'
                ? (index === 7 ? 'customer_name' : 'password') : null,
            }}),
          }}));
          const encoded = h.redactString('/x?jsessionid%253Dabc%2526next=1');
          const generic = h.redactString('/callback?access_token=TOPSECRET&next=1');
          const encodedGeneric = h.redactString(
            '/callback?access_token%253DTOPSECRET%2526next%253D1');
          const encodedKeyGeneric = h.redactString(
            '/callback?access%255Ftoken%253DKEYSECRET%2526next%253D1');
          const encodedKeyHandoff = h.redactString(
            '/callback?external%254CoginKey%253DHANDOFFSECRET%2526next%253D1');
          const args = h.safeArgs({{ref:'e7', value:'private', nested:{{token:'abc'}}}});
          const linkWal = await h.navigationWal(
            'https://app.test/one', 'https://app.test/two', 'link');
          const submitWal = await h.navigationWal(
            'https://app.test/one', 'https://app.test/two', '');
          const reloadWal = await h.navigationWal(
            'https://app.test/one', 'https://app.test/one', 'link');
          const getWal = await h.navigationWal(
            'https://app.test/find', 'https://app.test/find?id=8003', 'form-get');
          const formWal = await h.navigationWal(
            'https://app.test/find', '', 'form');
          const recoveries = {{
            link: await h.recoveryProvesNavigation(
              {{...linkWal, phase:'claimed'}}, 'https://app.test/two'),
            prepared: await h.recoveryProvesNavigation(
              {{...linkWal, phase:'prepared'}}, 'https://app.test/two'),
            submit: await h.recoveryProvesNavigation(
              {{...submitWal, phase:'claimed'}}, 'https://app.test/two'),
            reload: await h.recoveryProvesNavigation(
              {{...reloadWal, phase:'claimed'}}, 'https://app.test/one'),
            formGet: await h.recoveryProvesNavigation(
              {{...getWal, phase:'claimed'}}, 'https://app.test/find?id=8003'),
            formDocument: await h.recoveryProvesNavigation(
              {{...formWal, phase:'claimed', document_id:'old'}},
              'https://app.test/find', 'new'),
            formSameDocument: await h.recoveryProvesNavigation(
              {{...formWal, phase:'claimed', document_id:'same'}},
              'https://app.test/find', 'same'),
          }};
          const terminalRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          terminalRuntime.stopped = false;
          terminalRuntime.stored = {{taskInFlight:true}};
          terminalRuntime._save = () => {{}};
          terminalRuntime.panel = {{add() {{}}, status() {{}}}};
          await terminalRuntime._handleEvent({{kind:'terminal', payload:{{reason:'done'}}}});
          process.stdout.write(JSON.stringify({{clean, elements, hydrated, secretHydrated,
            formHydrated,
            encoded, generic,
            encodedGeneric, encodedKeyGeneric, encodedKeyHandoff, args,
            recoveries, terminalStopped: terminalRuntime.stopped}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertNotIn("my super secret", result["clean"])
        self.assertNotIn("super secret", result["clean"])
        self.assertNotIn("abc123", result["clean"])
        self.assertNotIn("field-secret", result["clean"])
        self.assertIn("externalLoginKey=[redacted]", result["clean"])
        self.assertIn("jsessionid%253D[redacted]", result["encoded"])
        self.assertNotIn("TOPSECRET", result["generic"])
        self.assertIn("access_token=[redacted]", result["generic"])
        self.assertNotIn("TOPSECRET", result["encodedGeneric"])
        self.assertIn("access_token%253D[redacted]", result["encodedGeneric"])
        self.assertEqual(result["encodedKeyGeneric"], "[redacted]")
        self.assertEqual(result["encodedKeyHandoff"], "[redacted]")
        self.assertEqual([item["ref"] for item in result["elements"]],
                         ["e0", "e4", "e5", "e7", "e8", "e9"])
        self.assertEqual(result["elements"][1]["role"], "link")
        self.assertEqual(result["elements"][0]["epoch"], 9)
        self.assertEqual(result["hydrated"][0]["attrs"], {
            "href": "/catalog/deployments/edge-api", "target": "_self"})
        self.assertNotIn("TOPSECRET", json.dumps(result["secretHydrated"]))
        self.assertEqual(result["secretHydrated"][0]["attrs"]["href"],
                         "/callback?access_token=[redacted]&next=1")
        self.assertEqual(result["formHydrated"][0]["attrs"]["field"], "customer_name")
        self.assertNotIn("field", result["formHydrated"][1]["attrs"])
        self.assertEqual(result["args"]["value"], "[redacted]")
        self.assertEqual(result["args"]["nested"]["token"], "[redacted]")
        self.assertIn("First line", result["elements"][4]["name"])
        self.assertIn("Second line", result["elements"][4]["name"])
        self.assertEqual(result["elements"][5]["name"], "Find")
        self.assertEqual(result["recoveries"], {
            "link": True, "prepared": False, "submit": False, "reload": False,
            "formGet": True,
            "formDocument": True, "formSameDocument": False,
        })
        self.assertTrue(result["terminalStopped"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed for runtime fault injection")
    def test_runtime_recovery_security_and_deadline_faults(self):
        source = _runtime_module_url()
        script = """
          const mod = await import('__SOURCE__');
          const h = mod.runtimeInternals;

          const tokenWal = await h.navigationWal(
            'https://app.test/callback?access_token=START',
            'https://app.test/callback?access_token=EXPECTED', 'link');
          const tokenProof = {
            exact: await h.recoveryProvesNavigation(
              {...tokenWal, phase:'claimed'},
              'https://app.test/callback?access_token=EXPECTED'),
            different: await h.recoveryProvesNavigation(
              {...tokenWal, phase:'claimed'},
              'https://app.test/callback?access_token=DIFFERENT'),
            wire: JSON.stringify(tokenWal),
            hasRaw: 'started_url' in tokenWal || 'expected_url' in tokenWal,
          };
          const fragmentWal = await h.navigationWal(
            'https://app.test/orders#details',
            'https://app.test/orders#confirmation', 'link');
          tokenProof.fragmentDisplay = fragmentWal.started_url_display;

          const pageLocation = {href:'https://app.test/form', origin:'https://app.test'};
          globalThis.location = pageLocation;
          globalThis.window = {location:pageLocation,
            __WOA_EMBEDDED_RUNTIME_STARTED__:false};
          let inheritedTarget = '';
          globalThis.document = {querySelector: selector => selector === 'base[target]' &&
            inheritedTarget ? {getAttribute:() => inheritedTarget} : null};
          const submitRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          submitRuntime.controller = {selectorMap:{}};
          let submitted = 0;
          const submitCase = async form => {
            submitRuntime.getElementByIndex = () => form;
            try {
              await submitRuntime._execute({action:'submit', args:{ref:'e0'}});
              return '';
            } catch (error) { return String(error.message || error); }
          };
          const submitChecks = {
            crossOrigin: await submitCase({tagName:'FORM',
              action:'https://evil.test/collect', target:'', requestSubmit(){ submitted += 1; }}),
            newWindow: await submitCase({tagName:'FORM',
              action:'https://app.test/save', target:'_blank', requestSubmit(){ submitted += 1; }}),
            allowed: await submitCase({tagName:'FORM',
              action:'https://app.test/save', target:'_self', requestSubmit(){ submitted += 1; }}),
          };
          submitChecks.submitted = submitted;
          const submitEvidenceCase = async (prevented, method='post') => {
            const form = new EventTarget();
            Object.assign(form, {tagName:'FORM', action:'https://app.test/save',
              target:'', method});
            if (prevented) form.addEventListener('submit', event => event.preventDefault());
            form.requestSubmit = () => form.dispatchEvent(
              new Event('submit', {bubbles:true, cancelable:true}));
            submitRuntime.getElementByIndex = () => form;
            return submitRuntime._execute({action:'submit', args:{ref:'e0'}});
          };
          const submitEvidence = {
            native: await submitEvidenceCase(false),
            ajax: await submitEvidenceCase(true),
            dialog: await submitEvidenceCase(false, 'dialog'),
          };

          const clickRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          let clicked = 0;
          clickRuntime.controller = {selectorMap:{},
            clickElement(){ clicked += 1; return {success:true}; }};
          const clickElementCase = async element => {
            clickRuntime.getElementByIndex = () => element;
            try {
              await clickRuntime._execute({action:'click', args:{ref:'e0'}});
              return '';
            } catch (error) { return String(error.message || error); }
          };
          const clickChecks = {
            namedWindow: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'reportWindow'}),
            topFrame: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'_top'}),
            misleadingFormTarget: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'_blank', formtarget:'_self'}),
            allowed: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'_self'}),
          };
          inheritedTarget = '_blank';
          clickChecks.baseWindow = await clickElementCase({tagName:'A',
            href:'https://app.test/next'});
          inheritedTarget = '';
          const submitForm = {tagName:'FORM', action:'https://app.test/save', target:''};
          clickChecks.crossOriginFormAction = await clickElementCase({tagName:'BUTTON',
            type:'submit', form:submitForm, formaction:'https://evil.test/collect',
            formAction:'https://evil.test/collect'});
          clickChecks.formTargetWindow = await clickElementCase({tagName:'INPUT',
            type:'submit', form:submitForm, formtarget:'reportWindow',
            formTarget:'reportWindow'});
          const samePageSubmit = {tagName:'INPUT', type:'submit', form:submitForm,
            formtarget:'_self', formTarget:'_self'};
          clickChecks.submitAllowed = await clickElementCase(samePageSubmit);
          clickRuntime.getElementByIndex = () => samePageSubmit;
          clickChecks.submitClickWaitsWithoutHint = clickRuntime._mayNavigate(
            {action:'click', args:{ref:'e0'}, expect_navigation:false}, {observation:{}});
          clickChecks.explicitSubmitWaitsWithoutHint = clickRuntime._mayNavigate(
            {action:'submit', args:{ref:'e0'}, expect_navigation:false}, {observation:{}});
          clickChecks.submitWaitsWithHint = clickRuntime._mayNavigate(
            {action:'submit', args:{ref:'e0'}, expect_navigation:true}, {observation:{}});
          clickRuntime.getElementByIndex = () => ({tagName:'A',
            href:'https://app.test/next', target:'_self'});
          clickChecks.linkWaitsWithoutHint = clickRuntime._mayNavigate(
            {action:'click', args:{ref:'e0'}, expect_navigation:false}, {observation:{}});
          clickChecks.clicked = clicked;

          const storage = new Map();
          globalThis.sessionStorage = {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
          };
          const originalStart = mod.EmbeddedWebRuntime.prototype.start;
          mod.EmbeddedWebRuntime.prototype.start = async function () {
            throw new Error('injected startup failure');
          };
          let startupRejected = false;
          try {
            await mod.startEmbeddedRuntime({endpoint:'http://127.0.0.1:1',
              sessionId:'reset-test', sessionGeneration:'generation-reset',
              clientToken:'token', panel:false});
          } catch { startupRejected = true; }
          const singletonReset = !window.__WOA_EMBEDDED_RUNTIME_STARTED__;
          mod.EmbeddedWebRuntime.prototype.start = originalStart;

          const deferredRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          deferredRuntime.stored = {cachedResult:{action_id:'a-1',
            observation_after:{page_epoch:1}}, inFlight:{action_id:'a-1'}};
          deferredRuntime.stopped = false;
          deferredRuntime.panel = null;
          deferredRuntime.currentObservation = null;
          deferredRuntime._hello = async () => {};
          let sendAttempts = 0;
          deferredRuntime._sendResult = async () => {
            sendAttempts += 1;
            if (sendAttempts === 1) throw new Error('injected response loss');
          };
          let observations = 0;
          deferredRuntime.observe = async causedBy => {
            observations += 1;
            const fresh = {page_epoch:2, caused_by_action_id:causedBy,
              observation:{url:'https://app.test/new-document'}};
            deferredRuntime.currentObservation = fresh;
            return fresh;
          };
          const deferredPosts = [];
          deferredRuntime._post = async (path, body) => {
            deferredPosts.push({path, page_epoch:body?.page_epoch});
            return {accepted:true};
          };
          let eventLoops = 0;
          deferredRuntime._eventLoop = async () => { eventLoops += 1; };
          const deferredReturned = await deferredRuntime._startConnectedRuntime();
          await new Promise(resolve => setTimeout(resolve, 400));
          const deferredRecovery = {sameRuntime:deferredReturned === deferredRuntime,
            sendAttempts, observations, deferredPosts, eventLoops,
            currentEpoch:deferredRuntime.currentObservation?.page_epoch};

          pageLocation.href = 'https://app.test/form#details';
          const deadlineRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          deadlineRuntime.config = {navigationGuardMs:45000};
          deadlineRuntime.runtimeInstanceId = 'runtime-deadline';
          deadlineRuntime.panel = null;
          deadlineRuntime.stopped = false;
          deadlineRuntime.currentObservation = {observation_id:'o-1', page_epoch:1,
            observation:{url:'https://app.test/form#details', elements:[]}};
          deadlineRuntime.stored = {inFlight:null, cachedResult:null};
          deadlineRuntime._save = () => {};
          deadlineRuntime._navigationExpectation = () => (
            {url:'https://app.test/next', kind:'link'});
          deadlineRuntime._execute = async () => (
            {success:true, native_navigation_expected:true});
          deadlineRuntime._mayNavigate = () => false;
          deadlineRuntime.observe = async actionId => {
            const after = {session_id:'s', observation_id:'o-2', page_epoch:2,
              caused_by_action_id:actionId,
              observation:{url:'https://app.test/form#details', elements:[]}};
            deadlineRuntime.currentObservation = after;
            return after;
          };
          let deadlineResult = null;
          deadlineRuntime._post = async (path, body) => {
            if (path === '/claims') return {claimed:true};
            if (path === '/results') deadlineResult = body;
            return {accepted:true};
          };
          await deadlineRuntime._handleAction({schema_version:1, session_id:'s',
            turn_id:'t', action_id:'a-short', based_on_observation_id:'o-1', page_epoch:1,
            deadline:Date.now() + 400, command_digest:'0'.repeat(64),
            command:{command_id:'a-short', action:'submit', args:{ref:'e0'},
              expect_navigation:false}});

          const samePageRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          samePageRuntime.runtimeInstanceId = 'runtime-same-page';
          samePageRuntime.panel = null;
          samePageRuntime.stopped = false;
          samePageRuntime.currentObservation = {observation_id:'o-3', page_epoch:3,
            observation:{url:'https://app.test/form#details', elements:[]}};
          samePageRuntime.stored = {inFlight:null, cachedResult:null};
          samePageRuntime._save = () => {};
          samePageRuntime._navigationExpectation = () => ({url:'', kind:''});
          samePageRuntime._execute = async () => ({success:true});
          samePageRuntime.observe = async actionId => {
            const after = {session_id:'s', observation_id:'o-4', page_epoch:4,
              caused_by_action_id:actionId,
              observation:{url:'https://app.test/form#details', elements:[]}};
            samePageRuntime.currentObservation = after;
            return after;
          };
          let samePageResult = null;
          samePageRuntime._post = async (path, body) => {
            if (path === '/claims') return {claimed:true};
            if (path === '/results') samePageResult = body;
            return {accepted:true};
          };
          await samePageRuntime._handleAction({schema_version:1, session_id:'s',
            turn_id:'t2', action_id:'a-same-page', based_on_observation_id:'o-3',
            page_epoch:3, deadline:Date.now() + 1000, command_digest:'1'.repeat(64),
            command:{command_id:'a-same-page', action:'submit', args:{ref:'e0'},
              expect_navigation:false}});

          const invalidForm = new EventTarget();
          Object.assign(invalidForm, {tagName:'FORM', action:'https://app.test/save',
            target:'', method:'post', requestSubmit(){}, checkValidity(){ return false; }});
          const invalidRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          invalidRuntime.runtimeInstanceId = 'runtime-invalid-form';
          invalidRuntime.panel = null;
          invalidRuntime.stopped = false;
          invalidRuntime.controller = {selectorMap:{}};
          invalidRuntime.getElementByIndex = () => invalidForm;
          invalidRuntime.currentObservation = {observation_id:'o-5', page_epoch:5,
            observation:{url:'https://app.test/form#details', elements:[]}};
          invalidRuntime.stored = {inFlight:null, cachedResult:null};
          invalidRuntime._save = () => {};
          invalidRuntime.observe = async actionId => {
            const after = {session_id:'s', observation_id:'o-6', page_epoch:6,
              caused_by_action_id:actionId,
              observation:{url:'https://app.test/form#details', elements:[]}};
            invalidRuntime.currentObservation = after;
            return after;
          };
          let invalidResult = null;
          invalidRuntime._post = async (path, body) => {
            if (path === '/claims') return {claimed:true};
            if (path === '/results') invalidResult = body;
            return {accepted:true};
          };
          await invalidRuntime._handleAction({schema_version:1, session_id:'s',
            turn_id:'t3', action_id:'a-invalid', based_on_observation_id:'o-5',
            page_epoch:5, deadline:Date.now() + 1000, command_digest:'2'.repeat(64),
            command:{command_id:'a-invalid', action:'submit', args:{ref:'e0'},
              expect_navigation:false}});

          const pageShowHandlers = new Set();
          window.addEventListener = (type, handler) => {
            if (type === 'pageshow') pageShowHandlers.add(handler);
          };
          window.removeEventListener = (type, handler) => {
            if (type === 'pageshow') pageShowHandlers.delete(handler);
          };
          // A page restored from the back-forward cache: the Runtime cannot vouch for the
          // lease it held, so it stops and says why; its supervisor (boot.ts) starts the next
          // one. Restarting itself as well had two owners racing for the page latch.
          const bfcacheRuntime = new mod.EmbeddedWebRuntime({
            endpoint:'https://gateway.test', sessionId:'bfcache-test',
            sessionGeneration:'generation-bfcache', clientToken:'token', panel:false,
          });
          let abortCount = 0;
          let disposeCount = 0;
          bfcacheRuntime.eventAbortController = {abort(){ abortCount += 1; }};
          bfcacheRuntime.controller = {dispose(){ disposeCount += 1; }};
          sessionStorage.setItem('woa.remote.v1.bfcache-test', JSON.stringify({
            sessionGeneration:'generation-bfcache',
            clientId:bfcacheRuntime.stored.clientId, epoch:7, lastEventId:'e00000007',
            inFlight:null, cachedResult:null,
          }));
          window.__WOA_EMBEDDED_RUNTIME_STARTED__ = bfcacheRuntime;
          let stopNotices = 0;
          bfcacheRuntime.onStopped(() => { stopNotices += 1; });
          const oldPageShowHandler = [...pageShowHandlers][0];
          oldPageShowHandler({persisted:false});
          oldPageShowHandler({persisted:true});
          oldPageShowHandler({persisted:true});
          await new Promise(resolve => setTimeout(resolve, 0));
          const bfcacheRecovery = {
            stopNotices, oldStopped:bfcacheRuntime.stopped, endState:bfcacheRuntime.endState,
            oldListenerRemoved:!pageShowHandlers.has(oldPageShowHandler),
            listenerCount:pageShowHandlers.size, abortCount, disposeCount,
            pageReleased:!window.__WOA_EMBEDDED_RUNTIME_STARTED__,
          };

          process.stdout.write(JSON.stringify({tokenProof, submitChecks, submitEvidence,
            clickChecks,
            startupRejected, singletonReset, deferredRecovery, deadlineResult,
            samePageResult, invalidResult, bfcacheRecovery}));
        """.replace("__SOURCE__", source)
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=15, check=True)
        result = json.loads(proc.stdout)
        self.assertTrue(result["tokenProof"]["exact"])
        self.assertFalse(result["tokenProof"]["different"])
        self.assertFalse(result["tokenProof"]["hasRaw"])
        for secret in ("START", "EXPECTED"):
            self.assertNotIn(secret, result["tokenProof"]["wire"])
        self.assertEqual(result["tokenProof"]["fragmentDisplay"],
                         "https://app.test/orders#details")
        self.assertIn("origin_refused", result["submitChecks"]["crossOrigin"])
        self.assertIn("current page", result["submitChecks"]["newWindow"])
        self.assertEqual(result["submitChecks"]["allowed"], "")
        self.assertEqual(result["submitChecks"]["submitted"], 1)
        self.assertTrue(result["submitEvidence"]["native"]["native_navigation_expected"])
        self.assertFalse(result["submitEvidence"]["ajax"]["native_navigation_expected"])
        self.assertFalse(result["submitEvidence"]["dialog"]["native_navigation_expected"])
        self.assertIn("current page", result["clickChecks"]["namedWindow"])
        self.assertIn("current page", result["clickChecks"]["topFrame"])
        self.assertIn("current page", result["clickChecks"]["misleadingFormTarget"])
        self.assertEqual(result["clickChecks"]["allowed"], "")
        self.assertIn("current page", result["clickChecks"]["baseWindow"])
        self.assertIn("origin_refused", result["clickChecks"]["crossOriginFormAction"])
        self.assertIn("current page", result["clickChecks"]["formTargetWindow"])
        self.assertEqual(result["clickChecks"]["submitAllowed"], "")
        self.assertEqual(result["clickChecks"]["clicked"], 2)
        self.assertFalse(result["clickChecks"]["submitClickWaitsWithoutHint"])
        self.assertFalse(result["clickChecks"]["explicitSubmitWaitsWithoutHint"])
        self.assertTrue(result["clickChecks"]["submitWaitsWithHint"])
        self.assertTrue(result["clickChecks"]["linkWaitsWithoutHint"])
        self.assertTrue(result["startupRejected"])
        self.assertTrue(result["singletonReset"])
        self.assertEqual(result["deferredRecovery"], {
            "sameRuntime": True,
            "sendAttempts": 2,
            "observations": 1,
            "deferredPosts": [{"path": "/observations", "page_epoch": 2}],
            "eventLoops": 1,
            "currentEpoch": 2,
        })
        self.assertEqual(result["deadlineResult"]["status"], "completed")
        self.assertFalse(result["deadlineResult"]["receipt"]["ok"])
        self.assertEqual(result["deadlineResult"]["receipt"]["error_code"],
                         "navigation_timeout")
        self.assertFalse(result["deadlineResult"]["receipt"]["navigated"])
        self.assertEqual(result["deadlineResult"]["receipt"]["url_before"],
                         "https://app.test/form#details")
        self.assertEqual(result["samePageResult"]["status"], "completed")
        self.assertTrue(result["samePageResult"]["receipt"]["ok"])
        self.assertFalse(result["samePageResult"]["receipt"]["navigated"])
        self.assertEqual(result["invalidResult"]["status"], "completed")
        self.assertFalse(result["invalidResult"]["receipt"]["ok"])
        self.assertEqual(result["invalidResult"]["receipt"]["error_code"],
                         "element_not_actionable")
        self.assertIn("form validation prevented submission",
                      result["invalidResult"]["receipt"]["error_detail"])
        self.assertFalse(result["invalidResult"]["receipt"]["navigated"])
        self.assertEqual(result["bfcacheRecovery"], {
            "stopNotices": 1,
            "oldStopped": True,
            "endState": "suspended",
            "oldListenerRemoved": True,
            "listenerCount": 0,
            "abortCount": 1,
            "disposeCount": 1,
            "pageReleased": True,
        })

    def test_runtime_has_no_server_brain_or_arbitrary_javascript_surface(self):
        source = _runtime_bytes().decode("utf-8")
        self.assertIn("PageController", source)
        self.assertIn("highlightLabelOpacity: 0", source)
        self.assertIn("playwright-highlight-container", source)
        self.assertNotIn("executeJavascript(", source)
        for forbidden in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SourceMap", "ControlPlan",
                          "evaluator"):
            self.assertNotIn(forbidden, source)

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

    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_pagecontroller_observation_is_viewport_bounded_and_scrollable(self):
        """P8.6: a large page stays bounded and off-screen controls require a fresh view."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(
            allowed_origins=[app.origin], session_id="bounded-observation-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser_process = playwright.chromium.launch(headless=True)
                page = browser_process.new_page(viewport={"width": 1280, "height": 800})
                page.goto(app.origin + "/large", wait_until="domcontentloaded")

                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                first = remote.observe()
                self.assertLess(len(first.elements), 100)
                self.assertTrue(any(element.name == "Action 0" for element in first.elements))
                self.assertFalse(any(element.name == "Action 999" for element in first.elements))
                self.assertIn("scroll to see more", first.content)

                receipt = remote.execute("scroll", {"direction": "down", "pages": 3})
                self.assertTrue(receipt.ok, receipt.error_detail)
                self.assertIsNotNone(receipt.observation)
                after = receipt.observation
                self.assertFalse(any(element.name == "Action 0" for element in after.elements))
                self.assertTrue(any(element.name.startswith("Action ")
                                    for element in after.elements))
                self.assertGreater(page.evaluate("scrollY"), 0)

                browser_process.close()
        finally:
            gateway.close()
            app.close()

    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_pagecontroller_survives_navigation_without_repeating_action(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(allowed_origins=[app.origin], session_id="browser-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        try:
            with sync_playwright() as playwright:
                browser_process = playwright.chromium.launch(headless=True)
                page = browser_process.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")

                remote = RemoteBrowserClient(channel, timeout_s=8)
                receipts = []
                execute = remote.execute

                def record_execute(*args, **kwargs):
                    receipt = execute(*args, **kwargs)
                    receipts.append(receipt)
                    return receipt

                remote.execute = record_execute
                started = remote.start()
                first = remote.observe()
                link = next(element for element in first.elements if element.role == "link")
                password = next(element for element in first.elements if element.role == "password")
                self.assertNotIn("fixture-password", first.content)
                self.assertNotIn("描述你想完成的目标", first.content)
                page.locator(".woa-launcher").click()
                page.locator("#woa-composer-input").fill("private assistant draft")
                opened = remote.execute("observe").observation
                self.assertNotIn("private assistant draft", opened.content)
                self.assertNotIn("描述你想完成的目标", opened.content)
                self.assertFalse(any("描述你想完成的目标" in element.name
                                     for element in opened.elements))
                first = opened
                link = next(element for element in first.elements if element.role == "link")
                password = next(element for element in first.elements if element.role == "password")
                initial_commands = remote.command_count
                self.assertIn("...", first.content)
                self.assertEqual(
                    link.attrs["href"],
                    "/page2?source=page-controller-long-navigation-target",
                )

                secret = "runtime-only-super-secret"
                chat = _Chat([
                    ("fill", {"ref": password.ref, "value": secret}),
                    ("click", {"ref": link.ref, "expect_navigation": True}),
                    ("finish", {"done": True, "why": "order details are visible"}),
                ])
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = SessionLedger(Path(tmp) / "embedded.jsonl", "embedded-react")
                    session = ServerAgentSession(
                        task=TaskSpec("embedded-runtime", "Open the order details"),
                        lab=LabApparatus(arm="P"), chat=chat,
                        browser=remote, control=_Control([[], [], [], []]), ledger=ledger,
                        step_budget=4, wall_budget_s=15,
                        run_id="embedded-react",
                    )
                    session.entry_observation = first
                    outcome = session.run()
                    result_wire = json.dumps(
                        [item.to_json() for item in channel._results.values()])
                    self.assertNotIn(secret, result_wire)
                    self.assertNotIn(secret, ledger.path.read_text())

                self.assertEqual(outcome.terminal_reason, "agent_finished")
                self.assertEqual(remote.command_count - initial_commands, 2)
                fill_receipt = next(item for item in receipts if item.action == "fill")
                receipt = next(item for item in receipts if item.action == "click")
                self.assertTrue(fill_receipt.ok)
                self.assertEqual(fill_receipt.args_echo["value"], "[redacted]")
                self.assertTrue(receipt.ok)
                self.assertTrue(receipt.navigated)
                self.assertEqual(
                    receipt.url_after,
                    app.origin + "/page2?source=page-controller-long-navigation-target",
                )
                self.assertEqual(started["client_id"], channel.wait_for_client(1).client_id)
                self.assertEqual(app.page2_requests, 1)
                self.assertEqual(errors, [])
                page.close()
                browser_process.close()
        finally:
            gateway.close()
            app.close()

    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_same_origin_iframe_form_navigation_is_observed_without_top_level_change(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")

        registry = RemoteSessionRegistry()
        app = _FixtureApplication(PAGE_CONTROLLER)
        app.start()
        grant, channel = registry.create(
            allowed_origins=[app.origin], session_id="iframe-browser-session")
        gateway = EmbeddedGateway(registry, sse_wait_s=0.1).start()
        app.runtime_url = gateway.runtime_url
        app.runtime_config = gateway.bootstrap_config(
            grant, page_controller_module=app.origin + "/page-controller.js")
        app.runtime_config["navigationGuardMs"] = 3_000
        try:
            with sync_playwright() as playwright:
                browser_process = playwright.chromium.launch(headless=True)
                page = browser_process.new_page()
                errors = []
                console_errors = []
                requests = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(
                    f"{message.type}:{message.text}"))
                page.on("request", lambda request: requests.append(request.url))
                host_path = "/accounting/control/FindInvoices"
                page.goto(app.origin + host_path, wait_until="domcontentloaded")

                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                first = remote.observe()
                submitters = [element for element in first.elements
                              if element.role == "button"]
                self.assertTrue(submitters, repr(first.elements))
                submit = submitters[0]
                self.assertEqual(submit.name, "RunReport")
                action_result = {}

                def submit_in_background():
                    try:
                        action_result["receipt"] = remote.execute(
                            "submit", {"ref": submit.ref})
                    except Exception as exc:
                        action_result["error"] = exc

                worker = threading.Thread(target=submit_in_background, daemon=True)
                worker.start()
                page.wait_for_function("""() => {
                  const frame = document.querySelector('iframe');
                  return frame?.contentDocument?.body?.innerText.includes('Invoice report ready');
                }""", timeout=5_000)
                worker.join(timeout=8)
                self.assertFalse(worker.is_alive(), "Runtime did not return iframe action evidence")
                self.assertNotIn(
                    "error", action_result,
                    f"{action_result.get('error')}; page_errors={errors}; "
                    f"console_errors={console_errors}; frames={[frame.url for frame in page.frames]}; "
                    f"result_requests={app.iframe_result_requests}; requests={requests}")
                receipt = action_result["receipt"]

                self.assertTrue(receipt.ok, receipt.error_detail)
                self.assertFalse(receipt.navigated)
                self.assertEqual(receipt.url_before, app.origin + host_path)
                self.assertEqual(receipt.url_after, app.origin + host_path)
                self.assertIn("Invoice report ready", remote.observe().content)
                self.assertEqual(app.iframe_result_requests, 1)
                self.assertEqual(errors, [])
                page.close()
                browser_process.close()
        finally:
            gateway.close()
            app.close()


class _FixtureApplication:
    def __init__(self, controller_path: Path):
        self.controller_path = controller_path
        self.runtime_url = ""
        self.runtime_config = {}
        self.page2_requests = 0
        self.iframe_result_requests = 0
        self.form_submissions = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                if self.path == "/page-controller.js":
                    return self.reply(controller_path.read_bytes(), "text/javascript")
                if self.path == "/page1":
                    return self.page("""
                      <h1>Orders</h1>
                      <input type="password" name="PASSWORD" value="fixture-password">
                      <a id="next" href="/page2?source=page-controller-long-navigation-target">
                        Open order details</a>
                    """)
                if self.path == "/form":
                    return self.page('<h1>Customer</h1><form method="post" action="/saved">'
                                     '<label>Phone <input name="phone" value="old"></label>'
                                     '<button type="submit">Save</button></form>')
                if self.path == "/large":
                    controls = "".join(
                        f'<button style="display:block;width:240px;height:28px;margin:2px">'
                        f'Action {index}</button>' for index in range(1000))
                    return self.page("<h1>Large workspace</h1>" + controls)
                if self.path.startswith("/page2?"):
                    owner.page2_requests += 1
                    return self.page("<h1>Order details</h1><button>Done</button>")
                if self.path == "/accounting/control/FindInvoices":
                    return self.page("""
                      <h1>Invoice workspace</h1>
                      <iframe title="Invoice search" src="/accounting/control/InvoiceSearchFrame">
                      </iframe>
                    """)
                if self.path == "/accounting/control/InvoiceSearchFrame":
                    return self.page("""
                      <form method="get" action="/accounting/control/InvoiceReport">
                        <input name="status" value="OPEN">
                        <input type="submit" value="RunReport">
                      </form>
                    """, runtime=False)
                if self.path.startswith("/accounting/control/InvoiceReport?"):
                    owner.iframe_result_requests += 1
                    return self.page("<h2>Invoice report ready</h2>", runtime=False)
                self.send_error(404)

            def do_POST(self):
                if self.path == "/saved":
                    owner.form_submissions += 1
                    return self.reply(b"saved", "text/plain")
                self.send_error(404)

            def page(self, content: str, *, runtime: bool = True):
                scripts = ""
                if runtime:
                    config = json.dumps(owner.runtime_config).replace("</", "<\\/")
                    scripts = (f"<script>window.__WOA_EMBED_CONFIG__={config}</script>" +
                               f"<script type=\"module\" src=\"{owner.runtime_url}\"></script>")
                html = ("<!doctype html><html><head><title>Fixture</title></head><body>" +
                        content + scripts + "</body></html>")
                self.reply(html.encode(), "text/html; charset=utf-8")

            def reply(self, body: bytes, content_type: str):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
