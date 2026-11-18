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


class EmbeddedRuntimeObservationTests(unittest.TestCase):
    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_linked_tab_panel_must_be_visible_unique_and_not_busy(self):
        from playwright.sync_api import sync_playwright
        if not RUNTIME.is_file():
            self.skipTest("assistant.js is not built; run scripts/build_runtime.py")
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
                page = browser.new_page()
                page.goto(app.origin + "/tabs")
                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                def payments():
                    receipt = remote.execute("observe", {})
                    self.assertTrue(receipt.ok, receipt.error_detail)
                    return next(e for e in receipt.observation.elements if e.role == "tab" and e.name == "Payments")
                self.assertEqual(payments().attrs["panel_ready"], "false")
                page.get_by_role("tab", name="Payments", exact=True).click()
                self.assertEqual(payments().attrs["panel_ready"], "true", page.locator("#payments-panel").evaluate(
                    "e => ({html:e.outerHTML,rects:e.getClientRects().length,tab:document.querySelector('#payments-tab').outerHTML})"))
                page.locator("#payments-panel").evaluate("e => e.setAttribute('aria-busy', 'true')")
                self.assertEqual(payments().attrs["panel_ready"], "false")
                page.locator("#payments-panel").evaluate("e => e.removeAttribute('aria-busy')")
                page.locator("#payments-panel").evaluate("e => e.hidden = true")
                self.assertEqual(payments().attrs["panel_ready"], "false")
                page.locator("#payments-panel").evaluate("e => {e.hidden=false; e.insertAdjacentHTML('afterend', e.outerHTML)}")
                self.assertEqual(payments().attrs["panel_ready"], "false")
                browser.close()
        finally:
            gateway.close()
            app.close()


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


    @browser_only
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

                # A real document navigation restores the person's expanded panel and
                # replays the timeline from the server without another launcher gesture.
                page.goto(app.origin + "/page2?source=navigation", wait_until="domcontentloaded")
                page.locator(".woa-window").wait_for(timeout=8000)
                self.assertEqual(page.locator(".woa-launcher").count(), 0)
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


    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_goto_asks_the_application_to_open_its_own_page_before_loading_it(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const pageLocation = {{href:'https://app.test/start', origin:'https://app.test',
            assign(value) {{ assigned.push(value); }}}};
          globalThis.location = pageLocation;
          globalThis.window = new EventTarget();
          window.location = pageLocation;
          const assigned = [], asked = [];
          const run = async (openPage, url) => {{
            const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
            runtime.config = {{navigationMode:'document'}};
            runtime.openPage = openPage;
            return (await runtime._execute({{action:'goto', args:{{url}}}})).message;
          }};
          const opened = await run(async url => {{ asked.push(url); return true; }}, '/orders?status=1');
          const declined = await run(async url => {{ asked.push(url); return false; }}, '/declined');
          const broken = await run(async () => {{ throw new Error('router gone'); }}, '/broken');
          const none = await run(undefined, '/none');
          process.stdout.write(JSON.stringify({{opened, declined, broken, none, assigned, asked}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        # The application is asked with the address on this origin; a page it opened is not
        # loaded, and one it declined, failed on or cannot open at all is loaded as before.
        self.assertEqual(result["asked"], ["https://app.test/orders?status=1", "https://app.test/declined"])
        self.assertEqual(result["opened"], "application navigation requested")
        self.assertEqual([result["declined"], result["broken"], result["none"]], ["navigation requested"] * 3)
        self.assertEqual(result["assigned"], ["https://app.test/declined", "https://app.test/broken",
                                              "https://app.test/none"])


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


    @browser_only
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


    @browser_only
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
                # The panel's own words, read from the contract rather than restated here, so
                # a change of copy cannot turn this into a check for text nobody renders.
                own_words = [panel_contract()["label_keys"][key]
                             for key in ("placeholder", "emptyGreeting", "emptyDetail")]
                self.assertNotIn("fixture-password", first.content)
                for words in own_words:
                    self.assertNotIn(words, first.content)
                page.locator(".woa-launcher").click()
                page.locator("#woa-composer-input").fill("private assistant draft")
                opened = remote.execute("observe").observation
                self.assertNotIn("private assistant draft", opened.content)
                for words in own_words:
                    self.assertNotIn(words, opened.content)
                    self.assertFalse(any(words in element.name for element in opened.elements))
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


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_goto_current_address_preserves_document_without_reload_or_retry(self):
        from playwright.sync_api import sync_playwright
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
                page = browser.new_page()
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")
                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                first = remote.observe()
                before_document = page.evaluate(
                    "window.__WOA_EMBEDDED_RUNTIME_STARTED__.documentId")
                before_commands = remote.command_count
                loads = []
                page.on("request", lambda request: loads.append(request.url)
                        if request.is_navigation_request() and request.frame == page.main_frame
                        else None)
                receipt = remote.execute("goto", {"url": "/page1"})
                self.assertTrue(receipt.ok, receipt.error_detail)
                self.assertFalse(receipt.navigated)
                self.assertEqual(receipt.url_after, first.url)
                self.assertGreater(receipt.observation.epoch, first.epoch)
                self.assertEqual(before_document, page.evaluate(
                    "window.__WOA_EMBEDDED_RUNTIME_STARTED__.documentId"))
                self.assertEqual(remote.command_count - before_commands, 1)
                self.assertEqual(loads, [])
                self.assertEqual(app.form_submissions, 0)
                browser.close()
        finally:
            gateway.close()
            app.close()


    @browser_only
    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_document_navigation_receipt_survives_history_canonicalization_without_retry(self):
        """The application adds its default query before the successor Runtime starts."""
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
                page = browser.new_page()
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")
                remote = RemoteBrowserClient(channel, timeout_s=8)
                remote.start()
                first = remote.observe()
                received_results = []
                wait_for_result = channel.wait_for_result

                def record_result(*args, **kwargs):
                    result = wait_for_result(*args, **kwargs)
                    received_results.append(result)
                    return result

                channel.wait_for_result = record_result
                before_commands = remote.command_count
                try:
                    receipt = remote.execute("goto", {"url": "/directory"})
                except TransportError as error:
                    facts = page.evaluate("""() => {
                      const runtime = window.__WOA_EMBEDDED_RUNTIME_STARTED__;
                      const action = runtime?.stored?.inFlight;
                      return {url: location.href, timeOrigin: performance.timeOrigin,
                        navigation: performance.getEntriesByType('navigation').map(entry =>
                          ({name:entry.name, type:entry.type, entryType:entry.entryType,
                            redirectCount:entry.redirectCount, startTime:entry.startTime})),
                        documentId: runtime?.documentId,
                        action: action && {phase:action.phase, documentId:action.document_id,
                          dispatchedAt:action.dispatched_at,
                          expectedDigest:action.expected_url_digest,
                          startedDigest:action.started_url_digest}};
                    }""")
                    self.fail(f"{error}; browser recovery facts: {facts}")
                self.assertTrue(receipt.ok, receipt.error_detail)
                self.assertTrue(receipt.navigated)
                self.assertEqual(receipt.url_before, first.url)
                self.assertEqual(receipt.url_after, app.origin + "/directory?group=mine")
                self.assertEqual(receipt.args_echo, {"url": "/directory"})
                self.assertEqual(remote.command_count - before_commands, 1)
                self.assertEqual(app.directory_requests, 1)
                self.assertEqual(page.evaluate(
                    "performance.getEntriesByType('navigation')[0].name"),
                    app.origin + "/directory")
                result = received_results[-1]
                self.assertEqual(result.action_id, receipt.command_id)
                self.assertEqual(result.observation_after.caused_by_action_id, receipt.command_id)

                # Loading another destination through an HTTP redirect is not this proof.
                before_commands = remote.command_count
                with self.assertRaises(TransportError):
                    remote.execute("goto", {"url": "/directory-redirect"})
                self.assertEqual(remote.command_count - before_commands, 1)
                self.assertEqual(app.directory_redirect_requests, 1)
                self.assertEqual(app.directory_requests, 2)
                self.assertEqual(app.form_submissions, 0)
                self.assertEqual(len([result for result in received_results
                                      if result.status == "indeterminate"]), 1)
                browser.close()
        finally:
            gateway.close()
            app.close()


    @browser_only
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


if __name__ == "__main__":
    unittest.main()
