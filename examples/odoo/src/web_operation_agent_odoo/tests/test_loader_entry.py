"""The assistant entry exists even when the assistant cannot start (P7.5 CR-43).

The user reported the same symptom several times across P7.4 and P7.5 -- "AI 助手入口看不到
了" -- with a different cause each time: a replayed approval decision that closed the session
before the panel mounted, and a bootstrap the server refused because the agent had come back
without its write configuration. Both were fixed; the symptom returned because the loader
rendered nothing at all unless startup fully succeeded. This drives the addon's loader in a
real browser with a refused bootstrap and requires an entry, a reason and a way to retry.
"""
from __future__ import annotations

import http.server
import json
import threading
import unittest
from pathlib import Path

LOADER = (Path(__file__).resolve().parents[3] / "deploy" / "addons" / "woa_runtime"
          / "static" / "src" / "loader.js")

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>host</title></head>
<body><main>host application</main>
<script>
  window.__woaCalls = []
  window.__woaBootstrapStatus = %s
  window.fetch = async (url, options) => {
    window.__woaCalls.push(String(url))
    if (String(url).endsWith('/woa/session-assertion')) {
      return new Response(JSON.stringify({event: 'bootstrap'}), {status: 200})
    }
    const status = window.__woaBootstrapStatus
    if (status === 200) {
      return new Response(JSON.stringify(
        {runtime_module: '/runtime.js', config: {}, renew_at: Date.now() + 600000}),
        {status: 200})
    }
    return new Response('{}', {status})
  }
</script>
<script type="module" src="/loader.js"></script>
</body></html>
"""

RUNTIME = """export async function startEmbeddedRuntime() {
  const host = document.createElement('div')
  host.id = 'woa-assistant'
  document.body.appendChild(host)
  return {stopped: false, canRenewSession: () => true, renewSession() {}}
}
"""


class _Server:
    def __init__(self, status: int):
        body = (PAGE % status).encode()
        loader = LOADER.read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path.startswith("/loader.js"):
                    payload, kind = loader, "text/javascript"
                elif self.path.startswith("/runtime.js"):
                    payload, kind = RUNTIME.encode(), "text/javascript"
                else:
                    payload, kind = body, "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@unittest.skipUnless(LOADER.is_file(), "the addon loader is not present")
class TheEntryIsThereEvenWhenTheAssistantIsNot(unittest.TestCase):
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

    def _open(self, status: int):
        server = _Server(status)
        self.addCleanup(server.close)
        page = self.browser.new_page()
        self.addCleanup(page.close)
        self.errors: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(server.origin + "/", wait_until="load")
        return page

    def test_a_refused_bootstrap_leaves_an_entry_that_says_why_and_offers_a_retry(self):
        page = self._open(403)
        entry = page.locator("#woa-assistant-unavailable")
        entry.wait_for(state="attached", timeout=8000)
        badge = entry.locator(".badge")
        self.assertEqual(badge.get_attribute("aria-label"), "助手暂时不可用")
        badge.click()
        note = entry.locator(".note")
        # The reason is the server's own status, not a shrug: the person can tell a refusal
        # from an outage, and so can whoever they show it to.
        self.assertIn("403", note.inner_text())
        self.assertTrue(entry.locator(".note button").is_visible())
        self.assertEqual(self.errors, [])

    def test_a_retry_that_succeeds_replaces_the_entry_with_the_assistant(self):
        page = self._open(503)
        page.locator("#woa-assistant-unavailable").wait_for(state="attached", timeout=8000)
        page.evaluate("() => { window.__woaBootstrapStatus = 200 }")
        page.locator("#woa-assistant-unavailable .badge").click()
        page.locator("#woa-assistant-unavailable .note button").click()
        page.locator("#woa-assistant").wait_for(state="attached", timeout=8000)
        self.assertEqual(page.locator("#woa-assistant-unavailable").count(), 0)
        self.assertEqual(self.errors, [])

    def test_a_startup_that_works_adds_no_entry_of_its_own(self):
        page = self._open(200)
        page.locator("#woa-assistant").wait_for(state="attached", timeout=8000)
        self.assertEqual(page.locator("#woa-assistant-unavailable").count(), 0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
