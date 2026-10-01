"""Host lifecycle over synthetic identity responses; no application or model calls."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.schemas import ClientHello
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent_yudao.application import YudaoIdentityPort
from web_operation_agent_yudao.native_server import BootstrapRefused, YudaoHost, YudaoHostConfig


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hosts = []
        self.addCleanup(lambda: [h.close() for h in self.hosts])
        self.config = YudaoHostConfig(
            api_base="http://yudao.invalid", public_origin="https://yudao.invalid",
            state_dir=Path(self.tmp.name), gateway_host="127.0.0.1", gateway_port=0)
        self.addCleanup(patch.stopall)
        patch("web_operation_agent_yudao.native_server._api_docs", return_value={}).start()
        self.chat = patch("web_operation_agent_yudao.native_server.ChatModel").start()
        self.chat.return_value.model = "scripted/no-model"
        self.chat.return_value.complete.side_effect = AssertionError("models are forbidden in this test")

        def actor(token):
            if token not in ("synthetic-a", "synthetic-a-new", "synthetic-b"):
                raise PrincipalError("not signed in")
            return {"id": "2" if token == "synthetic-b" else "1", "deptId": "10"}

        def page_identity(port, token):
            return port._assertion(actor(token), "page_session")

        def request(client, method, path, **kwargs):
            if method != "GET" or path != "system/auth/get-permission-info":
                raise AssertionError((method, path))
            return 0, {"code": 0, "data": {"user": actor(kwargs["token"])}}

        patch.object(YudaoIdentityPort, "page_identity", page_identity).start()
        patch("web_operation_agent_yudao.client.YudaoClient._request", request).start()

    def host(self):
        host = YudaoHost(self.config)
        self.hosts.append(host)
        return host

    def test_tabs_share_the_actor_s_session_and_renew_only_its_token(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        active = host._sessions[("1", "1")]
        renewed = host.bootstrap("synthetic-a-new", "1", "tab-b")
        self.assertEqual(first["config"]["sessionId"], renewed["config"]["sessionId"])
        self.assertIs(host._sessions[("1", "1")], active)
        self.assertEqual(active.client._token, "synthetic-a-new")
        self.assertEqual(active.session.channel.principal.delegation_id, active.delegation_id)
        self.assertEqual(host.server.session_count, 1)
        self.chat.return_value.complete.assert_not_called()

    def test_bootstrap_and_renewal_leave_navigation_to_the_application_document(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        renewed = host.bootstrap("synthetic-a-new", "1", "tab-a")
        # A synthetic popstate with the Runtime's own state discards Vue Router's
        # current path; its next ordinary push then falls back to origin + undefined.
        for response in (first, renewed):
            self.assertEqual(response["config"]["navigationMode"], "document")
        self.assertEqual(host._sessions[("1", "1")].session.navigation_mode, "document")
        self.chat.return_value.complete.assert_not_called()

    def test_actors_and_tenants_get_separate_sessions_and_tokens_never_reach_disk(self):
        host = self.host()
        responses = [host.bootstrap(token, tenant, "tab") for token, tenant in (
            ("synthetic-a", "1"), ("synthetic-b", "1"), ("synthetic-a", "2"))]
        self.assertEqual(len({r["config"]["sessionId"] for r in responses}), 3)
        self.assertEqual(host.server.session_count, 3)
        host.close()
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"synthetic-a", path.read_bytes())
                self.assertNotIn(b"synthetic-b", path.read_bytes())

    def test_restart_recovers_history_with_a_new_capability_without_replaying_a_turn(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab")
        session = host._sessions[("1", "1")].session
        session.channel.publish("progress", {"state": "finished", "message": "synthetic previous answer"}, turn_id="previous")
        host.close()
        restored = self.host()
        second = restored.bootstrap("synthetic-a", "1", "tab-new")
        recovered = restored._sessions[("1", "1")].session
        self.assertEqual(first["config"]["sessionId"], second["config"]["sessionId"])
        self.assertNotEqual(first["config"]["clientToken"], second["config"]["clientToken"])
        self.assertTrue(recovered.channel.recovered_history)
        principal = recovered.channel.principal
        lease = recovered.channel.accept_hello(ClientHello(
            schema_version=1, session_id=recovered.grant.session_id,
            client_id="tab", runtime="test/1", runtime_instance_id="restored-document",
            capabilities=("dom.observe",), page_origin=self.config.public_origin,
            application_id=principal.application_id, principal_digest=principal.binding_digest))["lease_id"]
        self.assertIn("synthetic previous answer", json.dumps(
            recovered.channel.panel_timeline(lease_id=lease)))
        self.chat.return_value.complete.assert_not_called()

    def test_invalid_bootstrap_does_not_create_a_session(self):
        host = self.host()
        for token, tenant, instance in (("", "1", "tab"), ("invalid", "1", "tab"),
                                        ("synthetic-a", "bad", "tab"), ("synthetic-a", "1", "")):
            with self.subTest(token=token), self.assertRaises(BootstrapRefused):
                host.bootstrap(token, tenant, instance)
        self.assertEqual(host.server.session_count, 0)
        self.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
