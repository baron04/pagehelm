from __future__ import annotations

import json
import importlib.util
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.durable import SQLiteExpiringNonceStore, SQLiteSessionStore
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.remote import RemoteProtocolError
from web_operation_agent_odoo.adapter import OdooApplicationAdapter

from web_operation_agent_odoo.native import (
    OdooDelegation, OdooDelegationDirectory, OdooSessionEvent, OdooSessionEventError,
    SignedOdooSessionVerifier, canonical_event, load_session_secret,
    sign_session_event,
)
from web_operation_agent_odoo.native_server import (OdooAuthorityMismatchError,
                                                     OdooNativeConfig,
                                                     OdooNativeHost)


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

    def test_a_nonce_stays_consumed_after_the_verifier_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSessionStore(Path(tmp) / "state.sqlite3")
            nonces = SQLiteExpiringNonceStore(store, namespace="odoo_session_event")
            consume = lambda nonce, expires, now: nonces.consume(
                nonce, expires_at=expires, now_ms=now)
            envelope = sign_session_event(_event(), SECRET)
            SignedOdooSessionVerifier(SECRET, nonce_consumer=consume).verify(
                envelope, now_ms=1_000_001)
            restarted = SQLiteExpiringNonceStore(
                SQLiteSessionStore(Path(tmp) / "state.sqlite3"),
                namespace="odoo_session_event")
            with self.assertRaisesRegex(OdooSessionEventError, "already consumed"):
                SignedOdooSessionVerifier(
                    SECRET,
                    nonce_consumer=lambda nonce, expires, now: restarted.consume(
                        nonce, expires_at=expires, now_ms=now),
                ).verify(envelope, now_ms=1_000_002)

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
        return adapter, principal, False, OdooDelegation(
            api_key_path=key,
            scopes=principal.scopes,
            allow_mutations=False,
        )

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

                renewed_event = OdooSessionEvent(
                    schema_version=1, event="bootstrap", database="trial",
                    company_id="7", actor_id="42", runtime_instance_id="a" * 32,
                    issued_at=now + 1, expires_at=now + 30_001, nonce="e" * 32)
                with patch.object(host, "_renew_active") as renew:
                    renew_status, renewed = self._post(
                        host.bootstrap_server.base_url + "/bootstrap",
                        sign_session_event(renewed_event, SECRET))
                self.assertEqual(renew_status, 200)
                self.assertEqual(renewed["config"]["clientToken"],
                                 payload["config"]["clientToken"])
                self.assertGreater(renewed["renew_at"], int(time.time() * 1000))
                renew.assert_called_once()

                busy_event = OdooSessionEvent(
                    schema_version=1, event="bootstrap", database="trial",
                    company_id="7", actor_id="42", runtime_instance_id="a" * 32,
                    issued_at=now + 2, expires_at=now + 30_002, nonce="f" * 32)
                # A session mid-turn, or holding a confirmation, cannot have its authority
                # swapped underneath it -- but this is the same page coming back, and
                # refusing it left the person with no panel at all and no way to answer the
                # very confirmation the refusal was about. It reconnects to its own session
                # and keeps the assertion it already has.
                with patch.object(host, "_renew_active", side_effect=RemoteProtocolError(
                        "principal renewal requires an idle session",
                        code="session_busy_turn", status=409)):
                    busy_status, busy = self._post(
                        host.bootstrap_server.base_url + "/bootstrap",
                        sign_session_event(busy_event, SECRET))
                self.assertEqual(busy_status, 200)
                self.assertEqual(busy["config"]["clientToken"],
                                 payload["config"]["clientToken"])
                # Any other protocol failure is still a refusal: only busy is survivable.
                other_event = OdooSessionEvent(
                    schema_version=1, event="bootstrap", database="trial",
                    company_id="7", actor_id="42", runtime_instance_id="a" * 32,
                    issued_at=now + 3, expires_at=now + 30_003, nonce="9" * 32)
                with patch.object(host, "_renew_active", side_effect=RemoteProtocolError(
                        "nope", code="principal_mismatch", status=409)):
                    other_status, _other = self._post(
                        host.bootstrap_server.base_url + "/bootstrap",
                        sign_session_event(other_event, SECRET))
                self.assertEqual(other_status, 503)

                revoke = OdooSessionEvent(
                    schema_version=1, event="revoke", database="trial",
                    company_id="7", actor_id="42", runtime_instance_id="",
                    issued_at=now + 1, expires_at=now + 30_001, nonce="d" * 32)
                revoke_status, revoked = self._post(
                    host.bootstrap_server.base_url + "/internal/revoke",
                    sign_session_event(revoke, SECRET))
                self.assertEqual((revoke_status, revoked), (200, {"revoked": 1}))
                self.assertEqual(host.server.session_count, 0)

    def test_slow_identity_rpc_does_not_hold_the_host_wide_lock(self):
        now = int(time.time() * 1000)
        config = OdooNativeConfig(
            rpc_base_url="http://127.0.0.1:8069",
            public_origin="http://127.0.0.1:18069",
            public_agent_url="http://127.0.0.1:18766/assistant",
            delegation_dir=self.root,
            session_secret_path=self.secret_path,
            state_dir=self.root / "state-lock",
            gateway_port=0,
            bootstrap_port=0,
            scripted=True)
        host = OdooNativeHost(config)
        entered = threading.Event()
        release = threading.Event()
        failure = []

        def slow_identity(_event):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release identity lookup")
            return self._bound_adapter(now)

        event = OdooSessionEvent(
            schema_version=1, event="bootstrap", database="trial",
            company_id="7", actor_id="42", runtime_instance_id="b" * 32,
            issued_at=now, expires_at=now + 30_000, nonce="f" * 32)

        def bootstrap():
            try:
                host.bootstrap(sign_session_event(event, SECRET))
            except BaseException as exc:
                failure.append(exc)

        with patch.object(host, "_adapter_and_principal", side_effect=slow_identity):
            worker = threading.Thread(target=bootstrap, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(1))
            acquired = host._lock.acquire(blocking=False)
            self.assertTrue(acquired, "identity RPC held the host-wide session lock")
            if acquired:
                host._lock.release()
            release.set()
            worker.join(3)
        host.close()

        self.assertFalse(worker.is_alive())
        self.assertEqual(failure, [])

    def test_valid_page_with_mismatched_api_authority_returns_typed_403(self):
        now = int(time.time() * 1000)
        config = OdooNativeConfig(
            rpc_base_url="http://127.0.0.1:8069",
            public_origin="http://127.0.0.1:18069",
            public_agent_url="http://127.0.0.1:18766/assistant",
            delegation_dir=self.root,
            session_secret_path=self.secret_path,
            state_dir=self.root / "state-mismatch",
            gateway_port=0, bootstrap_port=0, scripted=True)
        host = OdooNativeHost(config)
        event = OdooSessionEvent(
            schema_version=1, event="bootstrap", database="trial",
            company_id="7", actor_id="42", runtime_instance_id="c" * 32,
            issued_at=now, expires_at=now + 30_000, nonce="d" * 32)
        with patch.object(
                host, "_adapter_and_principal",
                side_effect=OdooAuthorityMismatchError("mismatch")):
            with host:
                status, payload = self._post(
                    host.bootstrap_server.base_url + "/bootstrap",
                    sign_session_event(event, SECRET))

        self.assertEqual(status, 403)
        self.assertEqual(payload, {
            "error": "identity_authority_mismatch", "retryable": False})

if __name__ == "__main__":
    unittest.main()
