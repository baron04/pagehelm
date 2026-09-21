"""The assistant's lifecycle under faults, in a real browser: the two invariants of D21.

I1 -- nothing the assistant does reaches the host page's own error handlers. Odoo answers an
uncaught error with its "Oops!" dialog; a timer that failed every two seconds once stacked
them until the page was black (P7.5 J5.05).

I2 -- the assistant is never silently absent. A person who can only reach a feature through
one entry must always find that entry, and it must say why when the assistant is not there
(P7.5 CR-43, reported three times with three different causes).

Everything here is real except the application: the gateway, the session registry,
`assistant.js` as built, PageController and Chromium. The host page includes `assistant.js`
the way a production application does, with `data-woa-bootstrap` naming its own endpoint,
and that endpoint is where the faults are injected. By default the host reaches the gateway
the way the single-node deployment does, through a same-origin reverse proxy under
`/assistant/`; one case puts the gateway on another origin, where a lost session cannot be
read and has to be inferred.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.transport.gateway import RUNTIME_PATH, EmbeddedGateway
from web_operation_agent.transport import RemoteSessionRegistry

PANEL_ROOT = Path(__file__).resolve().parents[2] / "web_runtime" / "panel"
_PAGE_CONTROLLER_PATH = os.environ.get("WOA_PAGE_CONTROLLER_MODULE", "")
PAGE_CONTROLLER = Path(_PAGE_CONTROLLER_PATH).expanduser() if _PAGE_CONTROLLER_PATH else None

#: The host page's own error channels, which the assistant must never reach (I1).
HOST_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>host</title>
<script>
  window.__hostErrors = []
  addEventListener('error', event => window.__hostErrors.push('error: ' + event.message))
  addEventListener('unhandledrejection',
    event => window.__hostErrors.push('rejection: ' + String(event.reason)))
</script>
<script type="module" src="%(runtime)s" data-woa-bootstrap="/bootstrap"></script>
</head><body><main><h1>Orders</h1><button>New order</button></main></body></html>
"""

#: At any moment after the grace period, one of these is on the page (I2): the standing
#: entry, or the panel's launcher or window, wherever in the shadow trees it is mounted.
PRESENT = """() => {
  if (document.getElementById('woa-assistant-entry')) return true
  const seek = root => {
    if (root.querySelector('.woa-launcher, .woa-window')) return true
    return [...root.querySelectorAll('*')].some(node => node.shadowRoot && seek(node.shadowRoot))
  }
  return seek(document)
}"""


_FORWARDED = {"authorization", "content-type", "last-event-id", "x-woa-lease", "origin"}


class _Host:
    """A host application whose bootstrap endpoint can be told to fail, and how.

    With `same_origin`, `/assistant/*` is forwarded to the gateway and a same-origin request
    without an Origin header gets the page's own, as the deployment's Caddy does.
    """

    def __init__(self, registry: RemoteSessionRegistry, *, same_origin: bool = True):
        self.registry = registry
        self.gateway: EmbeddedGateway | None = None
        self.statuses: list[int] = []   # consumed one per request; empty means succeed
        self.shared_session = False     # every page gets the same session
        self.sessions: list[str] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                if self.path.startswith("/assistant/"):
                    return self.forward("GET")
                if self.path == "/page-controller.js":
                    return self.reply(200, PAGE_CONTROLLER.read_bytes(), "text/javascript")
                if self.path == "/":
                    page = HOST_PAGE % {"runtime": owner.gateway.runtime_url}
                    return self.reply(200, page.encode(), "text/html; charset=utf-8")
                self.reply(404, b"not found", "text/plain")

            def forward(self, method: str):
                body = (self.rfile.read(int(self.headers.get("Content-Length") or 0))
                        if method == "POST" else None)
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() in _FORWARDED}
                headers.setdefault("Origin", owner.origin)
                request = urllib.request.Request(
                    owner.gateway.base_url + self.path[len("/assistant"):], data=body,
                    method=method, headers=headers)
                try:
                    response = urllib.request.urlopen(request, timeout=40)
                except urllib.error.HTTPError as refused:
                    response = refused
                payload = response.read()
                self.reply(response.status, payload,
                           response.headers.get("Content-Type", "application/octet-stream"))

            def do_POST(self):
                if self.path.startswith("/assistant/"):
                    return self.forward("POST")
                if self.path != "/bootstrap":
                    return self.reply(404, b"{}", "application/json")
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with owner.lock:
                    status = owner.statuses.pop(0) if owner.statuses else 200
                if status != 200:
                    return self.reply(status, b'{"error": "injected"}', "application/json")
                self.reply(200, json.dumps(owner.bootstrap()).encode(), "application/json")

            def reply(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.gateway = EmbeddedGateway(
            registry, sse_wait_s=0.1,
            **({"public_base_url": self.origin + "/assistant"} if same_origin else {})).start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._grant = None

    def bootstrap(self) -> dict:
        with self.lock:
            if self.shared_session and self._grant is not None:
                grant = self._grant
            else:
                grant, _channel = self.registry.create(
                    allowed_origins=[self.origin],
                    session_id=f"lifecycle-{len(self.sessions) + 1}")
                self._grant = grant
                self.sessions.append(grant.session_id)
        return {"schema_version": 1, "config": self.gateway.bootstrap_config(
            grant, page_controller_module=self.origin + "/page-controller.js")}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.gateway.close()


@unittest.skipUnless(RUNTIME_PATH.is_file(), "assistant.js is not built; run scripts/build_runtime.py")
@unittest.skipUnless((PANEL_ROOT / "panel.js").is_file(),
                     "the assistant panel is not built; run scripts/build_panel.py")
@unittest.skipUnless(PAGE_CONTROLLER is not None and PAGE_CONTROLLER.is_file(),
                     "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
class TheAssistantIsNeverSilentAndNeverLoud(unittest.TestCase):
    def setUp(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        self.host = self.make_host()
        self.addCleanup(self.host.close)
        self._playwright = sync_playwright().start()
        self.addCleanup(self._playwright.stop)
        self.browser = self._playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)

    def make_host(self, *, same_origin: bool = True) -> _Host:
        return _Host(RemoteSessionRegistry(), same_origin=same_origin)

    def open(self, *, session_storage: dict | None = None):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        if session_storage:
            # What duplicating a tab does: the copy starts with the original's sessionStorage.
            page.add_init_script(
                "for (const [key, value] of Object.entries(%s))"
                " sessionStorage.setItem(key, value)" % json.dumps(session_storage))
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(self.host.origin + "/", wait_until="domcontentloaded")
        return page, page_errors

    def assert_quiet(self, page, page_errors):
        """I1: the host heard nothing."""
        self.assertEqual(page.evaluate("() => window.__hostErrors"), [])
        self.assertEqual(page_errors, [])

    def watch_presence(self, page, seconds: float):
        """I2: sample the page for `seconds`; the assistant or its entry is always there."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.assertTrue(page.evaluate(PRESENT), "the assistant was silently absent")
            page.wait_for_timeout(200)

    def launcher(self, page):
        return page.locator(".woa-launcher")

    def entry_sentence(self, page) -> str:
        page.locator("#woa-assistant-entry").wait_for(state="attached", timeout=8000)
        return page.evaluate("""() => document.getElementById('woa-assistant-entry')
          .shadowRoot.querySelector('.note p').textContent""")

    def entry_note_hidden(self, page) -> bool:
        return page.evaluate("""() => document.getElementById('woa-assistant-entry')
          .shadowRoot.querySelector('.note').hidden""")

    def test_a_refused_bootstrap_leaves_an_entry_that_says_why(self):
        self.host.statuses = [403]
        page, errors = self.open()
        self.assertEqual(self.entry_sentence(page), "当前账号未获授权使用助手（403）。")
        self.assertTrue(self.entry_note_hidden(page))
        page.evaluate("""() => document.getElementById('woa-assistant-entry')
          .shadowRoot.querySelector('.badge').click()""")
        self.assertFalse(self.entry_note_hidden(page))
        # Nothing another attempt could fix is retried on its own.
        page.wait_for_timeout(2500)
        self.assertEqual(self.host.sessions, [])
        self.watch_presence(page, 1)
        self.assert_quiet(page, errors)

    def test_an_outage_is_shown_then_recovered_from_without_the_person(self):
        self.host.statuses = [503, 503]
        page, errors = self.open()
        self.assertIn("正在重试（503）", self.entry_sentence(page))
        self.watch_presence(page, 1)
        self.launcher(page).wait_for(timeout=15_000)
        page.locator("#woa-assistant-entry").wait_for(state="detached", timeout=5000)
        self.assertEqual(len(self.host.sessions), 1)
        self.assert_quiet(page, errors)

    def test_a_session_that_ends_under_the_page_is_replaced_by_a_new_one(self):
        """The agent restarted, or the session expired: the page reconnects on its own."""
        page, errors = self.open()
        self.launcher(page).wait_for(timeout=15_000)
        page.wait_for_timeout(10_500)   # a session that has run long enough to be trusted
        self.host.registry.retire(self.host.sessions[0], "session_closed")
        page.wait_for_function(
            "() => window.__WOA_ASSISTANT__?.state?.kind === 'running'"
            " && window.__WOA_ASSISTANT__.state.runtime.sessionId === 'lifecycle-2'",
            timeout=20_000)
        self.launcher(page).wait_for(timeout=10_000)
        self.watch_presence(page, 1)
        self.assert_quiet(page, errors)

    def test_across_origins_a_network_fault_never_times_the_connection_out(self):
        """The gateway on another origin: a retired session's answer carries no CORS header,
        so the browser sees an outage, not `unknown_session`. A network fault must keep the
        existing panel and reconnect loop alive past the former thirty-second cutoff."""
        self.host.close()
        self.host = self.make_host(same_origin=False)
        page, errors = self.open()
        self.launcher(page).wait_for(timeout=15_000)
        page.wait_for_timeout(10_500)
        self.host.registry.retire(self.host.sessions[0], "session_closed")
        page.wait_for_timeout(32_000)
        self.assertEqual(len(self.host.sessions), 1)
        self.assertEqual(page.evaluate("() => window.__WOA_ASSISTANT__.state.kind"), "running")
        self.assertEqual(page.evaluate(
            "() => window.__WOA_ASSISTANT__.state.runtime.panelHost.snapshot().connection.state"),
            "reconnecting")
        self.watch_presence(page, 1)
        self.assert_quiet(page, errors)

    def test_a_session_that_never_holds_keeps_a_capped_retry_cadence(self):
        """Every new session ends at once: retries continue, but the delay stays bounded."""
        page, errors = self.open()
        self.launcher(page).wait_for(timeout=15_000)
        stop = threading.Event()

        def retire_every_session():
            while not stop.is_set():
                for session_id in list(self.host.sessions):
                    self.host.registry.retire(session_id, "session_closed")
                time.sleep(0.3)

        reaper = threading.Thread(target=retire_every_session, daemon=True)
        reaper.start()
        self.addCleanup(stop.set)
        deadline = time.monotonic() + 40
        while len(self.host.sessions) <= 4 and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertGreater(len(self.host.sessions), 4)
        count = len(self.host.sessions)
        page.wait_for_timeout(9000)
        self.assertGreater(len(self.host.sessions), count)
        self.assertLessEqual(len(self.host.sessions), count + 2)
        self.assertIn("正在重试", self.entry_sentence(page))
        self.assert_quiet(page, errors)

    def test_a_session_another_page_took_over_is_left_to_that_page(self):
        """The one ending that must not reconnect: two pages would take it in turns.

        A duplicated tab carries the original's sessionStorage, so it is the same browser
        client with a new Runtime, and the server hands it the session.
        """
        self.host.shared_session = True
        first, first_errors = self.open()
        self.launcher(first).wait_for(timeout=15_000)
        stored = first.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
        second, second_errors = self.open(session_storage=stored)
        self.launcher(second).wait_for(timeout=15_000)
        first.wait_for_function(
            "() => window.__WOA_ASSISTANT__?.state?.kind === 'settled'", timeout=15_000)
        first.wait_for_timeout(3000)
        self.assertEqual(first.evaluate("() => window.__WOA_ASSISTANT__.state.reason"), "replaced")
        self.assertEqual(second.evaluate("() => window.__WOA_ASSISTANT__.state.kind"), "running")
        # The first page keeps the panel that says what happened; it is not absent.
        self.watch_presence(first, 1)
        self.assert_quiet(first, first_errors)
        self.assert_quiet(second, second_errors)


if __name__ == "__main__":
    unittest.main()
