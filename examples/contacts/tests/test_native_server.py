from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from examples.contacts.app import DEMO_API_TOKEN, DEMO_SESSION_COOKIE
from examples.contacts.server import NativeContactsHost, NativeServerConfig
from web_operation_agent.identity import PrincipalRevokedError
from web_operation_agent.remote import RemoteProtocolError


def _chromium_launches() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def _request(url: str, *, body: dict | None = None, cookie: str = "",
             origin: str = "") -> tuple[int, dict, list[str]]:
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    payload = json.loads(response.read() or b"{}")
    return response.status, payload, response.headers.get_all("Set-Cookie") or []


class NativeContactsHostTests(unittest.TestCase):
    def test_bootstrap_derives_actor_from_cookie_and_reuses_one_tab_session(self):
        with tempfile.TemporaryDirectory() as directory, NativeContactsHost(
                NativeServerConfig(Path(directory), port=0, scripted=True)) as host:
            endpoint = host.origin + "/woa/bootstrap"
            cookie = f"{DEMO_SESSION_COOKIE}=session-ada-northstar"
            status, first, _ = _request(
                endpoint, body={"runtime_instance_id": "tab-instance-123"},
                cookie=cookie, origin=host.origin)
            self.assertEqual(status, 200)
            self.assertEqual(first["config"]["applicationId"], "minimal.contacts")
            self.assertEqual(host.server.session_count, 1)

            status, second, _ = _request(
                endpoint, body={"runtime_instance_id": "tab-instance-123"},
                cookie=cookie, origin=host.origin)
            self.assertEqual(status, 200)
            self.assertEqual(second, first)
            self.assertEqual(host.server.session_count, 1)

            # The browser cannot select an actor. The only accepted input is an opaque
            # per-tab instance id; identity comes from the application's HttpOnly cookie.
            status, payload, _ = _request(
                endpoint, body={"runtime_instance_id": "tab-instance-456",
                                "actor": "mary-jackson"},
                cookie=cookie, origin=host.origin)
            self.assertEqual(status, 400)
            self.assertEqual(payload, {"error": "invalid_bootstrap"})
            self.assertEqual(host.server.session_count, 1)

    def test_bootstrap_refuses_missing_session_and_cross_origin_request(self):
        with tempfile.TemporaryDirectory() as directory, NativeContactsHost(
                NativeServerConfig(Path(directory), port=0, scripted=True)) as host:
            endpoint = host.origin + "/woa/bootstrap"
            status, payload, _ = _request(
                endpoint, body={"runtime_instance_id": "tab-instance-123"},
                origin=host.origin)
            self.assertEqual((status, payload), (401, {"error": "unauthenticated"}))
            status, payload, _ = _request(
                endpoint, body={"runtime_instance_id": "tab-instance-123"},
                cookie=f"{DEMO_SESSION_COOKIE}=session-ada-northstar",
                origin="https://attacker.example")
            self.assertEqual((status, payload), (403, {"error": "origin_refused"}))
            self.assertEqual(host.server.session_count, 0)

    def test_logout_revokes_the_issued_principal_and_clears_the_page_session(self):
        with tempfile.TemporaryDirectory() as directory, NativeContactsHost(
                NativeServerConfig(Path(directory), port=0, scripted=True)) as host:
            cookie = f"{DEMO_SESSION_COOKIE}=session-ada-northstar"
            status, bootstrap, _ = _request(
                host.origin + "/woa/bootstrap",
                body={"runtime_instance_id": "tab-instance-123"}, cookie=cookie,
                origin=host.origin)
            self.assertEqual(status, 200)
            session_id = bootstrap["config"]["sessionId"]
            principal = next(iter(host._sessions.values()))[1]
            status, payload, cookies = _request(
                host.origin + "/woa/logout", body={}, cookie=cookie, origin=host.origin)
            self.assertEqual((status, payload), (204, {}))
            self.assertTrue(any("Max-Age=0" in item and "HttpOnly" in item
                                for item in cookies))
            self.assertTrue(host.revocations.status(principal)["revoked"])
            self.assertEqual(host.server.session_count, 0)
            with self.assertRaises(PrincipalRevokedError):
                host.revocations.validate(principal)
            with self.assertRaises(RemoteProtocolError) as rejected:
                host.server.registry.authenticate(
                    session_id, bootstrap["config"]["clientToken"], host.origin)
            self.assertEqual(rejected.exception.code, "unknown_session")

    def test_application_html_never_contains_delegated_credentials(self):
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with tempfile.TemporaryDirectory() as directory, NativeContactsHost(
                NativeServerConfig(Path(directory), port=0, scripted=True)) as host:
            response = opener.open(host.origin + "/demo/login/ada-lovelace", timeout=5)
            source = response.read().decode("utf-8")
            self.assertEqual(response.geturl(), host.origin + "/contacts")
            self.assertIn('/woa/loader.js', source)
            self.assertNotIn(DEMO_API_TOKEN, source)
            self.assertNotIn("session-ada-northstar", source)
            cookie = next(item for item in jar if item.name == DEMO_SESSION_COOKIE)
            self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))


class NativeContactsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for native-host qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_ordinary_browser_loads_assistant_runs_task_and_reuses_session_on_reload(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as directory, NativeContactsHost(
                NativeServerConfig(Path(directory), port=0, scripted=True)) as host:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    # No add_init_script and no server bootstrap_script: this is the same
                    # URL an ordinary signed-in user opens in an ordinary browser.
                    page.goto(host.origin + "/demo/login/ada-lovelace",
                              wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_function(
                        "window.__WOA_RUNTIME_READY__ !== undefined", timeout=30_000)
                    page.evaluate(
                        "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                    page.locator("#woa-assistant").wait_for(state="visible", timeout=30_000)
                    self.assertEqual(host.server.session_count, 1)
                    self.assertNotIn(DEMO_API_TOKEN, page.locator("body").inner_text())

                    page.evaluate(
                        "task => window.__WOA_RUNTIME_READY__.then(r => r.submitTask(task))",
                        "Open Grace Hopper's contact record.")
                    page.wait_for_url("**/contacts/grace-hopper", timeout=30_000)
                    page.get_by_text("Grace Hopper", exact=True).wait_for(timeout=10_000)
                    self.assertEqual(host.application.write_requests, 0)

                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                    page.evaluate(
                        "async () => { await window.__WOA_RUNTIME_READY__; return true; }")
                    page.locator("#woa-assistant").wait_for(state="visible", timeout=30_000)
                    self.assertEqual(host.server.session_count, 1)
                    self.assertEqual(
                        len(list((Path(directory) / "runs").glob("*/audit.jsonl"))), 1)
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
