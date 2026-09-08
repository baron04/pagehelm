from __future__ import annotations

import base64
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

from web_operation_agent.embedded_gateway import EmbeddedGateway
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.remote import RemoteBrowserClient, RemoteSessionRegistry
from web_operation_agent.schemas import ApprovalRequest
from web_operation_agent.session import ServerAgentSession, TaskSpec
from web_operation_agent.tests.test_session import _Chat, _Control


RUNTIME = Path(__file__).resolve().parents[1] / "web_runtime" / "embedded.js"
_PAGE_CONTROLLER_PATH = os.environ.get("WOA_PAGE_CONTROLLER_MODULE", "")
PAGE_CONTROLLER = Path(_PAGE_CONTROLLER_PATH).expanduser() if _PAGE_CONTROLLER_PATH else None


class EmbeddedRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_action_progress_is_readable_and_never_echoes_fill_value(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = f"""
          const mod = await import('data:text/javascript;base64,{source}');
          const rows = [
            mod.describeAction({{action:'goto', args:{{url:'/orders'}}}}),
            mod.describeAction({{action:'fill', args:{{ref:'e12', value:'private-value'}}}}),
            mod.describeAction({{action:'submit', args:{{ref:'e15'}}}}),
          ];
          process.stdout.write(JSON.stringify(rows));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        rows = json.loads(proc.stdout)
        self.assertEqual(rows, ["打开页面：/orders", "填写页面字段：e12", "提交页面表单：e15"])
        self.assertNotIn("private-value", proc.stdout)

    @unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                         "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
    def test_real_confirmation_card_posts_authenticated_user_decision(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")

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
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(app.origin + "/page1", wait_until="domcontentloaded")
                channel.wait_for_client(5)
                request = ApprovalRequest(
                    1, "approval-browser", "Accept supplier quote",
                    ("Quote Q-1042",), int(time.time() * 1000) + 5000)
                channel.request_approval(request)
                host = page.locator("#woa-assistant")
                host.wait_for(state="attached", timeout=5000)
                page.wait_for_function(
                    "document.querySelector('#woa-assistant')?.shadowRoot?.querySelector('.approval')",
                    timeout=5000)
                card_text = host.evaluate(
                    "node => node.shadowRoot.querySelector('.approval').textContent")
                self.assertIn("Accept supplier quote", card_text)
                self.assertIn("Quote Q-1042", card_text)
                host.evaluate(
                    "node => node.shadowRoot.querySelector('.approval button:not(.deny)').click()")
                decision = channel.wait_for_approval(request.approval_id, timeout_s=2)
                self.assertEqual(decision.decision, "approved")
                self.assertEqual(errors, [])
                browser.close()
        finally:
            gateway.close()
            app.close()

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_approval_decision_is_persisted_before_idempotent_post(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = f"""
          const mod = await import('data:text/javascript;base64,{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.sessionId = 'session-1';
          runtime.stored = {{approvals:{{}}}};
          let saves = 0;
          runtime._save = () => {{ saves += 1; }};
          let prompts = 0;
          runtime.panel = {{
            add() {{}}, status() {{}},
            async requestApproval() {{ prompts += 1; return 'approved'; }},
          }};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); return {{accepted:true}}; }};
          const event = {{kind:'approval_required', payload:{{
            schema_version:1, approval_id:'approval-1', effect:'Update quote',
            targets:['Quote Q-1042'], expires_at:Date.now()+10000,
          }}}};
          await runtime._handleEvent(event);
          await runtime._handleEvent(event);
          process.stdout.write(JSON.stringify({{prompts, saves, posts,
            stored:runtime.stored.approvals}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["prompts"], 1)
        self.assertEqual(result["stored"], {"approval-1": "approved"})
        self.assertEqual(result["saves"], 1)
        self.assertEqual(len(result["posts"]), 2)
        self.assertEqual(result["posts"][0], {
            "path": "/approvals",
            "body": {"schema_version": 1, "session_id": "session-1",
                     "approval_id": "approval-1", "decision": "approved"},
        })

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_first_party_shell_can_submit_through_the_same_panel_api(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = f"""
          const mod = await import('data:text/javascript;base64,{source}');
          globalThis.location = {{href:'https://app.test/home'}};
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.stored = {{taskInFlight:false}};
          runtime._save = () => {{}};
          const statuses = [];
          runtime.panel = {{add() {{}}, status(value) {{statuses.push(value)}}, setBusy() {{}}}};
          const sent = [];
          let epoch = 0;
          runtime.observe = async () => ({{schema_version:1, session_id:'session-1',
            observation_id:`client:${{++epoch}}`, page_epoch:epoch,
            observation:{{url:globalThis.location.href}}}});
          runtime._post = async (path, body) => {{ sent.push({{path, body}}); return {{accepted:true}}; }};
          await runtime.submitTask('  Open deployment details  ');
          let overlapRejected = false;
          try {{ await runtime.submitTask('Open another page'); }} catch {{ overlapRejected = true; }}
          await runtime._handleEvent({{kind:'progress', payload:{{state:'needs_input', message:'more'}}}});
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
        self.assertIn("等待补充", result["statuses"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_product_turn_terminal_states_release_the_panel(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = f"""
          const mod = await import('data:text/javascript;base64,{source}');
          const expected = {{
            finished:'已结束', refused:'无法执行', cancelled:'已取消',
            indeterminate:'结果不确定', failed:'任务失败',
          }};
          const rows = [];
          for (const [state, label] of Object.entries(expected)) {{
            const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
            runtime.stored = {{taskInFlight:true}};
            runtime._save = () => {{}};
            let busy = true;
            let status = '';
            runtime.panel = {{
              add() {{}},
              setBusy(value) {{ busy = value; }},
              status(value) {{ status = value; }},
            }};
            await runtime._handleEvent({{kind:'progress', payload:{{state, message:'done'}}}});
            rows.push({{state, label, status, busy, taskInFlight:runtime.stored.taskInFlight}});
          }}
          process.stdout.write(JSON.stringify(rows));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        rows = json.loads(proc.stdout)
        self.assertEqual(
            [row["status"] for row in rows],
            ["已结束", "无法执行", "已取消", "结果不确定", "任务失败"],
        )
        self.assertTrue(all(row["busy"] is False for row in rows))
        self.assertTrue(all(row["taskInFlight"] is False for row in rows))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_goto_uses_opt_in_spa_navigation_and_keeps_document_default(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = f"""
          const mod = await import('data:text/javascript;base64,{source}');
          const pageLocation = {{href:'https://app.test/start', origin:'https://app.test',
            assign(value) {{ assigned = value; this.href = value; }}}};
          globalThis.location = pageLocation;
          globalThis.window = new EventTarget();
          window.location = pageLocation;
          let assigned = '';
          let pushed = '';
          let pops = 0;
          globalThis.history = {{pushState(_state, _title, value) {{
            pushed = value; pageLocation.href = value;
          }}}};
          globalThis.PopStateEvent = class extends Event {{}};
          window.addEventListener('popstate', () => {{ pops += 1; }});

          const spa = Object.create(mod.EmbeddedWebRuntime.prototype);
          spa.config = {{navigationMode:'spa'}};
          const spaResult = await spa._execute(
            {{action:'goto', args:{{url:'/object/company/company-1'}}}});

          pageLocation.href = 'https://app.test/start';
          const documentRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          documentRuntime.config = {{navigationMode:'document'}};
          const documentResult = await documentRuntime._execute(
            {{action:'goto', args:{{url:'/settings/profile'}}}});
          process.stdout.write(JSON.stringify({{pushed, pops, assigned,
            spaResult, documentResult}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["pushed"],
                         "https://app.test/object/company/company-1")
        self.assertEqual(result["pops"], 1)
        self.assertEqual(result["assigned"], "https://app.test/settings/profile")
        self.assertEqual(result["spaResult"]["message"], "SPA navigation requested")
        self.assertEqual(result["documentResult"]["message"], "navigation requested")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the browser module helpers")
    def test_redaction_and_pagecontroller_index_adapter(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = f"""
          const mod = await import('data:text/javascript;base64,{source}');
          const h = mod.runtimeInternals;
          const content = [
            '[0]<input type=password name=PASSWORD value=my super secret />',
            '[4]<a href=/next?externalLoginKey=abc123>Next />',
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
        self.assertIn("externalLoginKey=[redacted]", result["clean"])
        self.assertIn("jsessionid%253D[redacted]", result["encoded"])
        self.assertNotIn("TOPSECRET", result["generic"])
        self.assertIn("access_token=[redacted]", result["generic"])
        self.assertNotIn("TOPSECRET", result["encodedGeneric"])
        self.assertIn("access_token%253D[redacted]", result["encodedGeneric"])
        self.assertEqual(result["encodedKeyGeneric"], "[redacted]")
        self.assertEqual(result["encodedKeyHandoff"], "[redacted]")
        self.assertEqual([item["ref"] for item in result["elements"]],
                         ["e0", "e4", "e7", "e8", "e9"])
        self.assertEqual(result["elements"][1]["role"], "link")
        self.assertEqual(result["elements"][0]["epoch"], 9)
        self.assertEqual(result["hydrated"][0]["attrs"], {
            "href": "/catalog/deployments/edge-api", "target": "_self"})
        self.assertNotIn("TOPSECRET", json.dumps(result["secretHydrated"]))
        self.assertEqual(result["secretHydrated"][0]["attrs"]["href"],
                         "/callback?access_token=[redacted]&next=1")
        self.assertEqual(result["args"]["value"], "[redacted]")
        self.assertEqual(result["args"]["nested"]["token"], "[redacted]")
        self.assertIn("First line", result["elements"][3]["name"])
        self.assertIn("Second line", result["elements"][3]["name"])
        self.assertEqual(result["elements"][4]["name"], "Find")
        self.assertEqual(result["recoveries"], {
            "link": True, "prepared": False, "submit": False, "reload": False,
            "formGet": True,
            "formDocument": True, "formSameDocument": False,
        })
        self.assertTrue(result["terminalStopped"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed for runtime fault injection")
    def test_runtime_recovery_security_and_deadline_faults(self):
        source = base64.b64encode(RUNTIME.read_bytes()).decode()
        script = """
          const mod = await import('data:text/javascript;base64,__SOURCE__');
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
              sessionId:'reset-test', clientToken:'token', panel:false});
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
          const originalRestartStart = mod.EmbeddedWebRuntime.prototype.start;
          let restartedRuntime = null;
          let restartCount = 0;
          mod.EmbeddedWebRuntime.prototype.start = async function () {
            restartedRuntime = this;
            restartCount += 1;
            return this;
          };
          const bfcacheRuntime = new mod.EmbeddedWebRuntime({
            endpoint:'https://gateway.test', sessionId:'bfcache-test',
            clientToken:'token', panel:false,
          });
          let abortCount = 0;
          let disposeCount = 0;
          bfcacheRuntime.eventAbortController = {abort(){ abortCount += 1; }};
          bfcacheRuntime.controller = {dispose(){ disposeCount += 1; }};
          sessionStorage.setItem('woa.remote.v1.bfcache-test', JSON.stringify({
            clientId:bfcacheRuntime.stored.clientId, epoch:7, lastEventId:'e00000007',
            inFlight:null, cachedResult:null,
          }));
          window.__WOA_EMBEDDED_RUNTIME_STARTED__ = true;
          const oldPageShowHandler = [...pageShowHandlers][0];
          oldPageShowHandler({persisted:false});
          oldPageShowHandler({persisted:true});
          oldPageShowHandler({persisted:true});
          await new Promise(resolve => setTimeout(resolve, 0));
          const bfcacheRecovery = {
            restartCount, oldStopped:bfcacheRuntime.stopped,
            oldListenerRemoved:!pageShowHandlers.has(oldPageShowHandler),
            listenerCount:pageShowHandlers.size, abortCount, disposeCount,
            freshStoredEpoch:restartedRuntime?.stored?.epoch,
            freshRuntimeId:restartedRuntime?.runtimeInstanceId !== bfcacheRuntime.runtimeInstanceId,
          };
          mod.EmbeddedWebRuntime.prototype.start = originalRestartStart;

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
            "restartCount": 1,
            "oldStopped": True,
            "oldListenerRemoved": True,
            "listenerCount": 1,
            "abortCount": 1,
            "disposeCount": 1,
            "freshStoredEpoch": 7,
            "freshRuntimeId": True,
        })

    def test_runtime_has_no_server_brain_or_arbitrary_javascript_surface(self):
        source = RUNTIME.read_text()
        self.assertIn("PageController", source)
        self.assertIn("highlightLabelOpacity: 0", source)
        self.assertIn("playwright-highlight-container", source)
        self.assertNotIn("executeJavascript(", source)
        for forbidden in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SourceMap", "ControlPlan",
                          "evaluator"):
            self.assertNotIn(forbidden, source)

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
                started = remote.start()
                first = remote.observe()
                link = next(element for element in first.elements if element.role == "link")
                password = next(element for element in first.elements if element.role == "password")
                self.assertNotIn("fixture-password", first.content)
                self.assertNotIn("描述你想完成的目标", first.content)
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
                        task=TaskSpec("embedded-runtime", "Open the order details"), arm="P", chat=chat,
                        browser=remote, control=_Control([[], [], [], []]), ledger=ledger,
                        attestation=None, step_budget=4, wall_budget_s=15,
                        run_id="embedded-react",
                    )
                    session.entry_observation = first
                    outcome = session.run()
                    result_wire = json.dumps(
                        [item.to_json() for item in channel._results.values()])
                    self.assertNotIn(secret, result_wire)
                    self.assertNotIn(secret, ledger.path.read_text())

                self.assertEqual(outcome.terminal_reason, "agent_finished")
                self.assertEqual(remote.command_count, 2)
                receipts = [item.receipt for item in channel._results.values()]
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


class _FixtureApplication:
    def __init__(self, controller_path: Path):
        self.controller_path = controller_path
        self.runtime_url = ""
        self.runtime_config = {}
        self.page2_requests = 0
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
                if self.path.startswith("/page2?"):
                    owner.page2_requests += 1
                    return self.page("<h1>Order details</h1><button>Done</button>")
                self.send_error(404)

            def page(self, content: str):
                config = json.dumps(owner.runtime_config).replace("</", "<\\/")
                html = ("<!doctype html><html><head><title>Fixture</title></head><body>" + content +
                        f"<script>window.__WOA_EMBED_CONFIG__={config}</script>" +
                        f"<script type=\"module\" src=\"{owner.runtime_url}\"></script>" +
                        "</body></html>")
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
