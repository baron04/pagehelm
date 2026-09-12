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

from web_operation_agent.panel_view import design_tokens, panel_contract
from web_operation_agent.remote import RemoteSessionChannel

PANEL_ROOT = Path(__file__).resolve().parents[1] / "web_runtime" / "panel"
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

const configuration = JSON.parse(document.getElementById('configuration').textContent)
let snapshot = JSON.parse(document.getElementById('snapshot').textContent)
const listeners = new Set()
window.__calls = []
const record = name => (...args) => { window.__calls.push([name, ...args]) }

const host = {
  configuration,
  snapshot: () => snapshot,
  subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
  submit: record('submit'),
  retrySubmit: record('retrySubmit'),
  cancel: record('cancel'),
  decide: record('decide'),
  // The real store clears the view here before it tells the server. A double that only
  // recorded the call would let the panel pass a test the product would fail -- the same
  // shape of hole as a fixture that ignores the scope it claims to enforce.
  newConversation() {
    window.__calls.push(['newConversation'])
    window.__setSnapshot({entries: [], pendingApproval: null, notice: null, optimistic: null,
                          hasOlder: false})
  },
  loadOlder: record('loadOlder'),
  dismissNotice: record('dismissNotice'),
  reload: record('reload'),
}
window.__setSnapshot = patch => {
  snapshot = {...snapshot, ...patch}
  for (const listener of listeners) listener(snapshot)
}
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
             csp: str = ""):
        harness = _Harness(snapshot, csp=csp)
        self.addCleanup(harness.close)
        context = self.browser.new_context(viewport={"width": width, "height": height},
                                           reduced_motion=reduced_motion,
                                           color_scheme=color_scheme)
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
        self.assertIn("接管", notice.inner_text())
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
