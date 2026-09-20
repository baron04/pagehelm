from __future__ import annotations

import dataclasses
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from web_operation_agent.transport import (BrowserExecutor, TransportError, TurnCancelled)
from web_operation_agent.transport.gateway import EmbeddedGateway
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.transport import (RemoteBrowserClient, RemoteProtocolError,
                                         RemoteSessionClosed, RemoteSessionLimits,
                                         RemoteSessionRegistry)
from web_operation_agent.schemas import (
    SCHEMA_VERSIONS,
    ActionCommand,
    ActionEnvelope,
    ActionReceipt,
    ActionResultEnvelope,
    ApprovalDecision,
    ApprovalRequest,
    CancellationRequest,
    ClientHello,
    ElementRef,
    ObservationEnvelope,
    PageObservation,
)

from web_operation_agent.tests.transport.support import (
    DEFAULT_LEASE, ORIGIN, action_for, completed, hello, observation, principal,
)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.registry = RemoteSessionRegistry()
        self.grant, self.channel = self.registry.create(allowed_origins=[ORIGIN],
                                                        session_id="http-session")
        self.gateway = EmbeddedGateway(self.registry, sse_wait_s=0.05).start()
        self.lease = ""

    def tearDown(self):
        self.gateway.close()

    def request(self, method: str, path: str, body=None, *, token: str | None = None,
                origin: str = ORIGIN, headers=None, lease=DEFAULT_LEASE):
        payload = None if body is None else json.dumps(body).encode()
        request_headers = {"Origin": origin,
                           "Authorization": "Bearer " + (token or self.grant.client_token)}
        selected_lease = self.lease if lease is DEFAULT_LEASE else lease
        if selected_lease:
            request_headers["X-WOA-Lease"] = selected_lease
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(self.gateway.base_url + path, data=payload,
                                         method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_authenticated_http_sse_action_roundtrip_and_duplicate_result(self):
        sid = self.grant.session_id
        status, headers, raw_hello = self.request(
            "POST", f"/v1/sessions/{sid}/hello", hello(sid).to_json())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.lease = json.loads(raw_hello)["lease_id"]
        first = observation(sid, 1)
        self.assertEqual(self.request("POST", f"/v1/sessions/{sid}/observations",
                                      first.to_json())[0], 200)

        action = action_for(sid, first)
        self.channel.enqueue_action(action)
        status, headers, raw = self.request(
            "GET", f"/v1/sessions/{sid}/events", headers={"Last-Event-Id": "e00000001"})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        self.assertIn(f"id: e00000002".encode(), raw)
        self.assertIn(action.action_id.encode(), raw)
        claim_reply = json.loads(self.request(
            "POST", f"/v1/sessions/{sid}/claims",
            {"action_id": action.action_id})[2])
        self.assertFalse(claim_reply["duplicate"])

        after = observation(sid, 2, caused_by=action.action_id, url=ORIGIN + "/next")
        result = completed(action, after)
        first_reply = json.loads(self.request("POST", f"/v1/sessions/{sid}/results",
                                              result.to_json())[2])
        retry_reply = json.loads(self.request("POST", f"/v1/sessions/{sid}/results",
                                              result.to_json())[2])
        self.assertFalse(first_reply["duplicate"])
        self.assertTrue(retry_reply["duplicate"])

    def test_authenticated_approval_endpoint_roundtrip(self):
        sid = self.grant.session_id
        _, _, raw_hello = self.request(
            "POST", f"/v1/sessions/{sid}/hello", hello(sid).to_json())
        self.lease = json.loads(raw_hello)["lease_id"]
        request = ApprovalRequest(
            1, "approval-http", "Update the quote", ("Quote Q-1042",),
            int(time.time() * 1000) + 5000)
        self.channel.request_approval(request)
        body = ApprovalDecision(1, sid, request.approval_id, "approved").to_json()
        status, _, raw = self.request(
            "POST", f"/v1/sessions/{sid}/approvals", body)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["duplicate"])
        status, _, raw = self.request(
            "POST", f"/v1/sessions/{sid}/approvals", body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["duplicate"])

    def test_authenticated_cancellation_endpoint_is_message_bound(self):
        sid = self.grant.session_id
        _, _, raw_hello = self.request(
            "POST", f"/v1/sessions/{sid}/hello", hello(sid).to_json())
        self.lease = json.loads(raw_hello)["lease_id"]
        message = {"message_id": "message-http", "text": "Open next",
                   "page_url": ORIGIN + "/start"}
        self.assertEqual(self.request(
            "POST", f"/v1/sessions/{sid}/messages", message)[0], 200)
        self.channel.wait_for_message(timeout_s=0)
        body = CancellationRequest(1, sid, "message-http").to_json()
        status, _, raw = self.request(
            "POST", f"/v1/sessions/{sid}/cancellations", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"accepted": True, "duplicate": False})
        self.assertTrue(self.channel.message_cancelled("message-http"))

    def test_token_origin_cors_and_static_runtime_boundary(self):
        sid = self.grant.session_id
        self.assertEqual(self.request("POST", f"/v1/sessions/{sid}/hello",
                                      hello(sid).to_json(), token="wrong")[0], 401)
        self.assertEqual(self.request("POST", f"/v1/sessions/{sid}/hello",
                                      hello(sid).to_json(), origin="http://evil.test")[0], 403)
        # `assistant.js` and the panel are build artifacts; where one is absent the gateway
        # names the prerequisite instead of serving whatever happens to be in the directory.
        status, headers, body = self.request("GET", "/v1/runtime/assistant.js")
        if status == 200:
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            self.assertIn(b"class EmbeddedWebRuntime", body)
            self.assertNotIn(b"OPENAI_API_KEY", body)
            self.assertNotIn(b"ControlPlan", body)
        else:
            self.assertEqual(status, 404)
            self.assertIn(b"runtime_unavailable", body)
        status, headers, body = self.request("GET", "/v1/runtime/panel/panel.js")
        if status == 200:
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("immutable", headers["Cache-Control"])
            self.assertNotIn(b"clientToken", body)
            self.assertNotIn(b"ControlPlan", body)
        else:
            self.assertEqual(status, 404)
            self.assertIn(b"panel_unavailable", body)
        # Path traversal out of the panel directory is refused by the manifest, not by
        # string surgery on the request path.
        self.assertEqual(
            self.request("GET", "/v1/runtime/panel/../../assistant.js")[0], 404)

    def test_an_unbuilt_runtime_is_named_rather_than_answered_with_an_error(self):
        """`assistant.js` is a build artifact, like the panel, and is missing the same way."""
        from pathlib import Path

        from web_operation_agent.transport import gateway as embedded_gateway
        missing = Path("/nonexistent/assistant.js")
        with mock.patch.dict(embedded_gateway.RUNTIME_MODULES,
                             {"/v1/runtime/assistant.js": missing}):
            status, _, body = self.request("GET", "/v1/runtime/assistant.js")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "runtime_unavailable")

    def test_liveness_and_default_readiness_are_content_free(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "ok": True, "protocol": 1, "schema_version": 1})
        status, _, body = self.request("GET", "/readyz")
        self.assertEqual(status, 200)
        readiness = json.loads(body)
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["session_count"], 1)
        self.assertNotIn(self.grant.session_id, body.decode())
        self.assertNotIn(self.grant.client_token, body.decode())

    def test_readiness_callback_failure_is_masked_and_fails_closed(self):
        def broken_status():
            raise RuntimeError("secret readiness detail")

        with EmbeddedGateway(
                RemoteSessionRegistry(), sse_wait_s=0.01,
                readiness_provider=broken_status) as gateway:
            request = urllib.request.Request(gateway.base_url + "/readyz")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 503)
            body = raised.exception.read()
        readiness = json.loads(body)
        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["status_error"])
        self.assertNotIn(b"secret", body)

    def test_open_provider_circuit_makes_readiness_unavailable(self):
        def status():
            return {
                "ready": True,
                "accepting_sessions": True,
                "session_count": 2,
                "session_capacity": 10,
                "provider": {
                    "max_concurrent_requests": 4,
                    "in_flight": 0,
                    "consecutive_transient_failures": 3,
                    "circuit_open": True,
                    "retry_after_s": 12.5,
                    "untrusted_extra": "must not be exposed",
                },
                "untrusted_extra": "must not be exposed",
            }

        with EmbeddedGateway(
                RemoteSessionRegistry(), sse_wait_s=0.01,
                readiness_provider=status) as gateway:
            request = urllib.request.Request(gateway.base_url + "/readyz")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 503)
            body = raised.exception.read()
        readiness = json.loads(body)
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["accepting_sessions"])
        self.assertTrue(readiness["provider"]["circuit_open"])
        self.assertEqual(readiness["provider"]["retry_after_s"], 12.5)
        self.assertNotIn(b"untrusted_extra", body)

    def test_gateway_builds_one_ephemeral_first_party_bootstrap(self):
        source = self.gateway.bootstrap_script(
            self.grant, page_controller_module=ORIGIN + "/page-controller.js")
        self.assertIn(self.gateway.runtime_url, source)
        self.assertIn(self.grant.session_id, source)
        # The harness hands the page a whole session and loads the same `assistant.js` a
        # production page includes, so both are booted by the same supervisor.
        self.assertIn("window.__WOA_EMBED_CONFIG__ =", source)
        self.assertIn("__WOA_ASSISTANT__", source)
        self.assertNotIn("startEmbeddedRuntime", source)
        self.assertIn("location.origin", source)
        self.assertIn(ORIGIN, source)
        self.assertNotIn("ControlPlan", source)
        with self.assertRaisesRegex(ValueError, "allowed session origin"):
            self.gateway.bootstrap_script(
                self.grant, page_controller_module="http://other.test/controller.js")

    def test_public_proxy_url_is_separate_from_the_loopback_listener(self):
        registry = RemoteSessionRegistry()
        grant, _channel = registry.create(
            allowed_origins=["https://app.example"], session_id="proxied-session")
        with EmbeddedGateway(
                registry, public_base_url="https://app.example/assistant") as gateway:
            config = gateway.bootstrap_config(
                grant,
                page_controller_module="https://app.example/woa/page-controller.js")
            self.assertTrue(gateway.base_url.startswith("http://127.0.0.1:"))
            self.assertEqual(gateway.public_base_url, "https://app.example/assistant")
            self.assertEqual(config["endpoint"], "https://app.example/assistant")
            self.assertEqual(
                gateway.runtime_url,
                "https://app.example/assistant/v1/runtime/assistant.js")
            source = gateway.bootstrap_script(
                grant,
                page_controller_module="https://app.example/woa/page-controller.js")
            self.assertIn("https://app.example/assistant/v1/runtime/assistant.js", source)
            self.assertNotIn(gateway.base_url, source)

    def test_non_loopback_bind_requires_an_explicit_private_network_boundary(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            EmbeddedGateway(host="0.0.0.0")
        with EmbeddedGateway(
                host="0.0.0.0", allow_private_network_bind=True) as gateway:
            self.assertTrue(gateway.base_url.startswith("http://127.0.0.1:"))

    def test_public_proxy_url_refuses_downgrade_and_ambiguous_paths(self):
        for value in (
                "http://app.example/assistant",
                "https://user:secret@app.example/assistant",
                "https://app.example/assistant/",
                "https://app.example/assistant?token=secret",
                "https://app.example/a/../assistant"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "public_base_url"):
                    EmbeddedGateway(public_base_url=value)

    def test_hello_page_origin_must_match_browser_origin_header(self):
        registry = RemoteSessionRegistry()
        grant, _ = registry.create(
            allowed_origins=[ORIGIN, "http://other.test"],
            session_id="multi-origin-session",
        )
        with EmbeddedGateway(registry, sse_wait_s=0.01) as gateway:
            body = hello(grant.session_id).to_json()
            body["page_origin"] = "http://other.test"
            payload = json.dumps(body).encode()
            request = urllib.request.Request(
                gateway.base_url + f"/v1/sessions/{grant.session_id}/hello",
                data=payload,
                method="POST",
                headers={"Origin": ORIGIN,
                         "Authorization": "Bearer " + grant.client_token,
                         "Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 403)
            self.assertEqual(json.loads(raised.exception.read())["code"],
                             "origin_refused")

    def test_privacy_boundary_rejects_session_and_generic_credentials(self):
        sid = self.grant.session_id
        _, _, raw = self.request("POST", f"/v1/sessions/{sid}/hello",
                                 hello(sid).to_json())
        self.lease = json.loads(raw)["lease_id"]
        handoff = observation(
            sid, 1, url=ORIGIN + "/?externalLoginKey=secret-value")
        raw_token = observation(
            sid, 1, url=ORIGIN + "/callback?access_token=raw-secret&next=1")
        encoded_token = observation(
            sid, 1,
            url=ORIGIN + "/callback?access_token%253Dencoded-secret%2526next%253D1")
        triple_encoded_key_token = observation(
            sid, 1, url=(ORIGIN +
                "/callback?access%25255Ftoken%25253Dtriple-secret%252526next%25253D1"))
        triple_encoded_handoff = observation(
            sid, 1, url=(ORIGIN +
                "/callback?external%25254CoginKey%25253Dhandoff-secret%252526next%25253D1"))
        structured = observation(sid, 1)
        structured.observation.elements[0].attrs.update(
            {"name": "access_token", "value": "structured-secret"})
        direct = ObservationEnvelope(
            schema_version=SCHEMA_VERSIONS["ObservationEnvelope"],
            session_id=sid, observation_id="o-direct", page_epoch=1,
            observation=observation(sid, 1).observation,
            events=({"api_key": "direct-secret"},),
        )
        for leaked in (handoff, raw_token, encoded_token, triple_encoded_key_token,
                       triple_encoded_handoff, structured, direct):
            with self.subTest(leaked=leaked.observation_id):
                status, _, body = self.request(
                    "POST", f"/v1/sessions/{sid}/observations", leaked.to_json())
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["code"], "privacy_boundary")

        redacted = observation(
            sid, 1,
            url=ORIGIN + "/callback?access_token%253D%255Bredacted%255D")
        redacted.observation.elements[0].attrs.update(
            {"name": "access_token", "value": "[redacted]"})
        self.assertEqual(self.request(
            "POST", f"/v1/sessions/{sid}/observations", redacted.to_json())[0], 200)

    def test_session_endpoints_require_current_runtime_lease(self):
        sid = self.grant.session_id
        _, _, raw = self.request("POST", f"/v1/sessions/{sid}/hello",
                                 hello(sid).to_json())
        first_lease = json.loads(raw)["lease_id"]
        self.lease = first_lease
        first = observation(sid, 1)

        status, _, body = self.request(
            "POST", f"/v1/sessions/{sid}/observations", first.to_json(), lease="")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["code"], "invalid_lease")
        status, _, body = self.request(
            "POST", f"/v1/sessions/{sid}/observations", first.to_json(),
            lease="fabricated-lease")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "runtime_replaced")
        self.assertEqual(self.request(
            "POST", f"/v1/sessions/{sid}/observations", first.to_json())[0], 200)

        _, _, takeover_raw = self.request(
            "POST", f"/v1/sessions/{sid}/hello",
            hello(sid, runtime_instance_id="runtime-document-2").to_json(), lease="")
        self.lease = json.loads(takeover_raw)["lease_id"]
        status, _, body = self.request(
            "GET", f"/v1/sessions/{sid}/events", lease=first_lease)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "runtime_replaced")

    def test_cors_preflight_advertises_lease_header(self):
        sid = self.grant.session_id
        status, headers, _ = self.request(
            "OPTIONS", f"/v1/sessions/{sid}/observations")
        self.assertEqual(status, 204)
        self.assertIn("X-WOA-Lease", headers["Access-Control-Allow-Headers"])

    def test_bootstrap_requires_loadable_page_controller_url(self):
        with self.assertRaises(TypeError):
            self.gateway.bootstrap_config(self.grant)
        for invalid in ("@page-agent/page-controller", "/assets/page-controller.js",
                        "javascript:alert(1)", "https://user:pass@example.test/module.js"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "absolute"):
                self.gateway.bootstrap_config(
                    self.grant, page_controller_module=invalid)
        with self.assertRaisesRegex(ValueError, "allowed session origin"):
            self.gateway.bootstrap_config(
                self.grant,
                page_controller_module="https://cdn.example.test/page-controller.js?v=1",
            )
        module_url = ORIGIN + "/page-controller.js?v=1"
        config = self.gateway.bootstrap_config(
            self.grant, page_controller_module=module_url,
            panel_labels={"assistant": "Operations copilot", "send": "Send",
                          "statusReady": "Connected"},
            sensitive_query_keys={"handoffKey"})
        self.assertEqual(config["pageControllerModule"], module_url)
        self.assertEqual(config["navigationMode"], "document")
        self.assertEqual(config["principalExpiresAt"], 0)
        self.assertEqual(config["sessionExpiresAt"], self.grant.session_expires_at)
        self.assertEqual(config["panelLabels"], {
            "assistant": "Operations copilot", "send": "Send",
            "statusReady": "Connected"})
        self.assertEqual(config["sensitiveQueryKeys"], ["handoffkey"])
        with self.assertRaisesRegex(ValueError, "unknown panel label"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                panel_labels={"madeUp": "No"})
        with self.assertRaisesRegex(ValueError, "1-200"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                panel_labels={"assistant": "bad\nlabel"})
        spa = self.gateway.bootstrap_config(
            self.grant, page_controller_module=module_url, navigation_mode="spa")
        self.assertEqual(spa["navigationMode"], "spa")
        with self.assertRaisesRegex(ValueError, "navigation_mode"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                navigation_mode="invented")
        with self.assertRaisesRegex(ValueError, "sensitive_query_keys"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                sensitive_query_keys={"bad key"})
        normalized = self.gateway.bootstrap_config(
            self.grant,
            page_controller_module="http://APP.TEST:80/page-controller.js",
        )
        self.assertEqual(normalized["pageControllerModule"],
                         "http://APP.TEST:80/page-controller.js")


if __name__ == "__main__":
    unittest.main()
