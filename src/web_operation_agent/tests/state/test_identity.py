from __future__ import annotations

import unittest

from web_operation_agent.state.identity import (MAX_PRINCIPAL_LIFETIME_MS,
                                          IdentityAssertion, OperationContext,
                                          PrincipalContext, PrincipalError,
                                          bind_matching_identity)
from web_operation_agent.tests.support.identity import principal


class PrincipalContextTests(unittest.TestCase):
    def test_strict_roundtrip_digest_and_redacted_audit_projection(self):
        value = principal()
        restored = PrincipalContext.from_json(value.to_json())

        self.assertEqual(restored, value)
        self.assertEqual(restored.binding_digest, value.binding_digest)
        audit = value.audit_projection()
        self.assertEqual(audit["application_id"], "sample")
        self.assertEqual(audit["principal_digest"], value.binding_digest)
        for raw in (value.tenant_id, value.workspace_id, value.actor_id,
                    value.delegation_id):
            self.assertNotIn(raw, repr(audit))

    def test_scope_order_is_canonical_and_unknown_fields_are_rejected(self):
        value = principal()
        reversed_scopes = PrincipalContext(
            **{**value.to_json(), "scopes": tuple(reversed(value.scopes))})
        self.assertEqual(reversed_scopes.binding_digest, value.binding_digest)
        with self.assertRaisesRegex(PrincipalError, "fields"):
            PrincipalContext.from_json({**value.to_json(), "credential": "secret"})

    def test_actor_authority_digest_is_stable_across_fresh_delegations(self):
        first = principal()
        second = PrincipalContext(
            schema_version=1,
            application_id=first.application_id,
            tenant_id=first.tenant_id,
            workspace_id="workspace-2",
            actor_id=first.actor_id,
            delegation_id="delegation-2",
            issued_at=first.issued_at + 1,
            expires_at=first.expires_at + 1,
            scopes=("companies:read",),
        )
        other_actor = PrincipalContext.from_json(
            {**second.to_json(), "actor_id": "actor-2"})

        self.assertNotEqual(first.binding_digest, second.binding_digest)
        self.assertEqual(first.actor_authority_digest,
                         second.actor_authority_digest)
        self.assertNotEqual(second.actor_authority_digest,
                            other_actor.actor_authority_digest)

    def test_lifetime_identity_and_scope_contracts_fail_closed(self):
        base = principal().to_json()
        invalid = (
            {**base, "schema_version": 2},
            {**base, "application_id": "Fixture CRM"},
            {**base, "actor_id": "actor/../../other"},
            {**base, "scopes": []},
            {**base, "scopes": ["page:operate", "page:operate"]},
            {**base, "expires_at": base["issued_at"]},
            {**base, "expires_at": base["issued_at"] + MAX_PRINCIPAL_LIFETIME_MS + 1},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(PrincipalError):
                PrincipalContext.from_json(payload)

    def test_live_check_rejects_future_expired_and_other_application(self):
        value = principal()
        value.require_live(application_id="sample", now_ms=1_000_000)
        with self.assertRaisesRegex(PrincipalError, "not active"):
            value.require_live(now_ms=999_999)
        with self.assertRaisesRegex(PrincipalError, "expired"):
            value.require_live(now_ms=1_060_000)
        with self.assertRaisesRegex(PrincipalError, "another application"):
            value.require_live(application_id="ledger-demo", now_ms=1_000_000)


class OperationContextTests(unittest.TestCase):
    def test_operation_is_canonical_bounded_and_redacted(self):
        actor = principal()
        context = OperationContext.create(
            actor, session_id="session-1", turn_id="message-1",
            operation_id="op-query-1", deadline_ms=1_030_000,
            now_ms=1_000_000)

        context.require_live(
            application_id="sample", required_scope="companies:read",
            now_ms=1_000_001)
        self.assertEqual(context.expires_at, 1_030_000)
        audit = context.audit_projection()
        self.assertEqual(audit["principal_digest"], actor.binding_digest)
        self.assertEqual(audit["operation_context_digest"], context.binding_digest)
        for raw in (actor.tenant_id, actor.workspace_id, actor.actor_id,
                    actor.delegation_id, context.session_id, context.turn_id,
                    context.operation_id):
            self.assertNotIn(raw, repr(audit))

    def test_operation_rejects_scope_expiry_and_lifetime_escape(self):
        actor = principal()
        context = OperationContext.create(
            actor, session_id="session-1", turn_id="message-1",
            operation_id="op-query-1", deadline_ms=2_000_000,
            now_ms=1_000_000)
        self.assertEqual(context.expires_at, actor.expires_at)
        with self.assertRaisesRegex(PrincipalError, "required operation scope"):
            context.require_live(required_scope="companies:write", now_ms=1_000_001)
        with self.assertRaisesRegex(PrincipalError, "operation context has expired"):
            context.require_live(now_ms=actor.expires_at)
        with self.assertRaisesRegex(PrincipalError, "contained"):
            OperationContext(
                actor, "session-1", "message-1", "op-query-1",
                actor.issued_at, actor.expires_at + 1)


class IdentityBindingTests(unittest.TestCase):
    @staticmethod
    def assertion(source: str, *, actor_id: str = "actor-1",
                  workspace_id: str = "workspace-1") -> IdentityAssertion:
        return IdentityAssertion(
            1, source, "sample", workspace_id, workspace_id, actor_id,
            1_000_000, 1_060_000)

    def test_matching_page_and_api_identity_create_one_principal(self):
        page = self.assertion("page_session")
        delegated = self.assertion("delegated_api")
        value = bind_matching_identity(
            page, delegated, delegation_id="oauth-grant-1",
            scopes=("companies:read",), now_ms=1_000_001)

        self.assertEqual(value.actor_id, "actor-1")
        self.assertEqual(value.workspace_id, "workspace-1")
        self.assertNotIn("actor-1", repr(page.audit_projection()))

    def test_actor_or_workspace_mismatch_fails_closed(self):
        page = self.assertion("page_session")
        for delegated in (
            self.assertion("delegated_api", actor_id="actor-2"),
            self.assertion("delegated_api", workspace_id="workspace-2"),
        ):
            with self.subTest(delegated=delegated), self.assertRaisesRegex(
                    PrincipalError, "different principals"):
                bind_matching_identity(
                    page, delegated, delegation_id="oauth-grant-1",
                    scopes=("companies:read",), now_ms=1_000_001)


if __name__ == "__main__":
    unittest.main()
