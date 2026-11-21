"""Fail-closed server-side identity resolution for declarative PageHelm integrations."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import threading
import time
from typing import Callable
import urllib.error
import urllib.request

from web_operation_agent.business.errors import BusinessToolIdentityError, BusinessToolPermissionError
from web_operation_agent.state.identity import (
    PRINCIPAL_CONTEXT_VERSION,
    IdentityAssertion,
    OperationContext,
    PrincipalContext,
    PrincipalError,
    bind_matching_identity,
)

_MAX_CACHE_ENTRIES = 128


@dataclass(frozen=True, slots=True)
class ResolvedActor:
    """Server-side identity & permission resolution returned by an ``IdentityResolver``."""

    actor_id: str
    permissions: frozenset[str] = frozenset()
    tenant_id: str = "default"
    workspace_id: str = "default"

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise PrincipalError("ResolvedActor.actor_id must be a non-empty string")
        perms = frozenset() if self.permissions is None else frozenset(self.permissions)
        object.__setattr__(self, "permissions", perms)


IdentityResolver = Callable[[str], ResolvedActor]


class DeclarativeIdentityPort:
    """Fail-closed identity port backed by host-supplied ``IdentityResolver`` callbacks."""

    required = True

    def __init__(
        self,
        *,
        application_id: str,
        page_resolver: IdentityResolver | None,
        delegated_resolver: Callable[[], ResolvedActor] | None = None,
        has_delegated_credential: bool = False,
        same_origin_shared_session: bool = False,
        required_scopes: frozenset[str] = frozenset(),
        lifetime_s: int = 300,
    ):
        if page_resolver is None:
            raise ValueError("page_resolver is required")
        if has_delegated_credential and delegated_resolver is None:
            raise ValueError(
                "an independent api_token or headers_factory requires a real delegated_resolver"
            )
        if delegated_resolver is None and not same_origin_shared_session:
            raise ValueError(
                "delegated_resolver is required unless same_origin_shared_session=True "
                "with no independent api_token"
            )
        if same_origin_shared_session and has_delegated_credential:
            raise ValueError(
                "same_origin_shared_session=True cannot be combined with an independent api_token"
            )
        self.application_id = application_id
        self._page_resolver = page_resolver
        self._delegated_resolver = delegated_resolver
        self._same_origin_shared_session = bool(same_origin_shared_session)
        self.required_scopes = frozenset(required_scopes)
        self.lifetime_s = lifetime_s
        self._lock = threading.Lock()
        self._page_assertions: OrderedDict[IdentityAssertion, tuple[str, frozenset[str]]] = OrderedDict()
        self._bound_sessions: OrderedDict[str, tuple[str, int]] = OrderedDict()

    def _to_assertion(
        self,
        actor: ResolvedActor,
        source: str,
    ) -> tuple[IdentityAssertion, frozenset[str]]:
        issued = int(time.time() * 1000)
        perms = frozenset() if actor.permissions is None else frozenset(actor.permissions)
        return (
            IdentityAssertion(
                schema_version=PRINCIPAL_CONTEXT_VERSION,
                source=source,
                application_id=self.application_id,
                tenant_id=str(actor.tenant_id),
                workspace_id=str(actor.workspace_id),
                actor_id=str(actor.actor_id),
                issued_at=issued,
                expires_at=issued + self.lifetime_s * 1000,
            ),
            perms,
        )

    def _prune_caches_locked(self, now_ms: int) -> None:
        expired_assertions = [k for k in self._page_assertions if k.expires_at <= now_ms]
        for k in expired_assertions:
            self._page_assertions.pop(k, None)
        while len(self._page_assertions) >= _MAX_CACHE_ENTRIES:
            self._page_assertions.popitem(last=False)

        expired_sessions = [k for k, (_, exp) in self._bound_sessions.items() if exp <= now_ms]
        for k in expired_sessions:
            self._bound_sessions.pop(k, None)
        while len(self._bound_sessions) >= _MAX_CACHE_ENTRIES:
            self._bound_sessions.popitem(last=False)

    def page_identity(self, session_credential: str) -> IdentityAssertion:
        resolved = self._page_resolver(session_credential)
        assertion, perms = self._to_assertion(resolved, "page_session")
        with self._lock:
            self._prune_caches_locked(int(time.time() * 1000))
            self._page_assertions[assertion] = (session_credential, perms)
            self._page_assertions.move_to_end(assertion)
        return assertion

    def resolve_delegated(self) -> tuple[IdentityAssertion, frozenset[str]] | None:
        if self._delegated_resolver is None:
            return None
        resolved = self._delegated_resolver()
        return self._to_assertion(resolved, "delegated_api")

    def bind(
        self,
        page_identity: IdentityAssertion,
        *,
        delegation_id: str,
        lifetime_s: int = 300,
        now_ms: int | None = None,
    ) -> PrincipalContext:
        if not isinstance(page_identity, IdentityAssertion):
            raise PrincipalError("page identity assertion is required")
        with self._lock:
            cached = self._page_assertions.pop(page_identity, None)
        delegated_info = self.resolve_delegated()
        same_origin_cred: str | None = None
        if delegated_info is not None:
            delegated, permissions = delegated_info
            if cached is not None:
                permissions = permissions & cached[1]
        else:
            if not self._same_origin_shared_session or cached is None:
                raise PrincipalError("unrecognized or already-consumed page identity assertion")
            same_origin_cred, permissions = cached
            delegated = IdentityAssertion(
                schema_version=page_identity.schema_version,
                source="delegated_api",
                application_id=page_identity.application_id,
                tenant_id=page_identity.tenant_id,
                workspace_id=page_identity.workspace_id,
                actor_id=page_identity.actor_id,
                issued_at=page_identity.issued_at,
                expires_at=page_identity.expires_at,
            )
        granted = self.required_scopes & permissions
        if not granted:
            raise PrincipalError("the actor holds none of the scopes this integration uses")
        principal = bind_matching_identity(
            page_identity,
            delegated,
            delegation_id=delegation_id,
            scopes=tuple(sorted(granted)),
            now_ms=now_ms,
        )
        if same_origin_cred is not None:
            with self._lock:
                self._prune_caches_locked(int(time.time() * 1000))
                self._bound_sessions[principal.binding_digest] = (same_origin_cred, principal.expires_at)
                self._bound_sessions.move_to_end(principal.binding_digest)
        return principal

    def validate(self, principal: PrincipalContext | None, *, application_id: str) -> None:
        if principal is None:
            raise PrincipalError("this application requires a verified principal")
        principal.require_live(application_id=application_id)

    def _resolve_live_binding(self, principal: PrincipalContext) -> tuple[str, str, str, frozenset[str]]:
        delegated_info = self.resolve_delegated()
        if delegated_info is not None:
            assertion, permissions = delegated_info
            return assertion.actor_id, assertion.workspace_id, assertion.tenant_id, permissions
        with self._lock:
            entry = self._bound_sessions.get(principal.binding_digest)
        if entry is None:
            raise PrincipalError("session credential is no longer bound")
        cred, exp_ms = entry
        if int(time.time() * 1000) >= exp_ms:
            raise PrincipalError("bound session credential has expired")
        resolved = self._page_resolver(cred)
        return (
            str(resolved.actor_id),
            str(resolved.workspace_id),
            str(resolved.tenant_id),
            resolved.permissions,
        )

    def verify_live_actor(
        self,
        context: OperationContext | None,
        *,
        required_scope: str = "",
        mismatch_error_cls: type[Exception] = PrincipalError,
        mismatch_message: str = "actor differs from live session",
    ) -> frozenset[str]:
        if context is None:
            raise mismatch_error_cls("an actor-bound operation context is required")
        context.require_live(
            application_id=self.application_id,
            required_scope=required_scope or None,
        )
        try:
            actor_id, workspace_id, tenant_id, permissions = self._resolve_live_binding(context.principal)
        except PrincipalError as exc:
            raise mismatch_error_cls(str(exc)) from exc
        principal = context.principal
        if (
            actor_id != principal.actor_id
            or workspace_id != principal.workspace_id
            or tenant_id != principal.tenant_id
        ):
            raise mismatch_error_cls(mismatch_message)
        if required_scope and required_scope not in permissions:
            raise mismatch_error_cls(mismatch_message)
        return permissions

    def verify_actor_for_write(self, context: OperationContext | None, *, required_scope: str) -> str:
        if context is None:
            raise BusinessToolIdentityError("writes require an actor-bound context")
        context.require_live(application_id=self.application_id, required_scope=required_scope)
        try:
            actor_id, workspace_id, tenant_id, permissions = self._resolve_live_binding(context.principal)
        except PrincipalError as exc:
            raise BusinessToolIdentityError(str(exc)) from exc
        principal = context.principal
        if (
            actor_id != principal.actor_id
            or workspace_id != principal.workspace_id
            or tenant_id != principal.tenant_id
        ):
            raise BusinessToolIdentityError("delegated API token no longer matches bound actor")
        if required_scope not in permissions:
            raise BusinessToolPermissionError(f"actor lacks {required_scope} permission")
        return principal.actor_id


class HttpWhoamiIdentityPort(DeclarativeIdentityPort):
    """Dual-credential identity port resolving Cookie and Bearer token via an HTTP endpoint."""

    def __init__(
        self,
        *,
        origin: str,
        api_token: str = "",
        whoami_path: str = "/api/whoami",
        default_tenant_id: str = "default",
        endpoint_label: str = "application",
        **kwargs,
    ):
        self.origin = origin.rstrip("/")
        self._api_token = api_token
        self._whoami_path = whoami_path
        self._default_tenant_id = default_tenant_id
        self._endpoint_label = endpoint_label
        kwargs.pop("page_resolver", None)
        kwargs.pop("delegated_resolver", None)
        kwargs.pop("has_delegated_credential", None)
        kwargs.pop("same_origin_shared_session", None)
        super().__init__(
            page_resolver=lambda cred: self._resolve_whoami({"Cookie": cred}, "page_session"),
            delegated_resolver=lambda: self._resolve_whoami(
                {"Authorization": f"Bearer {self._api_token}"}, "delegated_api"
            ),
            has_delegated_credential=bool(api_token),
            same_origin_shared_session=False,
            **kwargs,
        )

    def _whoami(self, headers: dict[str, str], expected_source: str) -> tuple[IdentityAssertion, frozenset[str]]:
        request = urllib.request.Request(self.origin + self._whoami_path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise PrincipalError(f"the {self._endpoint_label} identity endpoint is unavailable") from exc
        if not isinstance(payload, dict) or payload.get("source") != expected_source:
            raise PrincipalError(f"the {self._endpoint_label} identity answer is not the expected side")
        permissions = payload.get("permissions")
        if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
            raise PrincipalError(f"the {self._endpoint_label} identity answer carries no permissions")
        issued = int(time.time() * 1000)
        tenant_id = str(payload.get("tenant_id") or self._default_tenant_id)
        workspace_id = str(payload.get("workspace") or payload.get("workspace_id") or "default")
        actor_id = str(payload.get("actor") or payload.get("actor_id") or "")
        return (
            IdentityAssertion(
                schema_version=PRINCIPAL_CONTEXT_VERSION,
                source=expected_source,
                application_id=self.application_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                issued_at=issued,
                expires_at=issued + self.lifetime_s * 1000,
            ),
            frozenset(permissions),
        )

    def _resolve_whoami(self, headers: dict[str, str], expected_source: str) -> ResolvedActor:
        assertion, permissions = self._whoami(headers, expected_source)
        return ResolvedActor(
            actor_id=assertion.actor_id,
            permissions=permissions,
            tenant_id=assertion.tenant_id,
            workspace_id=assertion.workspace_id,
        )


__all__ = [
    "DeclarativeIdentityPort",
    "HttpWhoamiIdentityPort",
    "IdentityResolver",
    "ResolvedActor",
]
