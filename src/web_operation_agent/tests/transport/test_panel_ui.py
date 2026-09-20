"""Chromium checks for the assistant panel.

These drive the built panel against a scripted host, so every state can be rendered without
a live session: the states a real run reaches rarely -- indeterminate, replaced, obscured
confirmation -- are exactly the ones worth pinning.

A checkout without the built panel skips with the prerequisite named. It is never reported
as a pass.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_operation_agent.transport.panel_view import design_tokens, panel_contract
from web_operation_agent.transport import RemoteSessionChannel

PANEL_ROOT = Path(__file__).resolve().parents[2] / "web_runtime" / "panel"
PANEL_BUNDLE = PANEL_ROOT / "panel.js"

DRIVER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>panel harness</title>
<style>body{margin:0;height:2000px;font-family:system-ui}</style></head>
<body>
<script id="configuration" type="application/json">__CONFIGURATION__</script>
<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script type="module" src="/driver.js"></script>
</body></html>
"""

# External on purpose: under `script-src 'self'` an inline module would be refused, and the
# refusal would be the harness's, not the panel's.
DRIVER_JS = """
import {mountPanel} from './panel.js'

// If the panel installs its own global handler this stops being the page's.
const pageHandler = () => {}
window.onerror = pageHandler
window.__onerrorIsOurs = () => window.onerror === pageHandler

const configuration = JSON.parse(document.getElementById('configuration').textContent)
let snapshot = JSON.parse(document.getElementById('snapshot').textContent)
const listeners = new Set()
window.__calls = []

// A class, and every method reads `this` -- because the real `PanelHost` is a class and
// every one of its methods reads `this`. The double used to be an object literal of
// receiver-free arrows, so `onClick={host.newConversation}` worked here and threw in
// production: the panel handed React a bare function reference, the real host lost its
// receiver, and the TypeError escaped into the host application, which raised its own
// "something went wrong" dialog. A double more forgiving than the thing it stands in for
// does not test the thing it stands in for.
class ScriptedHost {
  constructor(configuration) { this.configuration = configuration }
  snapshot() { return this.read() }
  read() { return snapshot }
  subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) }
  submit(text) { this.note('submit', text) }
  retrySubmit() { this.note('retrySubmit') }
  cancel() { this.note('cancel') }
  decide(id, decision) { this.note('decide', id, decision) }
  // The real store clears the view here before it tells the server. A double that only
  // recorded the call would let the panel pass a test the product would fail.
  newConversation() {
    this.note('newConversation')
    window.__setSnapshot({entries: [], pendingApproval: null, notice: null, optimistic: null,
                          hasOlder: false})
  }
  loadOlder(before) { this.note('loadOlder', before) }
  dismissNotice() { this.note('dismissNotice') }
  reload() { this.note('reload') }
  note(...args) { window.__calls.push(args) }
}

const host = new ScriptedHost(configuration)
window.__setSnapshot = patch => {
  snapshot = {...snapshot, ...patch}
  for (const listener of listeners) listener(snapshot)
}
window.__host = host
window.__panel = mountPanel({host, allowedOrigins: [location.origin]})
"""


def _snapshot(**patch) -> dict:
    base = {
        "connection": {"state": "ready", "busy": False, "stopping": False, "online": True},
        "entries": [],
        "pendingApproval": None,
        "notice": None,
        "hasOlder": False,
        "loadingOlder": False,
        "closed": False,
        "optimistic": None,
    }
    base.update(patch)
    return base


def _entry(identifier: str, kind: str, text: str, **extra) -> dict:
    entry = {"id": identifier, "sequence": int(identifier[1:] or 1), "turn_id": "turn-1",
             "at": 1_757_000_000_000, "kind": kind, "tone": "neutral", "text": text}
    entry.update(extra)
    return entry


# What a host application locking itself down would send. The panel must construct under
# it: no inline script, no injected <style>, no Trusted Types policy of its own.
STRICT_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
              "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; "
              "require-trusted-types-for 'script'; trusted-types 'none'")


class _Harness:
    def __init__(self, snapshot: dict, *, csp: str = ""):
        configuration = RemoteSessionChannel(
            "panel-harness", ("http://127.0.0.1",))._panel_configuration()
        page = (DRIVER_HTML
                .replace("__CONFIGURATION__", json.dumps(configuration))
                .replace("__SNAPSHOT__", json.dumps(snapshot)))
        body = page.encode()
        driver = DRIVER_JS.encode()
        bundle = PANEL_BUNDLE.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                if self.path == "/panel.js":
                    return self.reply(bundle, "text/javascript; charset=utf-8")
                if self.path == "/driver.js":
                    return self.reply(driver, "text/javascript; charset=utf-8")
                if self.path in {"/", "/index.html"}:
                    return self.reply(body, "text/html; charset=utf-8")
                self.send_error(404)

            def reply(self, payload: bytes, content_type: str):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if csp and content_type.startswith("text/html"):
                    self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@unittest.skipUnless(PANEL_BUNDLE.is_file(),
                     "the assistant panel is not built; run scripts/build_panel.py")
class _PanelBrowser(unittest.TestCase):
    def setUp(self):
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        self._playwright = sync_playwright().start()
        try:
            self.browser = self._playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            self._playwright.stop()
            if "Executable doesn't exist" in str(error):
                self.skipTest("Playwright Chromium is not installed")
            raise
        self.addCleanup(self._playwright.stop)
        self.addCleanup(self.browser.close)

    def open(self, snapshot: dict, *, width: int = 1280, height: int = 800,
             reduced_motion: str = "no-preference", color_scheme: str = "light",
             csp: str = "", device_scale_factor: float = 1):
        harness = _Harness(snapshot, csp=csp)
        self.addCleanup(harness.close)
        context = self.browser.new_context(viewport={"width": width, "height": height},
                                           reduced_motion=reduced_motion,
                                           color_scheme=color_scheme,
                                           device_scale_factor=device_scale_factor)
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(harness.origin + "/", wait_until="load")
        # Waiting on a locator rather than `wait_for_function`: the latter evaluates a
        # string in the page, which a strict CSP correctly refuses. A pending confirmation
        # opens the window straight away, so either surface counts as mounted.
        page.locator(".woa-launcher, .woa-window").first.wait_for(timeout=8000)
        return page

    PENDING = {"schema_version": 1, "approval_id": "a-1",
               "effect": "确认报价单 S00002，使其成为销售订单",
               "targets": ["sale.order · S00002 · Acme Corporation", "state · draft → sale"],
               "expires_at": 0, "principal_digest": "", "turn_id": "turn-1",
               "operation_id": "op-1"}

    def raise_approval(self, page, **overrides) -> None:
        """Publish a confirmation the way the session does, after the panel is up."""
        request = {**self.PENDING, **overrides}
        page.evaluate("request => window.__setSnapshot({pendingApproval:"
                      "Object.assign(request, {expires_at: Date.now() + 120000})})", request)
        page.locator(".woa-approval").wait_for(timeout=5000)

    def calls(self, page) -> list:
        return page.evaluate("window.__calls")


class PanelUITests(_PanelBrowser):
    # ---------------------------------------------------------------- launcher
    def test_the_launcher_carries_the_state_the_header_no_longer_shows(self):
        page = self.open(_snapshot())
        launcher = page.locator(".woa-launcher")
        launcher.wait_for()
        self.assertEqual(launcher.get_attribute("data-state"), "idle")
        page.evaluate(
            "window.__setSnapshot({connection:{state:'acting',busy:true,stopping:false,online:true}})")
        self.assertEqual(launcher.get_attribute("data-state"), "working")
        page.evaluate("""window.__setSnapshot({
            connection:{state:'replaced',busy:false,stopping:false,online:true}})""")
        self.assertEqual(launcher.get_attribute("data-state"), "closed")
        self.assertEqual(self.errors, [])

    def test_a_confirmation_opens_the_panel_and_marks_the_launcher_when_put_away(self):
        """An unanswered confirmation is not something to leave behind a badge."""
        page = self.open(_snapshot())
        page.locator(".woa-launcher").wait_for()
        self.raise_approval(page)
        page.locator(".woa-window").wait_for()
        # Put it away deliberately and the launcher says there is still something waiting.
        page.get_by_role("button", name=panel_contract()["label_keys"]["collapse"]).click()
        launcher = page.locator(".woa-launcher")
        launcher.wait_for()
        self.assertEqual(launcher.get_attribute("data-state"), "awaiting")
        self.assertEqual(launcher.locator(".woa-launcher__nub").inner_text(), "1")
        self.assertEqual(self.errors, [])

    def test_panel_interactions_do_not_reach_the_host_application_event_delegates(self):
        page = self.open(_snapshot())
        page.evaluate("""() => {
          window.__escapedPanelEvents = []
          for (const type of ['click', 'pointerdown', 'keydown', 'input', 'submit']) {
            document.addEventListener(type, () => window.__escapedPanelEvents.push(type))
          }
        }""")

        page.locator(".woa-launcher").click()
        composer = page.locator("#woa-composer-input")
        composer.fill("列出客户")
        page.keyboard.press("Enter")
        page.get_by_role(
            "button", name=panel_contract()["label_keys"]["collapse"]).click()

        self.assertEqual(page.evaluate("window.__escapedPanelEvents"), [])
        self.assertEqual(self.calls(page), [["submit", "列出客户"]])
        page.locator(".woa-launcher").wait_for(timeout=3000)
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------------- default state
    def test_opening_shows_the_default_state_and_a_suggestion_only_fills_the_composer(self):
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        empty = page.locator(".woa-empty")
        empty.wait_for()
        labels = panel_contract()["label_keys"]
        self.assertIn(labels["emptyGreeting"], empty.inner_text())
        suggestions = page.locator(".woa-suggestions button")
        self.assertEqual(suggestions.count(), 3)
        suggestions.nth(2).click()
        composer = page.locator("#woa-composer-input")
        self.assertIn("S00002", composer.input_value())
        # Filling is not submitting: a write must never start from one stray click.
        self.assertEqual(self.calls(page), [])
        self.assertEqual(self.errors, [])

    def test_a_clarification_releases_and_focuses_the_composer_for_the_reply(self):
        page = self.open(_snapshot(
            connection={"state": "needs_input", "busy": False,
                        "stopping": False, "online": True},
            entries=[
                _entry("e1", "goal", "打开 Acme"),
                _entry("e2", "result", "请选择：\n1. Acme — 伦敦\n2. Acme — 巴黎",
                       panel_state="needs_input", tone="warning"),
            ],
        ))
        page.locator(".woa-launcher").click()
        composer = page.locator("#woa-composer-input")
        composer.wait_for()

        self.assertTrue(composer.is_enabled())
        self.assertEqual(
            page.evaluate("document.querySelector('#woa-assistant').shadowRoot.activeElement.id"),
            "woa-composer-input",
        )
        self.assertIn("Acme — 伦敦", page.locator(".woa-thread").inner_text())
        self.assertEqual(page.locator(".woa-window").get_attribute("data-state"),
                         "needs_input")
        self.assertEqual(self.errors, [])

    def test_an_arriving_result_preserves_the_unsent_draft_focus_and_host_route(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "先查询报价单"),
            _entry("e2", "result", "已完成第一轮。", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        composer = page.locator("#woa-composer-input")
        composer.fill("这段话还没有发送")
        composer.focus()
        route = page.url

        page.evaluate("""() => window.__setSnapshot({entries:[
          {id:'e1',turn_id:'turn-1',kind:'goal',text:'先查询报价单',at:1,
           panel_state:'acting',tone:'neutral',replayed:false},
          {id:'e2',turn_id:'turn-1',kind:'result',text:'已完成第一轮。',at:2,
           panel_state:'success',tone:'success',replayed:false},
          {id:'e3',turn_id:'turn-2',kind:'goal',text:'后台到达的另一轮',at:3,
           panel_state:'acting',tone:'neutral',replayed:false},
          {id:'e4',turn_id:'turn-2',kind:'result',text:'另一轮也已完成。',at:4,
           panel_state:'success',tone:'success',replayed:false}
        ]})""")
        page.wait_for_timeout(300)

        self.assertEqual(composer.input_value(), "这段话还没有发送")
        self.assertEqual(page.url, route)
        self.assertEqual(
            page.evaluate("document.querySelector('#woa-assistant').shadowRoot.activeElement.id"),
            "woa-composer-input",
        )
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------------- approval
    def test_the_approval_card_is_keyboard_operable_and_escape_cancels(self):
        page = self.open(_snapshot())
        self.raise_approval(page)
        card = page.locator(".woa-approval")
        self.assertIn("S00002", card.inner_text())
        focused = page.evaluate(
            "document.querySelector('#woa-assistant').shadowRoot.activeElement.textContent")
        self.assertEqual(focused, panel_contract()["label_keys"]["deny"])
        # Tab is trapped between the two answers; nothing else is reachable from the card.
        page.keyboard.press("Tab")
        self.assertEqual(
            page.evaluate("document.querySelector('#woa-assistant').shadowRoot"
                          ".activeElement.getAttribute('data-testid')"),
            "woa-approve")
        page.keyboard.press("Escape")
        self.assertEqual([call[0] for call in self.calls(page)], ["decide"])
        self.assertEqual(self.calls(page)[0][2], "denied")
        self.assertEqual(self.errors, [])

    def test_a_page_switch_card_has_one_control_and_escape_does_not_decline(self):
        """The assistant asks before it leaves the page. The card offers the confirmation
        and nothing else: a person who does not want to leave stops the task."""
        labels = panel_contract()["label_keys"]
        page = self.open(_snapshot())
        self.raise_approval(page, kind="navigation", effect="离开当前页面，打开另一个页面",
                            targets=["将打开：/odoo/customers/13", "你的请求：打开 azur"])
        card = page.locator(".woa-approval")
        self.assertEqual(card.get_attribute("data-kind"), "navigation")
        self.assertIn(labels["navigationTitle"], card.inner_text())
        self.assertIn(labels["navigationHint"], card.inner_text())
        self.assertIn("/odoo/customers/13", card.inner_text())
        self.assertEqual(page.get_by_test_id("woa-deny").count(), 0)
        self.assertEqual(page.get_by_test_id("woa-approve").inner_text(),
                         labels["navigationConfirm"])
        focused = page.evaluate(
            "document.querySelector('#woa-assistant').shadowRoot"
            ".activeElement.getAttribute('data-testid')")
        self.assertEqual(focused, "woa-approve")
        page.keyboard.press("Escape")
        page.keyboard.press("Tab")
        self.assertEqual(self.calls(page), [], "neither key may answer a page switch")
        page.wait_for_timeout(700)
        page.get_by_test_id("woa-approve").click()
        self.assertEqual([(call[0], call[2]) for call in self.calls(page)],
                         [("decide", "approved")])
        self.assertEqual(self.errors, [])

    def test_the_approval_card_stays_usable_at_320_by_480(self):
        page = self.open(_snapshot(), width=320, height=480)
        self.raise_approval(page, effect="新建客户 Nordwind Handelsgesellschaft mbH & Co. KG",
                            targets=["res.partner · 新建一条客户记录"])
        window = page.locator(".woa-window").bounding_box()
        self.assertIsNotNone(window)
        assert window is not None
        # The window never leaves the viewport, however small the viewport is.
        self.assertGreaterEqual(window["x"], 0)
        self.assertGreaterEqual(window["y"], 0)
        self.assertLessEqual(window["x"] + window["width"], 321)
        self.assertLessEqual(window["y"] + window["height"], 481)
        for name in (panel_contract()["label_keys"]["deny"],
                     panel_contract()["label_keys"]["approve"]):
            button = page.get_by_role("button", name=name)
            self.assertTrue(button.is_visible(), name)
            box = button.bounding_box()
            assert box is not None
            self.assertGreaterEqual(box["height"], 28, name)
        # Under 360px the two answers stack, and the safe one sits nearest the thumb.
        deny = page.get_by_role("button", name=panel_contract()["label_keys"]["deny"]).bounding_box()
        approve = page.get_by_test_id("woa-approve").bounding_box()
        assert deny is not None and approve is not None
        self.assertGreater(deny["y"], approve["y"])
        self.assertEqual(self.errors, [])

    def test_an_expired_confirmation_stops_offering_buttons_and_frees_the_composer(self):
        """Past the deadline the server takes no answer, so the card must not ask for one.

        It did: the countdown reached 0:00 and the card sat there looking answerable, with
        the composer locked behind it. Pressing it made the runtime treat the 409 as "another
        page took this session over", stop, and -- after a reload, which replayed the same
        doomed decision -- never mount the panel at all (P7.5 CR-38).
        """
        page = self.open(_snapshot())
        self.raise_approval(page)
        composer = page.locator("textarea")
        self.assertTrue(composer.is_disabled(), "a live confirmation blocks the composer")

        page.evaluate("request => window.__setSnapshot({pendingApproval: request})",
                      {**self.PENDING, "expires_at": 0})
        page.wait_for_timeout(1200)

        self.assertEqual(page.locator("[data-testid=woa-approve]").count(), 0)
        self.assertEqual(page.locator("[data-testid=woa-deny]").count(), 0)
        self.assertTrue(page.locator(".woa-approval").is_visible(),
                        "what was asked stays readable")
        self.assertIn("已超时", page.locator(".woa-approval__blocked[data-expired]").inner_text())
        self.assertFalse(composer.is_disabled(), "the person can type again")

    def test_an_approval_that_is_not_visible_refuses_and_says_why(self):
        page = self.open(_snapshot())
        self.raise_approval(page, effect="为 S00007 开票", targets=["sale.order · S00007"])
        # Clicking before the card has been visible for its dwell is refused; the reason is
        # shown rather than the press silently doing nothing.
        page.get_by_test_id("woa-approve").dispatch_event("click")
        page.locator(".woa-approval__blocked").wait_for(timeout=3000)
        self.assertIn(panel_contract()["label_keys"]["approvalObscured"],
                      page.locator(".woa-approval__blocked").inner_text())
        self.assertEqual(self.calls(page), [])
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------------- answers
    def test_only_model_authored_prose_is_revealed_progressively(self):
        server = _entry("e2", "result", "3 张销售订单待开票。", authored="server",
                        panel_state="success")
        page = self.open(_snapshot(entries=[_entry("e1", "goal", "列出待开票订单"), server]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-md").wait_for()
        # A server projection is a fact the server computed; it appears whole.
        self.assertNotIn("woa-md__caret", page.locator(".woa-md").inner_html())
        page.evaluate("""window.__setSnapshot({entries:[
            {id:'e1',sequence:1,turn_id:'turn-2',at:0,kind:'goal',tone:'neutral',text:'再说一次'},
            {id:'e3',sequence:3,turn_id:'turn-2',at:0,kind:'result',tone:'neutral',
             authored:'model',panel_state:'success',
             text:'不是。这个页面背后的动作本身没有存任何 domain，它列的是全部销售订单。'}]})""")
        page.locator(".woa-md__caret").wait_for(timeout=3000)
        page.locator(".woa-md").last.click()
        page.wait_for_function(
            "!document.querySelector('#woa-assistant').shadowRoot.querySelector('.woa-md__caret')",
            timeout=3000)
        self.assertIn("全部销售订单", page.locator(".woa-md").last.inner_text())
        self.assertEqual(self.errors, [])

    def test_markdown_renders_tables_and_never_links_a_foreign_origin(self):
        answer = (
            "3 张销售订单待开票：\n\n"
            "| 单号 | 客户 | 金额 |\n| --- | --- | ---: |\n"
            "| S00007 | Deco Addict | 1,231.00 |\n\n"
            "见 [本地记录](/odoo/sales) 与 [外部站点](https://evil.example/steal)。\n\n"
            "```python\nprint('hello')\n```\n")
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出待开票订单"),
            _entry("e2", "result", answer, authored="server", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-md table").wait_for()
        self.assertEqual(page.locator(".woa-md table td").count(), 3)
        self.assertEqual(page.locator(".woa-md table td").nth(2).get_attribute("data-align"),
                         "right")
        # Untrusted output cannot mint a clickable link out of this session's origins.
        hrefs = page.locator(".woa-md a").evaluate_all("nodes => nodes.map(n => n.href)")
        self.assertEqual(len(hrefs), 1)
        self.assertIn("/odoo/sales", hrefs[0])
        self.assertIn("外部站点", page.locator(".woa-md__inert").inner_text())
        self.assertIn("print('hello')", page.locator(".woa-md__block pre").inner_text())
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------------- failures
    def test_retry_is_offered_only_for_a_failure_that_can_be_retried(self):
        page = self.open(_snapshot(notice={"code": "", "retryable": True}))
        page.locator(".woa-launcher").click()
        notice = page.locator(".woa-inline-error")
        notice.wait_for()
        self.assertEqual(notice.locator(".woa-inline-error__retry").count(), 1)
        page.evaluate(
            "window.__setSnapshot({notice:{code:'runtime_replaced',retryable:false}})")
        self.assertEqual(notice.get_attribute("data-terminal"), "true")
        self.assertEqual(notice.locator(".woa-inline-error__retry").count(), 0)
        # It names where the assistant went, because this is the one ending the page
        # does not reconnect from: the person continues in the other page.
        self.assertIn("另一个页面", notice.inner_text())
        self.assertEqual(self.errors, [])

    def test_a_failure_with_no_guidance_of_its_own_is_not_called_a_lost_connection(self):
        """`gateway_error` is the server falling over, not the network going away.

        The fallback used to be the reconnecting line, so any code the contract had not
        anticipated told the operator their connection had dropped and that nothing had
        been re-run -- two claims about a failure nobody had established the shape of.
        """
        notices = panel_contract()["notices"]
        page = self.open(_snapshot(notice={"code": "gateway_error", "retryable": True}))
        page.locator(".woa-launcher").click()
        notice = page.locator(".woa-inline-error")
        notice.wait_for()
        self.assertIn(notices["gateway_error"], notice.inner_text())
        page.evaluate("window.__setSnapshot({notice:{code:'nothing_maps_this',retryable:true}})")
        self.assertIn(notices["unexpected"], notice.inner_text())
        self.assertNotIn(notices["reconnecting"], notice.inner_text())
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------------- the work process
    def test_a_long_table_keeps_the_last_row_and_its_source(self):
        text = ("| Name | Email |\n| --- | --- |\n" + "\n".join(
            f"| Synthetic Contact {i:03} Demonstration Department Research Group | contact-{i:03}@synthetic.example.test |"
            for i in range(1, 51)) + "\n\n> source: all rows in this read")
        self.assertGreater(len(text), 4000)
        page = self.open(_snapshot(entries=[_entry("e1", "result", text, panel_state="success")]))
        page.locator(".woa-launcher").click()
        self.assertEqual(page.locator(".woa-md tbody tr").count(), 50)
        self.assertIn("contact-050@synthetic.example.test", page.locator(".woa-md").inner_text())
        self.assertIn("source: all rows in this read", page.locator(".woa-md").inner_text())
        self.assertEqual(self.errors, [])

    def test_source_details_expand_by_keyboard_without_hiding_partial_answer(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "未确认报价合计？"),
            _entry("e2", "result", "**小计**：12.50 USD\n\n这是部分回答", panel_state="success",
                   source_details="读取时间：2026-09-13T00:00:00Z\n\n扫描记录：2；匹配记录：2"),
        ]), width=390, height=844)
        page.locator(".woa-launcher").click()
        details = page.locator(".woa-answer-sources")
        summary = details.locator("summary")
        self.assertEqual(summary.inner_text(), panel_contract()["label_keys"]["answerSources"])
        self.assertFalse(details.evaluate("node => node.open"))
        self.assertTrue(page.get_by_text("这是部分回答", exact=True).is_visible())
        summary.focus()
        summary.press("Enter")
        self.assertTrue(details.evaluate("node => node.open"))
        self.assertIn("2026-09-13", details.inner_text())
        summary.press("Enter")
        self.assertFalse(details.evaluate("node => node.open"))
        self.assertEqual(self.errors, [])

    def test_a_turn_that_only_ran_shows_its_time_and_a_turn_that_acted_shows_its_steps(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出所有待开票的销售订单"),
            _entry("e2", "progress", "正在理解目标"),
            _entry("e3", "result", "十六张。", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-took").wait_for()
        # Nothing happened that a list could show, so there is no list and no control
        # promising one -- only how long it took.
        self.assertEqual(page.locator(".woa-work").count(), 0)
        self.assertIn(panel_contract()["label_keys"]["elapsedPrefix"],
                      page.locator(".woa-took").inner_text())

        page.evaluate("""() => window.__setSnapshot({entries: [
          {id:'f1',sequence:1,turn_id:'t2',at:1757000000000,kind:'goal',tone:'neutral',
           text:'打开 Acme 的客户记录'},
          {id:'f2',sequence:2,turn_id:'t2',at:1757000000000,kind:'progress',tone:'active',
           text:'正在理解目标'},
          {id:'f3',sequence:3,turn_id:'t2',at:1757000002000,kind:'action',tone:'active',
           text:'打开页面：/odoo/customers/10'},
          {id:'f4',sequence:4,turn_id:'t2',at:1757000003000,kind:'result',tone:'neutral',
           text:'已打开。',panel_state:'success'}]})""")
        # A turn that did something shows what it did, without being asked to.
        page.locator(".woa-step").first.wait_for()
        self.assertEqual(page.locator(".woa-step").count(), 2)
        self.assertEqual(self.errors, [])

    def test_a_running_turn_shows_its_steps_in_the_flow_rather_than_behind_a_control(self):
        """What it is doing is the conversation while it runs, not a box to go open.

        A read-only question spends most of its wall clock inside model calls and typed
        reads. The panel held one line -- 正在理解目标 -- for all of it, behind a summary
        that repeated that same line, and the person could not tell working from stuck.
        """
        page = self.open(_snapshot(
            connection={"state": "acting", "busy": True, "stopping": False, "online": True},
            entries=[
                _entry("e1", "goal", "检查名为 Acme 的客户是否存在"),
                _entry("e2", "progress", "正在思考", tone="active"),
                _entry("e3", "progress", "正在读取业务数据", tone="active"),
                _entry("e4", "progress", "已读取 3 条：客户", tone="active"),
            ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-step").first.wait_for()
        # Every step is on screen, in the order it happened, with nothing to go and open.
        self.assertEqual([line.strip() for line in
                          page.locator(".woa-step__text").all_inner_texts()],
                         ["正在思考", "正在读取业务数据", "已读取 3 条：客户"])
        # Only the newest node is running; the ones before it completed.
        self.assertEqual(page.locator(".woa-step[data-status='running']").count(), 1)
        self.assertEqual(
            page.locator(".woa-step[data-status='running'] .woa-spinner").count(), 1)
        self.assertEqual(page.locator(".woa-step[data-status='done']").count(), 2)
        self.assertEqual(self.errors, [])

    def test_a_settled_turn_keeps_its_timeline_and_opens_a_node_onto_the_call(self):
        """P7.5: the record of how an answer was produced outlives the answer.

        Folding the steps away once the turn landed took the answer's only checkable part
        with it: which call produced the numbers, with what arguments, and what came back.
        """
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "未确认报价单里已发送的合计多少钱？", at=1_757_000_000_000),
            _entry("e2", "progress", "正在读取业务数据", tone="active",
                   at=1_757_000_002_000,
                   step={"stage": "tool", "tool": "query_quotations", "arguments": "limit=50"}),
            _entry("e3", "progress", "已读取 3 条：未确认、未取消的报价单", tone="active",
                   at=1_757_000_005_000,
                   step={"stage": "read_done", "tool": "query_quotations",
                         "result": "3 条 · 未确认、未取消的报价单"}),
            _entry("e4", "result", "合计：504.00 USD", panel_state="success",
                   at=1_757_000_006_000),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-step").first.wait_for()
        self.assertEqual(page.locator(".woa-step").count(), 2)
        self.assertEqual(page.locator(".woa-step[data-status='done']").count(), 2)
        # Each node says how long that step took, and the first one took three seconds.
        self.assertEqual(page.locator(".woa-step__elapsed").first.inner_text(), "3s")
        # Opening a node shows what was called and with what -- the panel invents none of
        # it; every field is projected and bounded by the server.
        page.locator(".woa-step__line").first.click()
        detail = page.locator(".woa-step__detail").first
        detail.wait_for()
        self.assertIn("query_quotations", detail.inner_text())
        self.assertIn("limit=50", detail.inner_text())
        self.assertEqual(self.errors, [])

    def test_a_turn_that_ended_badly_marks_the_step_it_ended_on(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "把这张报价单确认掉", at=1_757_000_000_000),
            _entry("e2", "progress", "正在读取业务数据", tone="active",
                   at=1_757_000_001_000, step={"stage": "tool", "tool": "query_quotations"}),
            _entry("e3", "progress", "正在准备这次更改", tone="active",
                   at=1_757_000_003_000, step={"stage": "tool", "tool": "confirm_quotation"}),
            _entry("e4", "result", "你的账号没有执行这项操作的权限。", panel_state="refused",
                   at=1_757_000_004_000),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-step").first.wait_for()
        self.assertEqual(page.locator(".woa-step[data-status='failed']").count(), 1)
        self.assertEqual(
            page.locator(".woa-step[data-status='failed'] .woa-step__text").inner_text(),
            "正在准备这次更改")
        self.assertEqual(page.locator(".woa-step[data-status='done']").count(), 1)
        self.assertEqual(self.errors, [])

    def test_elapsed_time_rolls_over_without_showing_sixty_seconds(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "打开客户", at=1_757_000_000_000),
            _entry("e2", "action", "query_customers", tone="active",
                   at=1_757_000_001_000),
            _entry("e3", "result", "已打开。", panel_state="success",
                   at=1_757_000_119_600),
        ]))
        page.locator(".woa-launcher").click()
        elapsed_text = page.locator(".woa-took").inner_text()
        self.assertIn("2m 0s", elapsed_text)
        self.assertNotIn("60s", elapsed_text)
        self.assertEqual(self.errors, [])

    def test_the_newest_line_stays_in_view_without_being_asked(self):
        """Arriving content is not a reason to stop following it."""
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出所有待开票的销售订单"),
            _entry("e2", "result", "\n".join(f"第 {index} 行。" for index in range(40)),
                   panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-thread").wait_for()
        page.wait_for_timeout(400)
        self.assertEqual(page.locator(".woa-jump").count(), 0)

        # The echo of a message that has not reached the server yet grows the thread
        # without adding an entry. That is exactly the growth the old effect could not
        # see, and it is what left the reader parked above a button.
        page.evaluate("() => window.__setSnapshot({optimistic:"
                      "{turnId:'t2',text:'再列一次',failed:false}})")
        page.wait_for_timeout(400)
        self.assertEqual(page.locator(".woa-jump").count(), 0)
        distance = page.evaluate("""() => { const t = document.getElementById('woa-assistant')
            .shadowRoot.querySelector('.woa-thread')
            return t.scrollHeight - t.scrollTop - t.clientHeight }""")
        self.assertLessEqual(distance, 30)
        self.assertEqual(self.errors, [])

    def test_the_jump_control_is_anchored_and_the_latest_answer_has_breathing_room(self):
        answer = "\n\n".join(f"第 {index} 行内容。" for index in range(80))
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "给我一份长清单"),
            _entry("e2", "result", answer, panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        thread = page.locator(".woa-thread")
        thread.evaluate("node => { node.scrollTop = 0 }")
        page.locator(".woa-jump").wait_for()
        before = page.locator(".woa-jump").bounding_box()
        thread.evaluate("node => { node.scrollTop = Math.min(10, node.scrollHeight) }")
        after = page.locator(".woa-jump").bounding_box()
        assert before is not None and after is not None
        self.assertAlmostEqual(before["y"], after["y"], delta=1)

        page.locator(".woa-jump").click()
        page.wait_for_timeout(500)
        turn = page.locator(".woa-turn").last.bounding_box()
        composer = page.locator(".woa-composer").bounding_box()
        assert turn is not None and composer is not None
        self.assertGreaterEqual(composer["y"] - (turn["y"] + turn["height"]), 85)
        self.assertEqual(self.errors, [])

    def test_the_jump_control_waits_until_the_reader_is_meaningfully_away(self):
        answer = "\n\n".join(f"第 {index} 行内容。" for index in range(80))
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "给我一份长清单"),
            _entry("e2", "result", answer, panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        thread = page.locator(".woa-thread")
        page.wait_for_timeout(300)

        exact_distance = thread.evaluate("""node => {
          node.scrollTop = node.scrollHeight - node.clientHeight - 160
          return node.scrollHeight - node.scrollTop - node.clientHeight
        }""")
        page.wait_for_timeout(100)
        self.assertEqual(exact_distance, 160)
        self.assertEqual(page.locator(".woa-jump").count(), 0)

        beyond_distance = thread.evaluate("""node => {
          node.scrollTop = node.scrollHeight - node.clientHeight - 161
          return node.scrollHeight - node.scrollTop - node.clientHeight
        }""")
        page.wait_for_timeout(100)
        self.assertEqual(beyond_distance, 161)
        page.locator(".woa-jump").wait_for(timeout=3000)
        self.assertEqual(self.errors, [])

    def test_the_keyboard_scroll_target_uses_a_quiet_focus_boundary(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出客户"),
            _entry("e2", "result", "Acme", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        thread = page.locator(".woa-thread")
        # Opening puts the caret in the composer. One reverse Tab reaches the scrollable
        # transcript and activates the keyboard-only focus treatment.
        page.keyboard.press("Shift+Tab")
        self.assertTrue(thread.evaluate("node => node === node.getRootNode().activeElement"))
        style = thread.evaluate("""node => {
          const css = getComputedStyle(node)
          return {outline: css.outlineStyle, shadow: css.boxShadow}
        }""")
        self.assertEqual(style["outline"], "none")
        self.assertNotEqual(style["shadow"], "none")
        self.assertEqual(self.errors, [])

    def test_a_crash_inside_the_panel_never_reaches_the_page(self):
        """The panel has no iframe, so its realm is the host application's realm.

        Anything it lets escape is caught by whatever the host application installed --
        Odoo answers with its own "something went wrong" dialog. Containment has to be at
        the panel's own edges; installing a global handler would be the same trespass in
        the other direction.
        """
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        page.evaluate("() => { window.__host.snapshot = () => "
                      "{ throw new Error('the panel failed') } }")
        page.evaluate("() => window.__setSnapshot({})")
        page.wait_for_timeout(400)
        self.assertEqual(self.errors, [])
        # And it says so, rather than vanishing from the page without explanation.
        fatal = page.locator(".woa-fatal")
        fatal.wait_for(timeout=4000)
        self.assertTrue(fatal.is_visible())

    def test_the_panel_installs_no_global_error_handler(self):
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        self.assertTrue(page.evaluate("() => window.__onerrorIsOurs()"))
        self.assertEqual(self.errors, [])

    def test_an_undelivered_message_carries_its_own_retry(self):
        page = self.open(_snapshot(
            optimistic={"turnId": "turn-9", "text": "再带上金额", "failed": True}))
        page.locator(".woa-launcher").click()
        undelivered = page.locator(".woa-undelivered")
        undelivered.wait_for()
        self.assertIn(panel_contract()["label_keys"]["undelivered"], undelivered.inner_text())
        undelivered.locator("button").click()
        self.assertEqual([call[0] for call in self.calls(page)], ["retrySubmit"])
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------------- states and layout
    def test_every_panel_state_in_the_contract_renders(self):
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        window = page.locator(".woa-window")
        window.wait_for()
        for state in panel_contract()["panel_states"]:
            page.evaluate(
                "state => window.__setSnapshot({connection:{state, busy:state==='acting',"
                "stopping:state==='stopping', online:true}})", state)
            self.assertEqual(window.get_attribute("data-state"), state)
            self.assertTrue(window.is_visible(), state)
        self.assertEqual(self.errors, [])

    def test_the_palette_actually_reaches_the_shadow_root(self):
        """The tokens have to be declared on `:host`, not `:root`.

        Written after watching the panel render completely unstyled while every behavioural
        test stayed green: inside a shadow root `:root` matches nothing, so the whole
        palette silently resolved to the initial value of each property. Structure and
        behaviour tests cannot see that, so this one reads computed style.
        """
        tokens = design_tokens()
        page = self.open(_snapshot(entries=[_entry("e1", "goal", "列出订单")]))
        page.locator(".woa-launcher").click()
        window = page.locator(".woa-window")
        window.wait_for()
        for name in ("accent", "ink", "bg", "line", "warning"):
            self.assertEqual(
                window.evaluate(
                    "(node, name) => getComputedStyle(node)"
                    ".getPropertyValue(`--woa-${name}`).trim()", name),
                tokens["themes"]["light"][name], name)
        self.assertEqual(window.evaluate("node => getComputedStyle(node).fontSize"),
                         tokens["scale"]["font-size"])
        # The host application's own type must not reach in.
        page.evaluate("document.documentElement.style.fontSize = '31px'")
        page.evaluate("document.body.style.letterSpacing = '4px'")
        self.assertEqual(window.evaluate("node => getComputedStyle(node).fontSize"),
                         tokens["scale"]["font-size"])
        self.assertEqual(self.errors, [])

    def test_an_explicit_dark_theme_on_the_document_reaches_the_panel(self):
        tokens = design_tokens()
        page = self.open(_snapshot())
        page.evaluate("document.documentElement.dataset.theme = 'dark'")
        page.locator(".woa-launcher").click()
        window = page.locator(".woa-window")
        window.wait_for()
        self.assertEqual(
            window.evaluate("node => getComputedStyle(node).getPropertyValue('--woa-bg').trim()"),
            tokens["themes"]["dark"]["bg"])
        page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.assertEqual(
            window.evaluate("node => getComputedStyle(node).getPropertyValue('--woa-bg').trim()"),
            tokens["themes"]["light"]["bg"])
        self.assertEqual(self.errors, [])

    def test_long_values_wrap_instead_of_widening_the_window(self):
        long_name = "Nordwind Handelsgesellschaft mbH & Co. Kommanditgesellschaft " * 4
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", long_name),
            _entry("e2", "result", "a" * 900, authored="server", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-user").wait_for()
        window = page.locator(".woa-window").bounding_box()
        assert window is not None
        for selector in (".woa-user", ".woa-md"):
            box = page.locator(selector).first.bounding_box()
            assert box is not None, selector
            self.assertLessEqual(box["x"] + box["width"], window["x"] + window["width"] + 1,
                                 selector)
        self.assertFalse(page.locator(".woa-thread").evaluate(
            "node => node.scrollWidth > node.clientWidth + 1"))
        self.assertEqual(self.errors, [])

    def test_reduced_motion_stops_every_animation(self):
        page = self.open(_snapshot(
            connection={"state": "acting", "busy": True, "stopping": False, "online": True},
            entries=[_entry("e1", "goal", "列出订单"), _entry("e2", "action", "重新检查当前页面")]),
            reduced_motion="reduce")
        page.locator(".woa-launcher").click()
        page.locator(".woa-spinner").wait_for()
        durations = page.locator(".woa-window").evaluate(
            "node => [...node.querySelectorAll('*')].map(el =>"
            " getComputedStyle(el).animationDuration).filter(Boolean)")
        self.assertTrue(durations)
        # Chromium reports the 0.001ms floor as 1e-06s.
        self.assertTrue(all(float(value.rstrip("s")) < 0.01 for value in durations),
                        durations)
        self.assertEqual(self.errors, [])

    def test_a_new_conversation_clears_the_view_and_tells_the_server(self):
        """Starting over empties what the panel shows, and says so to the server.

        Only the *view* clears. The session's event log is what happened and the panel has
        no way to delete it -- which is why this asserts the call was made rather than
        asserting anything was erased anywhere else.
        """
        labels = panel_contract()["label_keys"]
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "把报价单 S00002 确认成销售订单"),
            _entry("e2", "result", "报价单 S00002 已确认为销售订单。",
                   authored="server", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-window").wait_for()
        self.assertIn("S00002", page.locator(".woa-thread").inner_text())

        page.get_by_role("button", name=labels["newChat"]).click()
        page.locator(".woa-empty").wait_for(timeout=3000)
        # Assert on the turns, not on the thread's text: one of the suggestions the empty
        # state offers names S00002 too, so a substring check here passes for the wrong
        # reason and would keep passing if nothing had cleared at all.
        self.assertEqual(page.locator(".woa-turns").count(), 0)
        self.assertEqual(page.locator(".woa-user").count(), 0)
        self.assertEqual([call[0] for call in self.calls(page)], ["newConversation"])
        self.assertEqual(self.errors, [])

    def test_the_composer_grows_and_only_scrolls_once_it_has_stopped_growing(self):
        """A single line must not be sitting next to a scrollbar.

        The box sizes itself to its content, so anything left over is sub-pixel: at 13px
        with a line height of 1.5 the rounding is half a pixel, which is enough for a
        permanent scrollbar on the machines that do not use overlay ones.
        """
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        field = page.locator(".woa-field textarea")
        field.wait_for()

        one_line = field.evaluate(
            "node => ({overflow: getComputedStyle(node).overflowY,"
            " overflowing: node.scrollHeight > node.clientHeight + 1,"
            " ceiling: parseFloat(getComputedStyle(node).maxHeight),"
            " height: node.clientHeight})")
        self.assertEqual(one_line["overflow"], "hidden")
        self.assertFalse(one_line["overflowing"], one_line)

        field.fill("\n".join(f"第 {index} 行" for index in range(40)))
        tall = field.evaluate(
            "node => ({overflow: getComputedStyle(node).overflowY,"
            " height: node.clientHeight,"
            " ceiling: parseFloat(getComputedStyle(node).maxHeight)})")
        # Past the ceiling it scrolls, and it stops at exactly the height the stylesheet
        # declares -- the value the component reads back rather than repeating.
        self.assertEqual(tall["overflow"], "auto")
        self.assertLessEqual(tall["height"], tall["ceiling"])
        self.assertGreater(tall["height"], one_line["height"])
        self.assertEqual(tall["ceiling"], one_line["ceiling"])
        self.assertEqual(self.errors, [])

    def test_the_title_bar_controls_still_work_while_it_is_also_the_drag_handle(self):
        """A window you can drag by its title bar must still have usable buttons in it."""
        page = self.open(_snapshot(entries=[_entry("e1", "goal", "列出订单")]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-window").wait_for()
        labels = panel_contract()["label_keys"]
        page.get_by_role("button", name=labels["newChat"]).click()
        self.assertEqual([call[0] for call in self.calls(page)], ["newConversation"])
        page.get_by_role("button", name=labels["collapse"]).click()
        page.locator(".woa-launcher").wait_for(timeout=3000)
        self.assertEqual(page.locator(".woa-window").count(), 0)
        self.assertEqual(self.errors, [])

    def test_a_window_nobody_has_moved_opens_at_the_side_and_clear_of_the_floor(self):
        """An assistant sits beside the application, not across the middle of it.

        The vertical placement is the part that was wrong: pinned 16px off the floor, the
        composer was in the last sliver of the viewport and the first thing a shorter
        window clipped.
        """
        page = self.open(_snapshot(), width=1440, height=900)
        page.locator(".woa-launcher").click()
        box = page.evaluate("""() => {
          const root = document.getElementById('woa-assistant').shadowRoot
          const rect = e => { const b = e.getBoundingClientRect()
            return {x: b.x, y: b.y, w: b.width, h: b.height} }
          return {window: rect(root.querySelector('.woa-window')),
                  composer: rect(root.querySelector('textarea'))}
        }""")
        self.assertLess(1440 - (box["window"]["x"] + box["window"]["w"]), 24)
        self.assertGreater(box["window"]["y"] + box["window"]["h"] / 2, 450)
        # The one control a person has to find is well clear of the viewport floor.
        self.assertGreater(900 - (box["composer"]["y"] + box["composer"]["h"]), 60)
        self.assertEqual(self.errors, [])

    def test_a_control_calls_its_host_as_a_method_and_never_escapes_the_panel(self):
        """Two failures, one click.

        `onClick={host.newConversation}` hands React a bare reference; the real host is a
        class, so it lost its receiver and threw. With no iframe the TypeError landed in
        the host application's realm, and Odoo raised its own error dialog over a page
        nothing had gone wrong on.
        """
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出所有待开票的销售订单"),
            _entry("e2", "result", "十六张。", panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-turn").wait_for()
        page.get_by_label(panel_contract()["label_keys"]["newChat"]).click()
        self.assertEqual([call[0] for call in self.calls(page)], ["newConversation"])
        self.assertEqual(page.locator(".woa-turn").count(), 0)
        self.assertEqual(self.errors, [])

    def test_a_host_that_throws_is_the_panel_s_problem_not_the_application_s(self):
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        page.evaluate("() => { window.__host.newConversation = () => "
                      "{ throw new Error('the host failed') } }")
        page.get_by_label(panel_contract()["label_keys"]["newChat"]).click()
        notice = page.locator(".woa-inline-error")
        notice.wait_for()
        self.assertIn(panel_contract()["notices"]["unexpected"], notice.inner_text())
        # Nothing reached the page: the host application never hears about this.
        self.assertEqual(self.errors, [])

    def test_the_window_moves_and_resizes_and_stays_in_the_viewport(self):
        page = self.open(_snapshot())
        page.locator(".woa-launcher").click()
        window = page.locator(".woa-window")
        window.wait_for()
        before = window.bounding_box()
        assert before is not None
        page.mouse.move(before["x"] + before["width"] / 2, before["y"] + 20)
        page.mouse.down()
        page.mouse.move(before["x"] + before["width"] / 2 - 160, before["y"] - 60, steps=6)
        page.mouse.up()
        moved = window.bounding_box()
        assert moved is not None
        self.assertLess(moved["x"], before["x"] - 100)
        self.assertLess(moved["y"], before["y"])

        page.mouse.move(moved["x"] + 1, moved["y"] + moved["height"] / 2)
        page.mouse.down()
        page.mouse.move(moved["x"] - 120, moved["y"] + moved["height"] / 2, steps=6)
        page.mouse.up()
        widened = window.bounding_box()
        assert widened is not None
        self.assertGreater(widened["width"], moved["width"] + 60)

        # A viewport that shrinks pulls the window back in rather than leaving it outside.
        page.set_viewport_size({"width": 420, "height": 420})
        page.wait_for_timeout(120)
        clamped = window.bounding_box()
        assert clamped is not None
        self.assertLessEqual(clamped["x"] + clamped["width"], 421)
        self.assertLessEqual(clamped["y"] + clamped["height"], 421)
        self.assertGreaterEqual(clamped["width"], 320)
        self.assertEqual(self.errors, [])


def _relative_luminance(rgb: list[float]) -> float:
    def linear(channel: float) -> float:
        channel /= 255
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    """WCAG contrast of a computed `rgb()`/`rgba()` foreground over an opaque background."""
    import re

    def parse(value: str) -> tuple[list[float], float]:
        numbers = [float(item) for item in re.findall(r"[\d.]+", value)]
        return numbers[:3], numbers[3] if len(numbers) > 3 else 1.0

    back, _ = parse(background)
    fore, alpha = parse(foreground)
    blended = [alpha * f + (1 - alpha) * b for f, b in zip(fore, back)]
    lighter, darker = sorted((_relative_luminance(blended), _relative_luminance(back)),
                             reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# The colour an element's text is drawn in, and the first opaque background behind it --
# walking out of the shadow root to its host if need be.
_COLOURS_JS = """([selector, pseudo, property]) => {
  const root = document.querySelector('#woa-assistant').shadowRoot
  const node = root.querySelector(selector)
  if (!node) return null
  const parent = el => el.parentElement
    || (el.getRootNode() instanceof ShadowRoot ? el.getRootNode().host : null)
  let background = 'rgb(255, 255, 255)'
  for (let at = node; at; at = parent(at)) {
    const value = getComputedStyle(at).backgroundColor
    const parts = value.match(/[\\d.]+/g) || []
    if (parts.length >= 3 && (parts.length < 4 || parseFloat(parts[3]) > 0.99)) {
      background = value
      break
    }
  }
  return {fg: getComputedStyle(node, pseudo || null)[property || 'color'], bg: background}
}"""

# Whether a control is inside the viewport and inside the window, and whether the point a
# person would press actually lands on it rather than on something drawn over it.
_REACHABLE_JS = """selector => {
  const root = document.querySelector('#woa-assistant').shadowRoot
  const node = root.querySelector(selector)
  if (!node) return {present: false}
  const box = node.getBoundingClientRect()
  const frame = root.querySelector('.woa-window').getBoundingClientRect()
  const x = box.left + box.width / 2, y = box.top + box.height / 2
  const hit = root.elementFromPoint(x, y)
  return {
    present: true, width: box.width, height: box.height,
    in_viewport: box.left >= 0 && box.top >= 0
      && box.right <= innerWidth + 0.5 && box.bottom <= innerHeight + 0.5,
    in_window: box.left >= frame.left - 0.5 && box.top >= frame.top - 0.5
      && box.right <= frame.right + 0.5 && box.bottom <= frame.bottom + 0.5,
    unobscured: Boolean(hit) && (hit === node || node.contains(hit)),
  }
}"""

_ACTIVE_JS = """() => {
  const active = document.querySelector('#woa-assistant').shadowRoot.activeElement
  if (!active) return null
  const style = getComputedStyle(active)
  return {id: active.id, testid: active.getAttribute('data-testid') || '',
          label: active.getAttribute('aria-label') || active.textContent.trim(),
          focus_visible: active.matches(':focus-visible'),
          outline_style: style.outlineStyle, outline_width: parseFloat(style.outlineWidth),
          outline_color: style.outlineColor, box_shadow: style.boxShadow}
}"""


class PanelU07Tests(_PanelBrowser):
    """U07, the part a machine can decide.

    Layout at 320 px, layout at 200 % zoom, both colour schemes, reduced motion, and whether
    the keyboard reaches the composer and both confirmation answers in a sensible order with
    a visible focus ring. 200 % zoom is emulated the way a browser produces it -- half the
    CSS viewport at twice the device pixel ratio -- which is the layout a 1280 x 800 window
    shows when zoomed, not the browser's zoom control itself.

    None of this is a screen-reader test. The DOM roles and names checked at the end are
    what a reader needs to be handed; whether it announces them sensibly is a person's
    judgement and stays with the manual acceptance steps.
    """

    LONG_QUESTION = ("请把 Nordwind Handelsgesellschaft mbH & Co. Kommanditgesellschaft 名下所有"
                     "未确认的报价单按金额从高到低列出来，并说明每一张的客户、币种和创建日期，"
                     "如果有并列金额的也全部列出。") * 2

    VIEWPORTS = (("320px", 320, 568, 1), ("200pct-zoom", 640, 400, 2))

    def _shot(self, page, name: str) -> None:
        import os
        directory = os.environ.get("WOA_U07_SCREENSHOT_DIR")
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(directory) / f"{name}.png"))

    def _conversation(self) -> dict:
        return _snapshot(entries=[
            _entry("e1", "goal", self.LONG_QUESTION),
            _entry("e2", "result", "找到 2 张：\n\n| 单号 | 金额 |\n|---|---|\n"
                   "| S00002 | 2947.50 USD |\n| S00007 | 1180.00 USD |",
                   authored="server", panel_state="success"),
        ])

    @staticmethod
    def _settle(page) -> None:
        """Let entrance animations finish, so geometry and pixels are the resting ones."""
        page.evaluate("""() => Promise.all(document.querySelector('#woa-assistant')
          .shadowRoot.getAnimations().map(animation => animation.finished))""")

    def assertLaidOut(self, page, where: str) -> None:
        """Nothing a person needs is clipped by the window or drawn over by something else."""
        facts = page.evaluate("""() => {
          const root = document.querySelector('#woa-assistant').shadowRoot
          const rect = selector => {
            const node = root.querySelector(selector)
            return node ? node.getBoundingClientRect().toJSON() : null
          }
          const heading = root.querySelector('.woa-approval h2')
          let heading_hit = null
          if (heading) {
            const box = heading.getBoundingClientRect()
            const hit = root.elementFromPoint(box.left + 24, box.top + box.height / 2)
            heading_hit = Boolean(hit) && heading.contains(hit)
          }
          // Where the thread is actually painted: its own box, cut by the shell around it
          // only if that shell clips.
          const shell = root.querySelector('.woa-thread-shell')
          const thread = rect('.woa-thread')
          if (getComputedStyle(shell).overflowY !== 'visible') {
            thread.bottom = Math.min(thread.bottom, shell.getBoundingClientRect().bottom)
          }
          // Whether the card still has an edge beside and below its pinned answers: a real
          // border, and an answers bar that stays inside it.
          const cardNode = root.querySelector('.woa-approval')
          let edge = null
          if (cardNode) {
            const style = getComputedStyle(cardNode)
            const outer = cardNode.getBoundingClientRect()
            const bar = root.querySelector('.woa-approval__actions').getBoundingClientRect()
            const left = parseFloat(style.borderLeftWidth)
            const right = parseFloat(style.borderRightWidth)
            const bottom = parseFloat(style.borderBottomWidth)
            edge = {left, right, bottom,
                    bar_inside: bar.left >= outer.left + left - 0.5
                      && bar.right <= outer.right - right + 0.5
                      && bar.bottom <= outer.bottom - bottom + 0.5}
          }
          const scrollers = [...root.querySelectorAll('*')].filter(node => {
            const style = getComputedStyle(node)
            // A text field scrolls its own value; what this counts is content panes.
            if (node.tagName === 'TEXTAREA' || node.tagName === 'INPUT') return false
            const scrolls = /(auto|scroll)/.test(style.overflowY)
            return scrolls && node.scrollHeight - node.clientHeight > 4
          }).map(node => node.className || node.tagName)
          const threadNode = root.querySelector('.woa-thread')
          return {window: rect('.woa-window'), field: rect('.woa-field'), edge,
                  thread, card: rect('.woa-approval'), scrollers,
                  actions: rect('.woa-approval__actions'),
                  card_in_thread: Boolean(cardNode && threadNode
                                          && threadNode.contains(cardNode)),
                  heading: heading ? heading.getBoundingClientRect().toJSON() : null,
                  heading_hit}
        }""")
        frame = facts["window"]
        self.assertLessEqual(facts["field"]["bottom"], frame["bottom"] + 0.5,
                             f"the composer is clipped by the window at {where}: {facts}")
        card = facts["card"]
        if card is None:
            return
        # The confirmation lives in the conversation, so the one thing that scrolls is the
        # conversation: the card used to be stacked under it, taking up to 62% of the window
        # and leaving the thread a sliver with a scrollbar of its own (owner, P7.5).
        self.assertTrue(facts["card_in_thread"],
                        f"the confirmation is not in the conversation at {where}: {facts}")
        self.assertLessEqual(facts["thread"]["bottom"], facts["field"]["top"] + 0.5,
                             f"the conversation runs under the composer at {where}: {facts}")
        self.assertEqual(facts["scrollers"], ["woa-thread"],
                         f"the conversation is not the only thing that scrolls at {where}: {facts}")
        actions = facts["actions"]
        self.assertGreaterEqual(actions["top"], facts["thread"]["top"] - 0.5,
                                f"the answers are scrolled above the conversation at {where}: {facts}")
        self.assertLessEqual(actions["bottom"], facts["thread"]["bottom"] + 0.5,
                             f"the answers are below the conversation at {where}: {facts}")
        edge = facts["edge"]
        self.assertGreaterEqual(min(edge["left"], edge["right"], edge["bottom"]), 1,
                                f"the confirmation has no edge at {where}: {edge}")
        self.assertTrue(edge["bar_inside"],
                        f"the pinned answers cover the confirmation's edge at {where}: {edge}")

    def assertReachable(self, page, selector: str, where: str) -> None:
        facts = page.evaluate(_REACHABLE_JS, selector)
        self.assertTrue(facts["present"], f"{selector} is missing at {where}")
        for fact in ("in_viewport", "in_window", "unobscured"):
            self.assertTrue(facts[fact], f"{selector} is not {fact} at {where}: {facts}")
        self.assertGreaterEqual(min(facts["width"], facts["height"]), 24,
                                f"{selector} is too small to press at {where}: {facts}")

    def test_narrow_and_zoomed_layouts_keep_every_control_inside_and_unobscured(self):
        labels = panel_contract()["label_keys"]
        for name, width, height, scale in self.VIEWPORTS:
            for scheme in ("light", "dark"):
                where = f"{name}/{scheme}"
                with self.subTest(where):
                    page = self.open(self._conversation(), width=width, height=height,
                                     color_scheme=scheme, device_scale_factor=scale)
                    page.locator(".woa-launcher").click()
                    composer = page.locator("#woa-composer-input")
                    composer.fill(self.LONG_QUESTION)
                    self._settle(page)
                    self.assertLaidOut(page, where)
                    box = page.locator(".woa-window").bounding_box()
                    assert box is not None
                    self.assertGreaterEqual(box["x"], 0, where)
                    self.assertGreaterEqual(box["y"], 0, where)
                    self.assertLessEqual(box["x"] + box["width"], width + 1, where)
                    self.assertLessEqual(box["y"] + box["height"], height + 1, where)
                    for selector in (f'[aria-label="{labels["newChat"]}"]',
                                     f'[aria-label="{labels["collapse"]}"]',
                                     "#woa-composer-input",
                                     f'[aria-label="{labels["send"]}"]'):
                        self.assertReachable(page, selector, where)
                    self.assertFalse(page.locator(".woa-thread").evaluate(
                        "node => node.scrollWidth > node.clientWidth + 1"), where)
                    self.assertFalse(page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth + 1"), where)
                    self._shot(page, f"u07-{name}-{scheme}-composer")

                    self.raise_approval(
                        page, effect="新建客户 Nordwind Handelsgesellschaft mbH & Co. KG",
                        targets=["res.partner · 新建一条客户记录",
                                 "邮箱 · buchhaltung@nordwind.example"])
                    self._settle(page)
                    self.assertLaidOut(page, f"{where} with a confirmation")
                    for selector in ('[data-testid="woa-deny"]',
                                     '[data-testid="woa-approve"]'):
                        self.assertReachable(page, selector, f"{where} with a confirmation")
                    self.assertTrue(page.locator(".woa-approval__effect").is_visible(), where)
                    self._shot(page, f"u07-{name}-{scheme}-confirmation")
                    self.assertEqual(self.errors, [], where)

    def test_both_colour_schemes_follow_the_system_and_keep_text_legible(self):
        tokens = design_tokens()
        text = (".woa-user", ".woa-md", "#woa-composer-input",
                ".woa-approval__effect", ".woa-approval__label", ".woa-approval__fine",
                '[data-testid="woa-deny"]', '[data-testid="woa-approve"]')
        for scheme in ("light", "dark"):
            with self.subTest(scheme):
                page = self.open(self._conversation(), color_scheme=scheme)
                page.locator(".woa-launcher").click()
                window = page.locator(".woa-window")
                # Nothing on the document asked for a theme: the system preference alone
                # has to reach the shadow root.
                self.assertEqual(window.evaluate(
                    "node => getComputedStyle(node).getPropertyValue('--woa-bg').trim()"),
                    tokens["themes"][scheme]["bg"])
                placeholder = page.evaluate(_COLOURS_JS, ["#woa-composer-input",
                                                         "::placeholder", "color"])
                self.assertGreaterEqual(_contrast(placeholder["fg"], placeholder["bg"]), 4.5,
                                        f"placeholder in {scheme}: {placeholder}")
                page.locator("#woa-composer-input").fill("列出订单")
                self.raise_approval(page)
                for selector in text:
                    colours = page.evaluate(_COLOURS_JS, [selector, "", "color"])
                    self.assertIsNotNone(colours, selector)
                    self.assertGreaterEqual(_contrast(colours["fg"], colours["bg"]), 4.5,
                                            f"{selector} in {scheme}: {colours}")
                # The focus ring is a non-text indicator: 3:1 against what it sits on.
                page.keyboard.press("Tab")
                active = page.evaluate(_ACTIVE_JS)
                ring = page.evaluate(_COLOURS_JS, [".woa-approval", "", "color"])
                self.assertGreaterEqual(_contrast(active["outline_color"], ring["bg"]), 3.0,
                                        f"focus ring in {scheme}: {active}")
                self.assertEqual(self.errors, [])

    def test_reduced_motion_removes_transitions_as_well_as_animations(self):
        script = ("node => [...node.querySelectorAll('*'), node].flatMap(el =>"
                  " getComputedStyle(el).transitionDuration.split(',').map(parseFloat))")
        for preference in ("no-preference", "reduce"):
            with self.subTest(preference):
                page = self.open(self._conversation(), reduced_motion=preference)
                page.locator(".woa-launcher").click()
                self.raise_approval(page)
                durations = page.locator(".woa-window").evaluate(script)
                if preference == "reduce":
                    self.assertTrue(all(value < 0.01 for value in durations), durations)
                else:
                    # Otherwise the check above would pass on a panel with nothing to stop.
                    self.assertTrue(any(value >= 0.05 for value in durations), durations)
                self.assertEqual(self.errors, [])

    def test_the_keyboard_reaches_the_composer_and_send_in_order_with_a_visible_ring(self):
        labels = panel_contract()["label_keys"]
        page = self.open(self._conversation())
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate(_ACTIVE_JS)["label"], labels["expand"])
        page.keyboard.press("Enter")
        page.locator(".woa-window").wait_for()
        self.assertEqual(page.evaluate(_ACTIVE_JS)["id"], "woa-composer-input",
                         "opening the panel puts the caret where the person types")
        page.keyboard.type("列出订单")

        # Walk backwards to the first control in the window, then forwards through it.
        for _ in range(20):
            if page.evaluate(_ACTIVE_JS)["label"] == labels["newChat"]:
                break
            page.keyboard.press("Shift+Tab")
        order = []
        for _ in range(20):
            active = page.evaluate(_ACTIVE_JS)
            if active is None:
                break
            order.append(active["label"] if not active["id"] else active["id"])
            self.assertTrue(active["focus_visible"], active)
            if active["id"] == "woa-composer-input":
                # The composer draws its ring on the field around the text, not on the
                # textarea's own outline.
                field = page.locator(".woa-field")
                self.assertEqual(field.get_attribute("data-focus"), "true")
                self.assertNotEqual(field.evaluate(
                    "node => getComputedStyle(node).boxShadow"), "none")
            else:
                # Most controls use an outside outline. The large scrollable transcript
                # uses a quiet inset shadow so keyboard focus does not resemble an error
                # border around the whole conversation.
                self.assertTrue(
                    (active["outline_style"] != "none" and active["outline_width"] >= 2)
                    or active["box_shadow"] != "none", active)
            if active["label"] == labels["send"]:
                break
            page.keyboard.press("Tab")
        for earlier, later in ((labels["newChat"], labels["collapse"]),
                               (labels["collapse"], "woa-composer-input"),
                               ("woa-composer-input", labels["send"])):
            self.assertIn(earlier, order)
            self.assertIn(later, order)
            self.assertLess(order.index(earlier), order.index(later), order)

        page.keyboard.press("Shift+Tab")
        self.assertEqual(page.evaluate(_ACTIVE_JS)["id"], "woa-composer-input")
        page.keyboard.press("Enter")
        self.assertEqual(self.calls(page), [["submit", "列出订单"]])
        self.assertEqual(self.errors, [])

    def test_both_confirmation_answers_are_reachable_and_operable_by_keyboard(self):
        visibility = panel_contract()["approval_visibility"]
        page = self.open(_snapshot())
        self.raise_approval(page)
        self.assertEqual(page.evaluate(_ACTIVE_JS)["testid"], "woa-deny",
                         "the safe answer takes focus first")
        page.keyboard.press("Shift+Tab")
        self.assertEqual(page.evaluate(_ACTIVE_JS)["testid"], "woa-approve",
                         "Shift+Tab from the first answer wraps to the second")
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate(_ACTIVE_JS)["testid"], "woa-deny")
        page.keyboard.press("Tab")
        active = page.evaluate(_ACTIVE_JS)
        self.assertEqual(active["testid"], "woa-approve")
        self.assertTrue(active["focus_visible"])
        self.assertNotEqual(active["outline_style"], "none")
        # A keyboard press is a real gesture: once the card has been visible long enough to
        # read, Enter approves exactly as a click does.
        page.wait_for_timeout(int(visibility["min_visible_ms"]) + 250)
        page.keyboard.press("Enter")
        self.assertEqual([call[0] for call in self.calls(page)], ["decide"])
        self.assertEqual(self.calls(page)[0][2], "approved")
        self.assertEqual(self.errors, [])

    def test_no_focus_ring_is_cut_off_by_a_container_that_clips(self):
        """A ring drawn outside its control vanishes wherever an ancestor clips.

        A timeline node opens onto what was called, so it is a control a keyboard reaches;
        drawing its ring outside itself put a stray line under the node instead of a ring
        around it wherever an ancestor clipped.
        """
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出所有待开票的销售订单，带金额"),
            _entry("e2", "progress", "正在读取业务数据", tone="active",
                   step={"stage": "tool", "tool": "query_sale_orders", "arguments": "limit=50"}),
            _entry("e3", "result", "3 张销售订单待开票。", authored="server",
                   panel_state="success"),
        ]))
        page.locator(".woa-launcher").click()
        page.locator(".woa-step__line").wait_for()
        # Real Tab presses: only keyboard focus draws the ring a keyboard user sees.
        ring_js = """() => {
          const root = document.querySelector('#woa-assistant').shadowRoot
          const node = root.activeElement
          if (!node || !node.matches(':focus-visible')) return null
          const style = getComputedStyle(node)
          const inset = style.outlineStyle === 'none' && style.boxShadow !== 'none'
          if (style.outlineStyle === 'none' && !inset) return null
          const name = `${node.tagName.toLowerCase()}.${node.className || node.id}`
          const grow = inset ? 0 : parseFloat(style.outlineOffset) + parseFloat(style.outlineWidth)
          const box = node.getBoundingClientRect()
          for (let at = node.parentElement; at; at = at.parentElement) {
            const clip = getComputedStyle(at)
            if (clip.overflowX === 'visible' && clip.overflowY === 'visible') continue
            const edge = at.getBoundingClientRect()
            if (box.left - grow < edge.left - 0.5 || box.top - grow < edge.top - 0.5
                || box.right + grow > edge.right + 0.5
                || box.bottom + grow > edge.bottom + 0.5) {
              return {name, clipped_by: at.className}
            }
          }
          return {name, clipped_by: ''}
        }"""
        page.locator("#woa-composer-input").focus()
        seen: dict[str, str] = {}
        for key in ("Shift+Tab",) * 12 + ("Tab",) * 12:
            page.keyboard.press(key)
            ring = page.evaluate(ring_js)
            if ring is not None:
                seen[ring["name"]] = ring["clipped_by"]
        # Otherwise an empty result could mean nothing was ever focused.
        self.assertGreaterEqual(len(seen), 3, seen)
        self.assertTrue(any("woa-step__line" in name for name in seen), seen)
        self.assertEqual({name: by for name, by in seen.items() if by}, {}, seen)
        summary_style = page.locator(".woa-step__line").evaluate("""node => {
          node.focus()
          const style = getComputedStyle(node)
          return {outline: style.outlineStyle, shadow: style.boxShadow}
        }""")
        self.assertEqual(summary_style["outline"], "none")
        self.assertNotEqual(summary_style["shadow"], "none")
        self.assertEqual(self.errors, [])

    def test_what_a_screen_reader_is_handed_names_every_control(self):
        """Roles and names only. Whether they are announced well is checked by a person."""
        page = self.open(self._conversation())
        page.locator(".woa-launcher").click()
        self.raise_approval(page)
        facts = page.evaluate("""() => {
          const root = document.querySelector('#woa-assistant').shadowRoot
          const text = id => (root.getElementById(id) || {}).textContent || ''
          const card = root.querySelector('[role="alertdialog"]')
          const input = root.querySelector('#woa-composer-input')
          return {
            window_label: root.querySelector('.woa-window').getAttribute('aria-label'),
            log: [root.querySelector('.woa-thread').getAttribute('role'),
                  root.querySelector('.woa-thread').getAttribute('aria-live')],
            unnamed_buttons: [...root.querySelectorAll('button')].filter(button =>
              !(button.getAttribute('aria-label') || button.textContent.trim())).length,
            input_label: [...root.querySelectorAll(`label[for="${input.id}"]`)]
              .map(label => label.textContent).join(''),
            card_title: text(card.getAttribute('aria-labelledby')),
            card_description: text(card.getAttribute('aria-describedby')),
          }
        }""")
        labels = panel_contract()["label_keys"]
        self.assertEqual(facts["window_label"], labels["assistant"])
        self.assertEqual(facts["log"], ["log", "polite"])
        self.assertEqual(facts["unnamed_buttons"], 0)
        self.assertEqual(facts["input_label"], labels["input"])
        self.assertIn(labels["approvalTitle"], facts["card_title"])
        self.assertIn("S00002", facts["card_description"])
        self.assertEqual(self.errors, [])


STORYBOOK_STATIC = Path(__file__).resolve().parents[4] / "frontend" / "storybook-static"


@unittest.skipUnless((STORYBOOK_STATIC / "iframe.html").is_file(),
                     "Storybook is not built; run npm run build-storybook in frontend")
class StorybookShowsThePanelAsShipped(unittest.TestCase):
    """Storybook is where a person reviews the panel, so it has to show the shipped panel.

    The palette, the type and the shadows are all declared on `:host`. A story rendered
    straight into the document has no host, every one of them silently resolves to nothing,
    and the reviewer is looking at a serif, borderless, transparent panel no user ever sees.
    """

    def setUp(self):
        import functools
        from http.server import SimpleHTTPRequestHandler
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Quiet, directory=str(STORYBOOK_STATIC)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"
        self._playwright = sync_playwright().start()
        try:
            self.browser = self._playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            self._playwright.stop()
            if "Executable doesn't exist" in str(error):
                self.skipTest("Playwright Chromium is not installed")
            raise
        self.addCleanup(self._playwright.stop)
        self.addCleanup(self.browser.close)

    def _story(self, story: str, theme: str):
        context = self.browser.new_context(viewport={"width": 1024, "height": 720})
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.origin}/iframe.html?id={story}&viewMode=story"
                  f"&globals=theme:{theme}", wait_until="load")
        return page

    def assertShipped(self, page, selector: str, theme: str, *, edged: bool = True) -> None:
        node = page.locator(selector).first
        node.wait_for(timeout=15_000)
        facts = node.evaluate("""node => {
          const style = getComputedStyle(node)
          return {in_shadow_root: node.getRootNode() instanceof ShadowRoot,
                  bg: style.getPropertyValue('--woa-bg').trim(),
                  font: style.fontFamily, shadow: style.boxShadow}
        }""")
        self.assertTrue(facts["in_shadow_root"], f"{selector} in {theme}: {facts}")
        self.assertEqual(facts["bg"], design_tokens()["themes"][theme]["bg"],
                         f"{selector} in {theme}: {facts}")
        self.assertTrue(facts["font"].startswith("Inter"), f"{selector} in {theme}: {facts}")
        if edged:
            # The window, the card and the field draw their edges with box-shadow tokens;
            # with no palette those resolve to nothing and the edge simply disappears.
            self.assertNotEqual(facts["shadow"], "none", f"{selector} in {theme}: {facts}")

    def test_a_window_story_renders_with_the_palette_type_and_edges_in_both_themes(self):
        for theme in ("light", "dark"):
            with self.subTest(theme):
                page = self._story("panel-window--first-open", theme)
                launcher = page.locator(".woa-launcher")
                launcher.wait_for(timeout=15_000)
                launcher.click()
                self.assertShipped(page, ".woa-window", theme)
                self.assertShipped(page, ".woa-field", theme)

    def test_a_confirmation_story_renders_the_card_as_shipped(self):
        page = self._story("panel-window--awaiting-confirmation", "dark")
        self.assertShipped(page, ".woa-approval", "dark")

    def test_a_single_piece_renders_as_shipped_and_stays_pressable(self):
        page = self._story("panel-launcher--launcher-idle", "light")
        # The launcher's disc carries its own gradient; the button itself draws no edge.
        self.assertShipped(page, ".woa-launcher", "light", edged=False)
        self.assertEqual(page.locator(".woa-launcher").evaluate(
            "node => getComputedStyle(node).pointerEvents"), "auto")


class PanelCspTests(_PanelBrowser):
    """F2b, as far as it reaches without an iframe.

    The policy is a response header the host application sends, so we cannot make it be
    there. What we can do -- and what these pin -- is that the panel never needs it relaxed:
    it constructs, renders and answers a confirmation under a policy that forbids inline
    script, inline style and every Trusted Types sink.
    """

    def test_the_panel_constructs_under_strict_csp_and_trusted_types(self):
        page = self.open(_snapshot(entries=[_entry("e1", "goal", "确认 S00002"),
                                            _entry("e2", "action", "重新检查当前页面")]),
                         csp=STRICT_CSP)
        violations: list[str] = []
        page.on("console", lambda message: violations.append(message.text)
                if "Content Security Policy" in message.text else None)
        self.raise_approval(page, effect="确认报价单 S00002",
                            targets=["sale.order · S00002"])
        self.assertIn("S00002", page.locator(".woa-approval").inner_text())
        # The stylesheet arrived: `adoptedStyleSheets` is not governed by style-src, and no
        # <style> element was injected for the policy to refuse.
        self.assertEqual(
            page.evaluate("document.querySelector('#woa-assistant').shadowRoot"
                          ".adoptedStyleSheets.length"), 1)
        self.assertEqual(
            page.evaluate("document.querySelector('#woa-assistant').shadowRoot"
                          ".querySelectorAll('style').length"), 0)
        self.assertEqual(violations, [])
        self.assertEqual(self.errors, [])

    def test_the_panel_mints_no_trusted_types_policy_and_writes_no_html(self):
        page = self.open(_snapshot(entries=[
            _entry("e1", "goal", "列出订单"),
            _entry("e2", "result", "**粗体** 与 `代码`", authored="server",
                   panel_state="success"),
        ]), csp=STRICT_CSP)
        # `trusted-types 'none'` means creating a policy throws. Rendering markdown as React
        # nodes is why nothing here ever needs one.
        self.assertTrue(page.evaluate(
            "(() => { try { trustedTypes.createPolicy('probe', {createHTML: s => s});"
            " return false } catch { return true } })()"))
        page.locator(".woa-launcher").click()
        page.locator(".woa-md").wait_for()
        self.assertIn("粗体", page.locator(".woa-md strong").inner_text())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
