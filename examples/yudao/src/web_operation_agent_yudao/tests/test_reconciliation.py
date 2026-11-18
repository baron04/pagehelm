"""Tests for same-actor read-only YudaoMutationReconciler (OPS-03)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.business.operations import reconcile
from web_operation_agent.composition import bind_application
from web_operation_agent.composition.ports import ApplicationMutationReconciler
from web_operation_agent.state.identity import IdentityAssertion, PRINCIPAL_CONTEXT_VERSION, PrincipalError
from web_operation_agent_yudao.application import APPLICATION_ID, YudaoApplication
from web_operation_agent_yudao.client import YudaoClient, YudaoReadRefused


class _FakeYudaoClient(YudaoClient):
    def __init__(self, *, actor_id: str = "1", tenant: str = "1"):
        super().__init__("http://yudao.invalid", None, token="synthetic-token", tenant=tenant)
        self.actor_id = actor_id
        self.records: dict[tuple[str, str], dict] = {}
        self.errors: dict[str, Exception] = {}
        self.http_calls: list[tuple[str, str, dict]] = []

    def _request_transport(self, method: str, path: str, *, params=None, body=None, token=None):
        self.http_calls.append((method, path, dict(params or {})))
        if method != "GET":
            raise AssertionError(f"reconciliation must never issue {method} {path}")
        if path in self.errors:
            raise self.errors[path]
        if path == "system/auth/get-permission-info":
            return 0, {
                "code": 0,
                "data": {
                    "user": {"id": int(self.actor_id), "deptId": 10, "nickname": "测试账号"},
                    "roles": ["crm_admin"],
                    "permissions": [],
                    "menus": [],
                },
            }
        if path == "system/dict-data/simple-list":
            return 0, {"code": 0, "data": [{"dictType": "crm_follow_up_type", "label": "电话", "value": "1"}]}
        if path.endswith("/get"):
            entity = path[: -len("/get")]
            record_id = str((params or {}).get("id") or "")
            record = self.records.get((entity, record_id))
            if record is None:
                return 1001, {"code": 1001, "msg": "记录不存在"}
            return 0, {"code": 0, "data": dict(record)}
        return 0, {"code": 0, "data": []}

    def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = ""):
        raise AssertionError(f"reconciliation must never re-dispatch writes: {method} {path}")


class YudaoMutationReconcilerTests(unittest.TestCase):
    def _principal(self, app: YudaoApplication, *, actor_id: str = "1", scopes=None, expired: bool = False):
        now_ms = int(time.time() * 1000)
        assertion = IdentityAssertion(
            schema_version=PRINCIPAL_CONTEXT_VERSION,
            source="page_session",
            application_id=APPLICATION_ID,
            tenant_id="1",
            workspace_id="10",
            actor_id=actor_id,
            issued_at=now_ms - 120_000,
            expires_at=now_ms + 60_000,
        )
        p = app.identity.bind(assertion, delegation_id="reconcile-test")
        if expired:
            p = replace(p, issued_at=now_ms - 60_000, expires_at=now_ms - 10_000)
        if scopes is not None:
            p = replace(p, scopes=tuple(scopes))
        return p

    def _app(self, client: _FakeYudaoClient, *, journal=None, writing_gets=frozenset()):
        return YudaoApplication(
            "https://yudao.invalid",
            None,
            {"paths": {}, "components": {}},
            api_base_url="http://yudao.invalid",
            client=client,
            conventions=None,
            native_writes=True,
            mutation_journal=journal,
            writing_gets=writing_gets,
        )

    def test_reconciler_implements_protocol_and_resolves_completed_failed_unknown(self):
        client = _FakeYudaoClient()
        app = self._app(client)
        reconciler = app.ports.reconciliation
        self.assertIsInstance(reconciler, ApplicationMutationReconciler)
        principal = self._principal(app)

        # 1. completed: readback matches all expected fields
        client.records[("crm/customer", "42")] = {"id": 42, "name": "新名称", "remark": "新备注"}
        res_completed = reconciler.reconcile(
            "crm_customer_update",
            {"entity": "crm/customer", "id": 42, "expected": {"name": "新名称", "remark": "新备注"}},
            evidence={"result_digest": ""},
            principal=principal,
        )
        self.assertEqual(res_completed.outcome, "completed")
        self.assertFalse(res_completed.requires_human)

        # 2. failed: readback shows pre-mutation values remain
        client.records[("crm/customer", "42")] = {"id": 42, "name": "旧名称", "remark": "旧备注"}
        res_failed = reconciler.reconcile(
            "crm_customer_update",
            {
                "entity": "crm/customer",
                "id": 42,
                "expected": {"name": "新名称"},
                "before": {"name": "旧名称"},
            },
            evidence={"result_digest": ""},
            principal=principal,
        )
        self.assertEqual(res_failed.outcome, "failed")
        self.assertFalse(res_failed.requires_human)

        # 3. failed: create with confirmed id where record does not exist
        res_create_missing = reconciler.reconcile(
            "crm_customer_create",
            {"entity": "crm/customer", "id": 999, "expected": {"name": "不存在"}},
            evidence={},
            principal=principal,
        )
        self.assertEqual(res_create_missing.outcome, "failed")
        self.assertFalse(res_create_missing.requires_human)

        # 4. unknown: create without confirmed record id
        res_create_no_id = reconciler.reconcile(
            "crm_customer_create",
            {"entity": "crm/customer", "id": None, "expected": {"name": "未知"}},
            evidence={},
            principal=principal,
        )
        self.assertEqual(res_create_no_id.outcome, "unknown")
        self.assertTrue(res_create_no_id.requires_human)

        # 5. unknown: transient readback 5xx error
        client.errors["erp/sale-order/get"] = YudaoReadRefused("upstream down", code=503, reason="503")
        res_503 = reconciler.reconcile(
            "erp_document_status",
            {"entity": "erp/sale-order", "id": 7, "expected": {"status": 20}},
            evidence={},
            principal=principal,
        )
        self.assertEqual(res_503.outcome, "unknown")
        self.assertTrue(res_503.requires_human)

        # 6. unknown: partial effect flag requires human review
        res_partial = reconciler.reconcile(
            "crm_customer_update",
            {"entity": "crm/customer", "id": 42, "expected": {"name": "旧名称"}},
            evidence={"unverified_effect": "unrelated_field_preservation"},
            principal=principal,
        )
        self.assertEqual(res_partial.outcome, "unknown")
        self.assertTrue(res_partial.requires_human)

        # Strictly GET-only; never dispatches writes
        self.assertTrue(all(method == "GET" for method, _path, _params in client.http_calls))

    def test_fenced_get_and_auth_enforcement(self):
        client = _FakeYudaoClient()
        app = self._app(client, writing_gets=frozenset({"/admin-api/crm/customer/get"}))
        reconciler = app.ports.reconciliation
        principal = self._principal(app)

        # Fenced GET endpoint must not be called; returns unknown + requires_human.
        calls_before = len(client.http_calls)
        res_fenced = reconciler.reconcile(
            "crm_customer_update",
            {"entity": "crm/customer", "id": 42, "expected": {"name": "新名称"}},
            evidence={},
            principal=principal,
        )
        self.assertEqual(res_fenced.outcome, "unknown")
        self.assertTrue(res_fenced.requires_human)
        self.assertIn("fence", res_fenced.reason)
        self.assertFalse(any(path == "crm/customer/get" for _, path, _ in client.http_calls[calls_before:]))

        # Missing/expired/wrong actor/missing scope/403 readback raise PrincipalError
        with self.assertRaises(PrincipalError):
            reconciler.reconcile("crm_customer_update", {"entity": "crm/customer", "id": 42}, evidence={}, principal=None)
        expired_principal = self._principal(app, expired=True)
        with self.assertRaises(PrincipalError):
            reconciler.reconcile(
                "crm_customer_update",
                {"entity": "crm/customer", "id": 42},
                evidence={},
                principal=expired_principal,
            )
        no_scope_principal = self._principal(app, scopes=("yudao.other",))
        with self.assertRaises(PrincipalError):
            reconciler.reconcile(
                "crm_customer_update",
                {"entity": "crm/customer", "id": 42},
                evidence={},
                principal=no_scope_principal,
            )
        client_other = _FakeYudaoClient(actor_id="99")
        app_other = self._app(client_other)
        with self.assertRaises(PrincipalError):
            app_other.ports.reconciliation.reconcile(
                "crm_customer_update",
                {"entity": "crm/customer", "id": 42},
                evidence={},
                principal=principal,
            )

        client_403 = _FakeYudaoClient()
        client_403.errors["crm/customer/get"] = YudaoReadRefused("denied", code=403, reason="forbidden")
        app_403 = self._app(client_403)
        with self.assertRaises(PrincipalError):
            app_403.ports.reconciliation.reconcile(
                "crm_customer_update",
                {"entity": "crm/customer", "id": 42, "expected": {"name": "x"}},
                evidence={},
                principal=self._principal(app_403),
            )

    def test_end_to_end_reconcile_with_sqlite_mutation_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteMutationJournal(Path(tmp) / "mutations.sqlite3")
            client = _FakeYudaoClient()
            client.records[("erp/sale-order", "15")] = {"id": 15, "status": 20}
            app = self._app(client, journal=journal)
            bound = bind_application(app)
            principal = self._principal(app)
            identity = {
                k: v for k, v in principal.audit_projection().items()
                if k in {"tenant_digest", "workspace_digest", "actor_digest"}
            }
            digest = "a" * 64
            journal.register(
                digest,
                "b" * 64,
                expires_at=int(time.time() * 1000) + 60_000,
                reconciliation={
                    "application_id": APPLICATION_ID,
                    "tool": "erp_document_status",
                    "args": {
                        "entity": "erp/sale-order",
                        "id": 15,
                        "operation": "erp_document_status",
                        "expected": {"status": 20},
                        "before": {"status": 10},
                    },
                    "identity": identity,
                },
            )
            journal.claim(digest, "b" * 64)
            journal.finish(digest, "indeterminate", reconciliation={"phase": "dispatch"})
            report = reconcile(journal, digest, application=bound, principal=principal)
            self.assertEqual(report["outcome"], "completed")
            self.assertFalse(report["requires_human"])
            self.assertEqual(report["dispatches"], 0)


if __name__ == "__main__":
    unittest.main()
