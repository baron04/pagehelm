from __future__ import annotations

from web_operation_agent.tests import browser_only

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from web_operation_agent.transport.gateway import EmbeddedGateway
from web_operation_agent.transport.panel_view import panel_contract
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.transport import (RemoteBrowserClient, RemoteSessionChannel,
                                        RemoteSessionRegistry, TransportError)
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


class EmbeddedRuntimeBootTests(unittest.TestCase):
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
            mounted: Boolean(runtime.panelHost), endState: runtime.endState}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["order"], ["hello", "attach", "observe"])
        self.assertTrue(result["mounted"], "the panel must exist once the server has spoken")
        self.assertTrue(result["threw"], "the caller still learns that startup failed")
        # A session the server no longer has lapsed: this Runtime stops without a word and
        # the next message brings the person's session back, rather than spinning on a
        # connection that will not come or telling them something went wrong.
        self.assertEqual(result["endState"], "lapsed")
        self.assertEqual(result["states"], [])
        self.assertEqual(result["notices"], [])


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


    def test_runtime_has_no_server_brain_or_arbitrary_javascript_surface(self):
        source = _runtime_bytes().decode("utf-8")
        self.assertNotIn("executeJavascript(", source)
        for forbidden in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SourceMap", "ControlPlan",
                          "evaluator"):
            self.assertNotIn(forbidden, source)



class _FixtureApplication:
    def __init__(self, controller_path: Path):
        self.controller_path = controller_path
        self.runtime_url = ""
        self.runtime_config = {}
        self.page2_requests = 0
        self.directory_requests = 0
        self.directory_redirect_requests = 0
        self.iframe_result_requests = 0
        self.form_submissions = 0
        owner = self

        from web_operation_agent.transport.gateway import _QuietThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def finish(self):
                try:
                    super().finish()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_GET(self):
                if self.path == "/page-controller.js":
                    return self.reply(controller_path.read_bytes(), "text/javascript")
                if self.path == "/tabs":
                    return self.page('''
                      <div role="tablist">
                        <button id="basic-tab" role="tab" aria-controls="basic-panel" aria-selected="true">Basic</button>
                        <button id="payments-tab" role="tab" aria-controls="payments-panel" aria-selected="false"
                          onclick="this.setAttribute('aria-selected','true');document.querySelector('#payments-panel').hidden=false">Payments</button>
                      </div>
                      <section id="basic-panel" role="tabpanel" aria-labelledby="basic-tab">Synthetic customer</section>
                      <section id="payments-panel" role="tabpanel" aria-labelledby="payments-tab" style="min-height:20px" hidden></section>
                    ''')
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
                if self.path == "/required-form":
                    return self.page('''
                      <h1>New contact</h1>
                      <form id="required-contact" method="post" action="/saved">
                        <label>Name <input name="name" required></label>
                        <label>Email <input name="email" type="email" required></label>
                        <button id="save-contact" type="submit" disabled
                          style="cursor:not-allowed">Save contact</button>
                      </form>
                      <script>
                        window.requiredFormEvents = {click: 0, submit: 0};
                        const form = document.querySelector('#required-contact');
                        const button = document.querySelector('#save-contact');
                        form.addEventListener('change', () => {
                          button.disabled = ![...form.querySelectorAll('input')]
                            .every(field => field.checkValidity());
                          button.style.cursor = button.disabled ? 'not-allowed' : 'pointer';
                        });
                        button.addEventListener('click', () => window.requiredFormEvents.click++);
                        form.addEventListener('submit', event => {
                          window.requiredFormEvents.submit++;
                          event.preventDefault();
                        });
                      </script>
                    ''')
                if self.path == "/large":
                    controls = "".join(
                        f'<button style="display:block;width:240px;height:28px;margin:2px">'
                        f'Action {index}</button>' for index in range(1000))
                    return self.page("<h1>Large workspace</h1>" + controls)
                if self.path.startswith("/page2?"):
                    owner.page2_requests += 1
                    return self.page("<h1>Order details</h1><button>Done</button>")
                if self.path == "/directory-redirect":
                    owner.directory_redirect_requests += 1
                    self.send_response(302)
                    self.send_header("Location", "/directory")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/directory":
                    owner.directory_requests += 1
                    return self.page('''
                      <script>history.replaceState({}, '', '/directory?group=mine')</script>
                      <h1>Project directory</h1><button>My projects</button>
                    ''')
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
                try:
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.server = _QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)



if __name__ == "__main__":
    unittest.main()
