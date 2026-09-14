from __future__ import annotations

import json
import importlib.util
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.identity import PrincipalContext
from web_operation_agent_odoo.adapter import OdooApplicationAdapter

from web_operation_agent_odoo.native import (
    OdooDelegationDirectory, OdooSessionEvent, OdooSessionEventError,
    SignedOdooSessionVerifier, canonical_event, load_session_secret,
    sign_session_event,
)
from web_operation_agent_odoo.native_server import OdooNativeConfig, OdooNativeHost


SECRET = b"a deployment-owned test secret!!" * 2


def _event(**overrides) -> OdooSessionEvent:
    values = {
        "schema_version": 1,
        "event": "bootstrap",
        "database": "trial",
        "company_id": "7",
        "actor_id": "42",
        "runtime_instance_id": "a" * 32,
        "issued_at": 1_000_000,
        "expires_at": 1_030_000,
        "nonce": "b" * 32,
    }
    values.update(overrides)
    return OdooSessionEvent(**values)


class SignedOdooSessionContract(unittest.TestCase):
    def test_a_signed_bootstrap_becomes_only_a_short_lived_page_assertion(self):
        event = SignedOdooSessionVerifier(SECRET).verify(
            sign_session_event(_event(), SECRET), now_ms=1_000_001)
        identity = event.page_identity()

        self.assertEqual(identity.source, "page_session")
        self.assertEqual(
            (identity.tenant_id, identity.workspace_id, identity.actor_id),
            ("trial", "7", "42"))
        self.assertNotIn("secret", json.dumps(event.to_json()))

    def test_the_browser_cannot_change_any_signed_identifier(self):
        envelope = sign_session_event(_event(), SECRET)
        envelope["event"]["actor_id"] = "43"
        with self.assertRaisesRegex(OdooSessionEventError, "did not verify"):
            SignedOdooSessionVerifier(SECRET).verify(envelope, now_ms=1_000_001)

    def test_a_nonce_is_consumed_once_even_when_the_envelope_is_identical(self):
        envelope = sign_session_event(_event(), SECRET)
        verifier = SignedOdooSessionVerifier(SECRET)
        verifier.verify(envelope, now_ms=1_000_001)
        with self.assertRaisesRegex(OdooSessionEventError, "already consumed"):
            verifier.verify(envelope, now_ms=1_000_002)

    def test_expired_future_and_oversized_lifetimes_are_refused(self):
        verifier = SignedOdooSessionVerifier(SECRET)
        with self.assertRaisesRegex(OdooSessionEventError, "not currently valid"):
            verifier.verify(sign_session_event(_event(), SECRET), now_ms=1_030_000)
        with self.assertRaisesRegex(OdooSessionEventError, "not currently valid"):
            SignedOdooSessionVerifier(SECRET).verify(
                sign_session_event(_event(issued_at=1_010_001, expires_at=1_020_000,
                                          nonce="c" * 32), SECRET),
                now_ms=1_000_000)
        with self.assertRaisesRegex(OdooSessionEventError, "lifetime"):
            _event(expires_at=1_060_001)

    def test_revoke_covers_every_runtime_for_one_server_resolved_actor(self):
        event = _event(event="revoke", runtime_instance_id="")
        verified = SignedOdooSessionVerifier(SECRET).verify(
            sign_session_event(event, SECRET), now_ms=1_000_001)
        self.assertEqual(verified.event, "revoke")
        with self.assertRaisesRegex(OdooSessionEventError, "only a bootstrap"):
            verified.page_identity()

    def test_canonical_bytes_are_stable_and_unknown_fields_are_refused(self):
        event = _event()
        self.assertEqual(canonical_event(event), canonical_event(
            OdooSessionEvent.from_json(dict(reversed(list(event.to_json().items()))))))
        value = event.to_json() | {"permissions": ["admin"]}
        with self.assertRaisesRegex(OdooSessionEventError, "fields"):
            OdooSessionEvent.from_json(value)

    def test_secret_file_permissions_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-secret"
            path.write_bytes(SECRET)
            os.chmod(path, 0o600)
            self.assertEqual(load_session_secret(path), SECRET)
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(OdooSessionEventError, "0600"):
                load_session_secret(path)

    def test_the_independent_odoo_addon_producer_matches_the_agent_verifier(self):
        addon = (Path(__file__).resolve().parents[3] / "deploy" / "addons"
                 / "woa_runtime" / "claims.py")
        spec = importlib.util.spec_from_file_location("woa_runtime_claims", addon)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        produced = module.session_event(
            event="bootstrap", database="trial", company_id=7, actor_id=42,
            runtime_instance_id="a" * 32, now_ms=1_000_000)
        produced["nonce"] = "b" * 32

        verified = SignedOdooSessionVerifier(SECRET).verify(
            module.signed_envelope(produced, SECRET), now_ms=1_000_001)

        self.assertEqual(verified, _event())


class PerUserDelegationContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "keys").mkdir()
        self.key = self.root / "keys" / "actor-42.json"
        self.key.write_text(json.dumps({
            "database": "trial", "login": "reader@example.test",
            "api_key": "a" * 32,
        }))
        self.manifest = self.root / "delegations.json"
        self.manifest.write_text(json.dumps({
            "schema_version": 1,
            "principals": {
                "trial:42": {
                    "api_key_file": "keys/actor-42.json",
                    "scopes": ["customers:read"],
                    "allow_mutations": False,
                },
            },
        }))
        os.chmod(self.key, 0o600)
        os.chmod(self.manifest, 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_signed_actor_selects_only_its_own_private_delegation(self):
        selected = OdooDelegationDirectory(self.root).resolve(_event())
        self.assertEqual(selected.api_key_path, self.key.resolve())
        self.assertEqual(selected.scopes, ("customers:read",))
        self.assertFalse(selected.allow_mutations)

    def test_an_unconfigured_actor_and_a_path_escape_are_refused(self):
        with self.assertRaisesRegex(OdooSessionEventError, "no configured"):
            OdooDelegationDirectory(self.root).resolve(_event(actor_id="43"))
        value = json.loads(self.manifest.read_text())
        value["principals"]["trial:42"]["api_key_file"] = "../stolen.json"
        self.manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(OdooSessionEventError, "escapes"):
            OdooDelegationDirectory(self.root).resolve(_event())

    def test_world_readable_credentials_are_refused(self):
        os.chmod(self.key, 0o644)
        with self.assertRaisesRegex(OdooSessionEventError, "0600"):
            OdooDelegationDirectory(self.root).resolve(_event())


class OrdinaryBrowserHostContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.secret_path = self.root / "session-secret"
        self.secret_path.write_bytes(SECRET)
        os.chmod(self.secret_path, 0o600)
        (self.root / "delegations.json").write_text(json.dumps({
            "schema_version": 1, "principals": {},
        }))
        os.chmod(self.root / "delegations.json", 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _post(url, payload):
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _bound_adapter(self, now_ms):
        key = self.root / "test-key.json"
        key.write_text(json.dumps({
            "database": "trial", "login": "user@example.test",
            "api_key": "a" * 32,
        }))
        os.chmod(key, 0o600)
        adapter = OdooApplicationAdapter(
            base_url="http://127.0.0.1:8069",
            public_origin="http://127.0.0.1:18069",
            api_key_path=key,
            page_controller_module=(
                "http://127.0.0.1:18069/woa_runtime/static/src/page-controller.js"))
        principal = PrincipalContext(
            schema_version=1, application_id="odoo", tenant_id="trial",
            workspace_id="7", actor_id="42", delegation_id="test-delegation",
            issued_at=now_ms, expires_at=now_ms + 300_000,
            scopes=tuple(sorted(adapter.ports.identity.required_scopes)))
        return adapter, principal, False

    def test_signed_bootstrap_issues_one_capability_and_logout_revokes_it(self):
        now = int(time.time() * 1000)
        config = OdooNativeConfig(
            rpc_base_url="http://127.0.0.1:8069",
            public_origin="http://127.0.0.1:18069",
            public_agent_url="http://127.0.0.1:18766/assistant",
            delegation_dir=self.root,
            session_secret_path=self.secret_path,
            state_dir=self.root / "state",
            gateway_port=0,
            bootstrap_port=0,
            scripted=True)
        host = OdooNativeHost(config)
        with patch.object(host, "_adapter_and_principal",
                          return_value=self._bound_adapter(now)):
            with host:
                bootstrap = OdooSessionEvent(
                    schema_version=1, event="bootstrap", database="trial",
                    company_id="7", actor_id="42", runtime_instance_id="a" * 32,
                    issued_at=now, expires_at=now + 30_000, nonce="c" * 32)
                envelope = sign_session_event(bootstrap, SECRET)
                status, payload = self._post(
                    host.bootstrap_server.base_url + "/bootstrap", envelope)

                self.assertEqual(status, 200)
                self.assertEqual(host.server.session_count, 1)
                self.assertEqual(payload["schema_version"], 1)
                self.assertIn("clientToken", payload["config"])
                self.assertNotIn("actor_id", json.dumps(payload))
                with urllib.request.urlopen(
                        host.bootstrap_server.base_url + "/internal/metrics") as response:
                    metrics = json.loads(response.read())
                self.assertEqual(metrics["schema_version"], 1)
                self.assertEqual(metrics["active_runs"], 0)
                self.assertNotIn("actor", json.dumps(metrics))

                replay_status, replay = self._post(
                    host.bootstrap_server.base_url + "/bootstrap", envelope)
                self.assertEqual((replay_status, replay),
                                 (401, {"error": "invalid_session_event"}))

                revoke = OdooSessionEvent(
                    schema_version=1, event="revoke", database="trial",
                    company_id="7", actor_id="42", runtime_instance_id="",
                    issued_at=now + 1, expires_at=now + 30_001, nonce="d" * 32)
                revoke_status, revoked = self._post(
                    host.bootstrap_server.base_url + "/internal/revoke",
                    sign_session_event(revoke, SECRET))
                self.assertEqual((revoke_status, revoked), (200, {"revoked": 1}))
                self.assertEqual(host.server.session_count, 0)

if __name__ == "__main__":
    unittest.main()
