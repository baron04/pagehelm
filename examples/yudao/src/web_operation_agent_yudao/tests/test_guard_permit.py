"""An approved click through the proxy: only what the card shows is sent, each effect once."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from web_operation_agent.execution.submit_contract import FormData, SubmitPermit, build_card
from web_operation_agent_yudao.guard_proxy import GUARD_PATH, NativeGuardProxy
from web_operation_agent_yudao.page_guard import GuardRefused, NativePageGuard

ACTOR = "a" * 64
PREFIX = "/admin-api/crm/contact/"
CATALOG = frozenset({("PUT", PREFIX + "update"), ("POST", PREFIX + "create-line")})
LOADED = {"id": 7, "name": "张三", "mobile": "1"}


def card(mobile="2"):
    form = lambda m: FormData.from_json({"model": m, "mode": "update", "record_id": "7"})
    return build_card(loaded=form(LOADED), form=form({**LOADED, "mobile": mobile}), page="/crm/contact",
                      button="保存", mode="update", record_id="7", target_name="张三")


class PermitProxyTests(unittest.TestCase):
    def setUp(self):
        self.record, self.calls, self.window_calls = dict(LOADED), [], []
        owner = self

        class Application(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def handle_api(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if "/woa/page-guard/" in self.path:
                    step = self.path.rsplit("/", 1)[1]
                    owner.window_calls.append(step)
                    payload = {"code": 0, "data": {"active": True, "guard": "read_only_statements"}
                               if step == "begin" else {"active": False}}
                else:
                    owner.calls.append((self.command, self.path))
                    if self.command == "PUT":
                        owner.record.update(json.loads(body))
                    payload = {"code": 0, "data": owner.record}
                out = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            do_GET = do_POST = do_PUT = handle_api

        self.app = ThreadingHTTPServer(("127.0.0.1", 0), Application)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.guard = NativePageGuard(frozenset(), writing_gets=frozenset())
        self.origin = "http://fixture.test"
        self.proxy = NativeGuardProxy(
            upstream=f"http://127.0.0.1:{self.app.server_port}", origin=self.origin, guard=self.guard,
            resolve_actor=lambda token, tenant: ACTOR, application_window=True).start()
        self.cookie = ""

    def tearDown(self):
        self.proxy.close()
        self.app.shutdown()
        self.app.server_close()

    def call(self, method, path, body=None, *, assistant=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.http.server_port, timeout=3)
        headers = {"tenant-id": "1", "Origin": self.origin, "Authorization": "Bearer t",
                   "Content-Type": "application/json"}
        if assistant:
            headers["X-WOA-Assistant"] = "1"
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, body=json.dumps(body if body is not None else {}), headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        cookie = response.getheader("Set-Cookie")
        connection.close()
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        return response.status, payload

    def arm(self, mobile="2"):
        permit = SubmitPermit(ACTOR, card(mobile), (PREFIX,), CATALOG, dict(LOADED), lambda: dict(self.record))
        self.guard.arm_permit(ACTOR, permit)
        return permit

    def test_one_click_releases_the_card_once_and_nothing_else(self):
        permit = self.arm()
        status, payload = self.call("POST", GUARD_PATH + "/begin")
        self.assertEqual((status, payload["guard"]), (200, "approved_submit"))
        self.assertEqual(self.window_calls, ["end"])        # the application's own window is off
        update = {**LOADED, "mobile": "2"}
        self.assertEqual(self.call("PUT", PREFIX + "update", update)[0], 200)
        self.assertEqual(self.call("PUT", PREFIX + "update", update)[0], 403)          # clicked twice
        self.assertEqual(self.call("POST", PREFIX + "create-line", {"x": 1})[0], 403)  # not on the card
        self.assertEqual(self.call("POST", "/admin-api/crm/customer/create", {"name": "x"})[0], 403)
        self.assertEqual(self.record["mobile"], "2")
        self.assertEqual([c for c in self.calls if c[0] == "PUT"], [("PUT", PREFIX + "update")])
        self.assertEqual(permit.duplicates, 1)
        self.assertEqual(len(permit.allowed()), 1)
        self.assertEqual(permit.allowed()[0].status, 200)
        self.assertEqual(self.call("POST", GUARD_PATH + "/end")[0], 200)
        self.assertFalse(self.guard.permit_active(ACTOR))
        self.assertIs(self.guard.finished_permit(ACTOR), permit)

    def test_the_persons_own_requests_are_not_held_during_a_permit(self):
        self.arm()
        self.call("POST", GUARD_PATH + "/begin")
        status, payload = self.call("PUT", PREFIX + "update", {**LOADED, "name": "Mine"}, assistant=False)
        self.assertEqual(status, 200)
        self.assertEqual(self.record["name"], "Mine")

    def test_a_record_changed_after_the_card_stops_the_write(self):
        self.arm()
        self.call("POST", GUARD_PATH + "/begin")
        self.record["name"] = "Someone else"
        self.assertEqual(self.call("PUT", PREFIX + "update", {**LOADED, "mobile": "2"})[0], 403)
        self.assertEqual(self.record["name"], "Someone else")
        self.assertEqual(self.record["mobile"], "1")

    def test_another_open_window_blocks_the_permit(self):
        other = self.guard.begin(ACTOR, "e" * 64)
        self.assertGreater(other, 0)
        self.arm()
        status, payload = self.call("POST", GUARD_PATH + "/begin")
        self.assertEqual((status, payload["error"]), (403, "other_window_open"))

    def test_no_permit_means_a_normal_read_only_window(self):
        status, payload = self.call("POST", GUARD_PATH + "/begin")
        self.assertEqual(payload["guard"], "read_only_statements")
        self.assertEqual(self.call("PUT", PREFIX + "update", {**LOADED, "mobile": "2"})[0], 403)

    def test_only_one_permit_at_a_time_and_disarm_releases_it(self):
        self.arm()
        with self.assertRaises(GuardRefused):
            self.arm()
        self.assertIsNotNone(self.guard.disarm_permit(ACTOR))
        self.arm()

    def test_a_read_the_person_made_before_asking_is_known_too_and_not_held(self):
        mark = self.guard.read_mark(ACTOR)
        status, _ = self.call("GET", PREFIX + "page?pageNo=1", assistant=False)
        self.assertEqual(status, 200)
        self.assertEqual(self.guard.reads_since(ACTOR, mark), ((PREFIX + "page", "pageNo=1"),))
        # the person's own write is not recorded as a read and never held
        self.assertEqual(self.call("PUT", PREFIX + "update", {**LOADED, "name": "Mine"}, assistant=False)[0], 200)
        self.assertEqual(len(self.guard.reads_since(ACTOR, mark)), 1)

    def test_reads_the_page_made_are_recorded_for_the_executor(self):
        mark = self.guard.read_mark(ACTOR)
        self.call("GET", PREFIX + "get?id=7")
        self.assertEqual(self.guard.reads_since(ACTOR, mark), ((PREFIX + "get", "id=7"),))


if __name__ == "__main__":
    unittest.main()
