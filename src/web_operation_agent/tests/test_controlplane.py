"""The authorisation plane: what it accepts, and what it must never reveal."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import tempfile

from web_operation_agent.controlplane import (ControlGatewayConfig, ControlPlaneClient,
                                              ControlPlaneError, plan_from_spec)

OPS = [{"webapp": "accounting", "request": "createSomething",
        "targets": [{"thingId": "42", "otherId": "demo1"}], "changes": {"amount": "20"}}]


class PlanConstruction(unittest.TestCase):
    def test_a_plan_is_built_from_the_operators_entries(self):
        plan = plan_from_spec("p1", "T", OPS)
        self.assertEqual(len(plan.tickets), 1)
        self.assertEqual(plan.tickets[0].request, "createSomething")

    def test_uri_is_accepted_as_a_spelling_of_request(self):
        plan = plan_from_spec("p1", "T", [{"webapp": "w", "uri": "r", "targets": [{"a": "1"}]}])
        self.assertEqual(plan.tickets[0].request, "r")

    def test_an_entry_without_a_target_is_refused(self):
        for bad in ({"webapp": "w", "request": "r"},
                    {"webapp": "w", "request": "r", "targets": []},
                    {"webapp": "w", "targets": [{"a": "1"}]}):
            with self.assertRaises(ControlPlaneError):
                plan_from_spec("p1", "T", [bad])

    def test_the_same_authorisation_always_has_the_same_digest(self):
        """Equivalent authorisation inputs must produce an identical plan."""
        a = plan_from_spec("p1", "T", OPS)
        b = plan_from_spec("p1", "T", json.loads(json.dumps(OPS)))
        self.assertEqual(a.content_digest, b.content_digest)

    def test_a_different_authorisation_has_a_different_digest(self):
        other = [dict(OPS[0], changes={"amount": "21"})]
        self.assertNotEqual(plan_from_spec("p1", "T", OPS).content_digest,
                            plan_from_spec("p1", "T", other).content_digest)


class _FakeGateway(BaseHTTPRequestHandler):
    calls: list = []
    refuse_index = None

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        _FakeGateway.calls.append(
            (self.path, payload, self.headers.get("x-woa-fixture-key")))
        if self.path == "/admin/reset":
            return self._send(200, {"ok": True})
        if self.path == "/admin/declare":
            idx = len([c for c in _FakeGateway.calls if c[0] == "/admin/declare"]) - 1
            if _FakeGateway.refuse_index == idx:
                return self._send(200, {"ok": False, "error": "refused"})
            return self._send(200, {"ok": True, "operation_id": payload["operation_id"],
                                    "permits": len(payload["targets"]), "service": "svc"})
        self._send(404, {"error": "nope"})

    def do_GET(self):
        if self.path == "/admin/trace":
            return self._send(200, {"trace": [
                {"event": "permit_consumed", "webapp": "accounting", "uri": "createSomething"},
                {"event": "write_blocked", "webapp": "accounting", "uri": "somethingElse"},
                {"event": "allowed", "webapp": "accounting", "uri": "view"}]})
        self._send(404, {"error": "nope"})

    def log_message(self, *a):
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Installation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), _FakeGateway)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key = Path(cls.tmp.name) / "key"
        cls.key.write_text("s3cret-key\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def _client(self):
        _FakeGateway.calls = []
        _FakeGateway.refuse_index = None
        return ControlPlaneClient(ControlGatewayConfig(
            base_url=f"http://127.0.0.1:{self.srv.server_address[1]}",
            key_file=self.key.resolve(),
            key_header="X-WOA-Fixture-Key",
        ))

    def test_a_run_starts_by_clearing_the_previous_one(self):
        c = self._client()
        c.begin_run("r1")
        self.assertEqual(_FakeGateway.calls[0][0], "/admin/reset")
        self.assertEqual(_FakeGateway.calls[0][1]["run_id"], "r1")

    def test_the_key_is_sent_and_comes_from_the_file(self):
        c = self._client()
        c.begin_run("r1")
        self.assertEqual(_FakeGateway.calls[0][2], "s3cret-key")

    def test_every_ticket_is_installed(self):
        c = self._client()
        installed = c.install(plan_from_spec("p", "T", OPS * 2))
        self.assertEqual(len(installed), 2)
        declares = [c for c in _FakeGateway.calls if c[0] == "/admin/declare"]
        self.assertEqual(len(declares), 2)
        self.assertEqual(declares[0][1]["uri"], "createSomething")

    def test_a_refused_ticket_aborts_the_whole_plan(self):
        c = self._client()
        _FakeGateway.refuse_index = 1
        with self.assertRaises(ControlPlaneError):
            c.install(plan_from_spec("p", "T", OPS * 3))

    def test_a_missing_key_file_is_an_error_not_a_silent_open(self):
        with self.assertRaises(ControlPlaneError):
            ControlPlaneClient(ControlGatewayConfig(
                base_url="http://127.0.0.1:1",
                key_file=Path("/no/such/key"),
                key_header="X-WOA-Fixture-Key",
            ))

    def test_an_unreachable_gateway_is_an_error_not_a_pass(self):
        c = ControlPlaneClient(ControlGatewayConfig(
            base_url=f"http://127.0.0.1:{_free_port()}",
            key_file=self.key.resolve(),
            key_header="X-WOA-Fixture-Key",
        ))
        with self.assertRaises(ControlPlaneError):
            c.begin_run("r1")

    def test_the_outcome_is_read_from_the_enforcement_point_not_from_the_agent(self):
        c = self._client()
        out = c.outcome()
        self.assertEqual(out["permit_consumed"], 1)
        self.assertEqual(out["write_blocked"], 1)
        self.assertEqual(out["consumed_requests"], ["accounting/createSomething"])


class ExplicitGatewayConfiguration(unittest.TestCase):
    def test_client_has_no_legacy_defaults(self):
        with self.assertRaises(TypeError):
            ControlPlaneClient()  # type: ignore[call-arg]

    def test_plain_http_is_limited_to_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            ControlGatewayConfig(
                base_url="http://gateway.example.test",
                key_file=Path("/deployment/control.key"),
                key_header="X-WOA-Control-Key",
            )

    def test_key_header_must_be_an_explicit_safe_header_name(self):
        for header in ("", "Host", "Content-Type", "bad header", "x_bad"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                ControlGatewayConfig(
                    base_url="https://gateway.example.test",
                    key_file=Path("/deployment/control.key"),
                    key_header=header,
                )

    def test_non_finite_timeout_is_refused(self):
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ControlGatewayConfig(
                    base_url="https://gateway.example.test",
                    key_file=Path("/deployment/control.key"),
                    key_header="X-WOA-Control-Key",
                    timeout_s=timeout,
                )


if __name__ == "__main__":
    unittest.main()


class ScopeIsABoundaryNotAnAnswer(unittest.TestCase):
    PLAN = plan_from_spec("p", "T", [{"webapp": "accounting", "request": "createWidgetLink",
                                      "targets": [{"widgetId": "W-77", "linkId": "L-9"}]}])
    PROMPT = "Link widget W-77 to the open link record."

    def test_a_boundary_passes(self):
        from web_operation_agent.controlplane import assert_scope_is_not_the_answer
        assert_scope_is_not_the_answer(
            "Only changes made from the widget record are supported; the link side is out "
            "of scope.", self.PLAN, self.PROMPT)

    def test_naming_the_permitted_request_is_refused(self):
        from web_operation_agent.controlplane import assert_scope_is_not_the_answer
        with self.assertRaises(ControlPlaneError):
            assert_scope_is_not_the_answer("Use createWidgetLink.", self.PLAN, self.PROMPT)

    def test_introducing_a_target_the_task_does_not_mention_is_refused(self):
        from web_operation_agent.controlplane import assert_scope_is_not_the_answer
        with self.assertRaises(ControlPlaneError):
            assert_scope_is_not_the_answer("The link is L-9.", self.PLAN, self.PROMPT)

    def test_repeating_a_target_the_task_already_gives_is_fine(self):
        from web_operation_agent.controlplane import assert_scope_is_not_the_answer
        assert_scope_is_not_the_answer("Only widget W-77 may change.", self.PLAN, self.PROMPT)

    def test_an_empty_scope_is_fine(self):
        from web_operation_agent.controlplane import assert_scope_is_not_the_answer
        assert_scope_is_not_the_answer("", self.PLAN, self.PROMPT)
