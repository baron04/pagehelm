"""Exercise HTTP admission against an isolated SQLite-backed fake application."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from web_operation_agent_yudao.guard_proxy import GUARD_PATH, NativeGuardProxy
from web_operation_agent_yudao.page_guard import GuardRefused, NativePageGuard


READ = "/admin-api/crm/customer/get"


class GuardProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "synthetic.sqlite3"
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO records VALUES (1, 'Synthetic customer')")
        self.calls = []
        self.window = {"code": 0, "data": {"active": True, "guard": "read_only_statements"}}
        self.entered, self.release = threading.Event(), threading.Event()
        owner = self
        class Application(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def handle_api(self):
                owner.calls.append((self.command, self.path, self.headers.get("Cookie")))
                if "/woa/page-guard/" in self.path:
                    # The application's own window, as its server answers it.
                    payload = json.dumps(owner.window).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers(); self.wfile.write(payload)
                    return
                if self.path.endswith("/slow-update"):
                    owner.entered.set()
                    owner.release.wait(3)
                with sqlite3.connect(owner.db) as connection:
                    if self.command != "GET" or self.path.endswith("/unsafe-get"):
                        connection.execute("UPDATE records SET name = 'Changed'")
                    name = connection.execute("SELECT name FROM records WHERE id=1").fetchone()[0]
                payload = json.dumps(getattr(owner, "app_body", None) or {"name": name}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            do_GET = do_POST = handle_api
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), Application)
        self.worker = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.worker.start()
        self.origin = "http://fixture.test"
        self.guard = NativePageGuard(frozenset({READ}))
        def identity(token, tenant):
            if tenant != "1" or token not in {"synthetic-a", "synthetic-b"}:
                raise GuardRefused("identity_required")
            return ("a" if token.endswith("a") else "b") * 64
        self.proxy = NativeGuardProxy(upstream=f"http://127.0.0.1:{self.app.server_port}",
            origin=self.origin, guard=self.guard, resolve_actor=identity).start()
        self.cookie = ""

    def tearDown(self):
        self.release.set()
        self.proxy.close(); self.app.shutdown(); self.app.server_close(); self.worker.join(3)
        self.tmp.cleanup()

    def request(self, method, path, *, token="synthetic-a", cookie=None, origin=None, body=b"{}", visit_tenant="",
                assistant=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.http.server_port, timeout=3)
        headers = {"tenant-id": "1", "Origin": origin or self.origin,
                   "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if visit_tenant:
            headers["visit-tenant-id"] = visit_tenant
        if assistant:
            # What the page the assistant is working puts on its own API requests.
            headers["X-WOA-Assistant"] = "1"
        selected = self.cookie if cookie is None else cookie
        if selected:
            headers["Cookie"] = selected
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        new_cookie = response.getheader("Set-Cookie")
        status = response.status
        connection.close()
        return status, payload, new_cookie

    def begin(self):
        status, payload, cookie = self.request("POST", GUARD_PATH + "/begin")
        self.assertEqual(status, 200); self.assertTrue(payload["active"])
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie)
        self.cookie = cookie.split(";", 1)[0]

    def test_guarded_fill_cannot_persist_even_if_the_page_requests_a_write(self):
        self.begin()
        for method, path in (("POST", "/admin-api/crm/customer/update"),
                             ("GET", "/admin-api/crm/customer/unsafe-get")):
            status, body, _ = self.request(method, path)
            self.assertEqual(status, 403)
            self.assertEqual(body["error"], "page_write_blocked")
        status, body, _ = self.request("GET", READ + "?id=1")
        self.assertEqual(status, 200); self.assertEqual(body["name"], "Synthetic customer")
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0][2]) # No window cookie is forwarded upstream.
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT name FROM records").fetchone()[0], "Synthetic customer")
        status, body, _ = self.request("POST", GUARD_PATH + "/end")
        self.assertEqual(status, 200); self.assertFalse(body["active"])
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update")[0], 200)

    def test_the_persons_own_requests_are_never_held_by_the_assistants_window(self):
        self.begin()
        # Another tab, another device, or the person's own click: no mark, no fence.
        status, body, _ = self.request("POST", "/admin-api/crm/customer/update", assistant=False)
        self.assertEqual((status, body["name"]), (200, "Changed"))
        # The page the assistant is working is still held.
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update")[0], 403)

    def test_an_accepted_person_write_is_observed_for_the_waiting_turn(self):
        self.begin()
        actor = "a" * 64
        self.assertEqual(self.guard.user_write_mark(actor), 0)
        self.app_body = {"code": 0, "data": 7}
        status, _body, _ = self.request("POST", "/admin-api/crm/customer/update",
                                        body=b'{"id": 7, "name": "x"}', assistant=False)
        self.assertEqual(status, 200)
        event = self.guard.user_write_after(actor, 0, ("/admin-api/crm/customer/",), "7")
        self.assertEqual((event["method"], event["record_id"]), ("POST", "7"))
        # a different record, an unaccepted answer, and anything before the mark do not wake it
        self.assertIsNone(self.guard.user_write_after(actor, 0, ("/admin-api/crm/customer/",), "8"))
        mark = self.guard.user_write_mark(actor)
        self.app_body = {"code": 500, "msg": "rejected"}
        self.request("POST", "/admin-api/crm/customer/update", body=b'{"id": 7}', assistant=False)
        self.assertIsNone(self.guard.user_write_after(actor, mark, ("/admin-api/crm/customer/",), "7"))
        self.app_body = {"code": 0, "data": {"id": 9}}
        self.request("POST", "/admin-api/crm/customer/create", body=b'{"name": "y"}', assistant=False)
        event = self.guard.user_write_after(actor, mark, ("/admin-api/crm/customer/",), "")
        self.assertEqual(event["record_id"], "9")

    def test_a_create_handoff_ignores_an_unrelated_update_under_the_prefix(self):
        self.begin()
        actor = "a" * 64
        self.app_body = {"code": 0, "data": None}
        self.request("POST", "/admin-api/crm/customer/update", body=b'{"id": 7}', assistant=False)
        mark = self.guard.user_write_mark(actor)
        prefixes = ("/admin-api/crm/customer/",)
        # An update of some other record under the same prefix is not the create.
        self.request("POST", "/admin-api/crm/customer/update", body=b'{"id": 4}', assistant=False)
        self.assertIsNone(self.guard.user_write_after(actor, mark, prefixes, "", create_only=True))
        # A lookalike that is not the create interface does not count either.
        self.request("POST", "/admin-api/crm/customer/update-status", body=b'{"id": 4}', assistant=False)
        self.assertIsNone(self.guard.user_write_after(actor, mark, prefixes, "", create_only=True))
        self.app_body = {"code": 0, "data": {"id": 11}}
        self.request("POST", "/admin-api/crm/customer/create", body=b'{"name": "z"}', assistant=False)
        event = self.guard.user_write_after(actor, mark, prefixes, "", create_only=True)
        self.assertEqual(event["record_id"], "11")

    def windowed(self):
        self.proxy.close()
        self.proxy = NativeGuardProxy(upstream=f"http://127.0.0.1:{self.app.server_port}",
            origin=self.origin, guard=self.guard, resolve_actor=self.proxy.resolve_actor,
            application_window=True).start()

    def test_a_window_opens_with_the_applications_own_and_closes_it(self):
        self.windowed()
        status, payload, cookie = self.request("POST", GUARD_PATH + "/begin")
        self.assertEqual((status, payload["guard"]), (200, "read_only_statements"))
        self.cookie = cookie.split(";", 1)[0]
        self.assertEqual(self.calls[-1][:2], ("POST", "/admin-api/woa/page-guard/begin"))
        self.assertIsNone(self.calls[-1][2])
        self.request("POST", GUARD_PATH + "/end")
        self.assertEqual(self.calls[-1][:2], ("POST", "/admin-api/woa/page-guard/end"))

    def test_a_window_the_application_does_not_confirm_is_not_opened(self):
        self.windowed()
        for answer in ({"code": 0, "data": {"active": True, "guard": "something-else"}},
                       {"code": 0, "data": {"active": False, "guard": "read_only_statements"}},
                       {"code": 404, "msg": "not found"}):
            self.window = answer
            with self.subTest(answer=answer):
                status, body, _ = self.request("POST", GUARD_PATH + "/begin")
                self.assertEqual((status, body["error"]), (403, "application_window_unavailable"))
                self.assertFalse(self.guard.protected("a" * 64))

    def test_begin_acknowledges_only_after_the_prior_mutation_response(self):
        results = {}
        prior = threading.Thread(target=lambda: results.update(prior=self.request(
            "POST", "/admin-api/crm/customer/slow-update")))
        prior.start(); self.assertTrue(self.entered.wait(2))
        begin = threading.Thread(target=lambda: results.update(begin=self.request("POST", GUARD_PATH + "/begin")))
        begin.start()
        with self.guard._condition:
            self.assertTrue(self.guard._condition.wait_for(
                lambda: self.guard._actors["a" * 64].opening > 0, timeout=2))
        self.assertNotIn("begin", results)
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update")[0], 403)
        self.release.set(); prior.join(3); begin.join(3)
        self.assertFalse(prior.is_alive()); self.assertFalse(begin.is_alive())
        self.assertEqual(results["prior"][0], 200); self.assertTrue(results["begin"][1]["active"])

    def test_bad_identity_origin_or_cookie_cannot_open_or_release_a_window(self):
        for kwargs in ({"token": ""}, {"token": "unknown"}, {"origin": "http://other.test"},
                       {"cookie": self.proxy.cookie_name + "=bad"}):
            self.assertIn(self.request("POST", GUARD_PATH + "/begin", **kwargs)[0], {401, 403})
        self.begin()
        self.assertEqual(self.request("POST", GUARD_PATH + "/end", cookie="")[0], 403)
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update")[0], 403)

    def test_ending_one_browser_does_not_release_another_browser_window(self):
        self.begin()
        _, _, other_cookie = self.request("POST", GUARD_PATH + "/begin", cookie="")
        _, body, _ = self.request("POST", GUARD_PATH + "/end")
        self.assertTrue(body["active"])
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update")[0], 403)
        _, body, _ = self.request("POST", GUARD_PATH + "/end", cookie=other_cookie.split(";", 1)[0])
        self.assertFalse(body["active"])

    def test_another_actor_is_not_given_the_first_actors_window(self):
        self.begin()
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update", token="synthetic-b")[0], 200)
        self.assertEqual(self.request("POST", "/admin-api/crm/customer/update")[0], 403)

    def test_ingress_probe_checks_both_routes_and_never_reaches_the_upstream(self):
        self.proxy.origin = self.proxy.url
        self.proxy.verify_ingress()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request("GET", "/admin-api/woa-native-guard/probe")[0], 403)
        self.proxy.origin = f"http://127.0.0.1:{self.app.server_port}"
        with self.assertRaises(GuardRefused) as failed:
            self.proxy.verify_ingress()
        self.assertEqual(failed.exception.code, "guard_ingress_unverified")

    def test_anonymous_login_support_does_not_allow_anonymous_business_requests(self):
        for method, path in (("POST", "/admin-api/system/captcha/get"),
                             ("POST", "/admin-api/system/captcha/check"),
                             ("GET", "/admin-api/system/tenant/get-by-website?website=fixture.test")):
            self.assertEqual(self.request(method, path, token="")[0], 200)
        count = len(self.calls)
        for method, path in (("GET", READ), ("POST", "/admin-api/crm/customer/update")):
            status, body, _ = self.request(method, path, token="")
            self.assertEqual((status, body["code"], body["data"]), (200, 401, None))
        self.assertEqual(len(self.calls), count)

    def test_internal_ingress_probe_preserves_public_origin_enforcement(self):
        self.proxy.ingress_base = self.proxy.url
        self.proxy.origin = "http://localhost:40002"
        self.proxy.verify_ingress()
        self.assertEqual(self.calls, [])
        status, body, _ = self.request("POST", GUARD_PATH + "/begin", origin="http://localhost:40001")
        self.assertEqual((status, body["error"]), (403, "origin_refused"))
        status, body, _ = self.request("POST", GUARD_PATH + "/begin", origin="http://localhost:40002")
        self.assertEqual((status, body["active"]), (200, True))
        self.proxy.ingress_base = f"http://127.0.0.1:{self.app.server_port}"
        with self.assertRaises(GuardRefused) as failed:
            self.proxy.verify_ingress()
        self.assertEqual(failed.exception.code, "guard_ingress_unverified")

    def test_internal_ingress_base_rejects_non_origin_urls(self):
        for address in ("file:///tmp/probe", "http://user:secret@localhost", "http://localhost/path", "http://localhost?query=1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                NativeGuardProxy(upstream="http://127.0.0.1:1", origin=self.origin,
                    ingress_base=address, guard=self.guard, resolve_actor=lambda *_: None)

    def test_same_browser_can_end_independent_windows_on_two_origins(self):
        other = NativeGuardProxy(upstream=self.proxy.upstream.geturl(), origin="http://fixture.test:40002",
            guard=NativePageGuard(frozenset({READ})), resolve_actor=self.proxy.resolve_actor).start()
        self.addCleanup(other.close)
        self.assertNotEqual(self.proxy.cookie_name, other.cookie_name)
        first = self.request("POST", GUARD_PATH + "/begin", cookie="")[2].split(";", 1)[0]
        def request(proxy, operation, cookie):
            connection = http.client.HTTPConnection("127.0.0.1", proxy.http.server_port, timeout=3)
            connection.request("POST", GUARD_PATH + "/" + operation, body=b"{}", headers={
                "Origin": proxy.origin, "Authorization": "Bearer synthetic-a", "tenant-id": "1",
                "Content-Type": "application/json", "Cookie": cookie})
            response = connection.getresponse()
            status, body, received = response.status, json.loads(response.read()), response.getheader("Set-Cookie")
            connection.close()
            self.assertEqual(status, 200)
            return body, received
        second = request(other, "begin", "")[1].split(";", 1)[0]
        cookies = first + "; " + second
        for proxy in (self.proxy, other):
            self.assertFalse(request(proxy, "end", cookies)[0]["active"])

    def test_expired_browser_token_uses_native_login_envelope_without_dispatch(self):
        count = len(self.calls)
        for method, path in (("GET", READ), ("POST", "/admin-api/crm/customer/update")):
            status, body, _ = self.request(method, path, token="expired", assistant=False)
            self.assertEqual((status, body), (200, {"code": 401, "msg": "账号未登录", "data": None}))
        self.assertEqual(len(self.calls), count)

    def test_unsupported_visit_tenant_context_is_not_silently_read_as_the_primary_tenant(self):
        status, body, _ = self.request("GET", READ, visit_tenant="2")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "native_visit_tenant_unsupported")
        self.assertFalse(self.calls)
        self.assertEqual(self.request("GET", READ, visit_tenant="1")[0], 200)


if __name__ == "__main__":
    unittest.main()
