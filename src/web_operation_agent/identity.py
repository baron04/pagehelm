"""Immutable server-side principal binding for one application session."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass


PRINCIPAL_CONTEXT_VERSION = 1
MAX_PRINCIPAL_LIFETIME_MS = 24 * 60 * 60 * 1000
_APPLICATION_ID = re.compile(r"[a-z][a-z0-9.-]{2,79}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}\Z")
_SCOPE = re.compile(r"[a-z][a-z0-9:._-]{1,99}\Z")


class PrincipalError(ValueError):
    """A principal assertion is malformed, expired, or bound to another application."""


class PrincipalRevokedError(PrincipalError):
    """The application or operator withdrew one issued principal's authority."""


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PrincipalError(f"{name} must be a non-negative integer")
    return value


def _opaque_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise PrincipalError(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class IdentityAssertion:
    """Identity resolved by one trusted application authentication boundary."""

    schema_version: int
    source: str
    application_id: str
    tenant_id: str
    workspace_id: str
    actor_id: str
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.schema_version != PRINCIPAL_CONTEXT_VERSION:
            raise PrincipalError("identity assertion version is unsupported")
        if self.source not in {"page_session", "delegated_api"}:
            raise PrincipalError("identity assertion source is unsupported")
        if not isinstance(self.application_id, str) \
                or not _APPLICATION_ID.fullmatch(self.application_id):
            raise PrincipalError("application_id is invalid")
        for name in ("tenant_id", "workspace_id", "actor_id"):
            _opaque_id(getattr(self, name), name)
        issued_at = _integer(self.issued_at, "issued_at")
        expires_at = _integer(self.expires_at, "expires_at")
        if expires_at <= issued_at or expires_at - issued_at > MAX_PRINCIPAL_LIFETIME_MS:
            raise PrincipalError("identity assertion lifetime is invalid")

    def require_live(self, *, now_ms: int | None = None) -> None:
        now = int(time.time() * 1000) if now_ms is None else _integer(now_ms, "now_ms")
        if now < self.issued_at or now >= self.expires_at:
            raise PrincipalError("identity assertion is not currently valid")

    def audit_projection(self) -> dict:
        def digest_id(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "application_id": self.application_id,
            "tenant_digest": digest_id(self.tenant_id),
            "workspace_digest": digest_id(self.workspace_id),
            "actor_digest": digest_id(self.actor_id),
            "expires_at": self.expires_at,
        }


def bind_matching_identity(page: IdentityAssertion, delegated: IdentityAssertion, *,
                           delegation_id: str, scopes: tuple[str, ...],
                           now_ms: int | None = None) -> "PrincipalContext":
    """Create a principal only when page and API authentication resolve identically."""
    if not isinstance(page, IdentityAssertion) or page.source != "page_session":
        raise PrincipalError("page identity assertion is required")
    if not isinstance(delegated, IdentityAssertion) or delegated.source != "delegated_api":
        raise PrincipalError("delegated API identity assertion is required")
    now = int(time.time() * 1000) if now_ms is None else _integer(now_ms, "now_ms")
    page.require_live(now_ms=now)
    delegated.require_live(now_ms=now)
    identity = ("application_id", "tenant_id", "workspace_id", "actor_id")
    if any(getattr(page, name) != getattr(delegated, name) for name in identity):
        raise PrincipalError("page session and delegated API resolve to different principals")
    return PrincipalContext(
        schema_version=PRINCIPAL_CONTEXT_VERSION,
        application_id=page.application_id,
        tenant_id=page.tenant_id,
        workspace_id=page.workspace_id,
        actor_id=page.actor_id,
        delegation_id=_opaque_id(delegation_id, "delegation_id"),
        issued_at=max(page.issued_at, delegated.issued_at),
        expires_at=min(page.expires_at, delegated.expires_at),
        scopes=scopes,
    )


@dataclass(frozen=True)
class PrincipalContext:
    """A trusted host assertion shared by the page session and delegated API tools.

    The values are identifiers, never credentials. Constructing this object does not prove
    them; an application adapter or authenticated host must resolve them from its own session
    and delegated API before creating a product session.
    """

    schema_version: int
    application_id: str
    tenant_id: str
    workspace_id: str
    actor_id: str
    delegation_id: str
    issued_at: int
    expires_at: int
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PRINCIPAL_CONTEXT_VERSION:
            raise PrincipalError("principal context version is unsupported")
        if not isinstance(self.application_id, str) \
                or not _APPLICATION_ID.fullmatch(self.application_id):
            raise PrincipalError("application_id is invalid")
        for name in ("tenant_id", "workspace_id", "actor_id", "delegation_id"):
            _opaque_id(getattr(self, name), name)
        issued_at = _integer(self.issued_at, "issued_at")
        expires_at = _integer(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise PrincipalError("principal context must expire after it is issued")
        if expires_at - issued_at > MAX_PRINCIPAL_LIFETIME_MS:
            raise PrincipalError("principal context lifetime exceeds 24 hours")
        if (not isinstance(self.scopes, tuple) or not self.scopes
                or len(self.scopes) != len(set(self.scopes))
                or any(not isinstance(scope, str) or not _SCOPE.fullmatch(scope)
                       for scope in self.scopes)):
            raise PrincipalError("principal scopes must be a non-empty unique tuple")
        object.__setattr__(self, "scopes", tuple(sorted(self.scopes)))

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "application_id": self.application_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "actor_id": self.actor_id,
            "delegation_id": self.delegation_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
        }

    @classmethod
    def from_json(cls, value: dict) -> "PrincipalContext":
        fields = {
            "schema_version", "application_id", "tenant_id", "workspace_id",
            "actor_id", "delegation_id", "issued_at", "expires_at", "scopes",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise PrincipalError("principal context fields do not match schema 1")
        if not isinstance(value.get("scopes"), list):
            raise PrincipalError("principal scopes must be an array")
        return cls(**{**value, "scopes": tuple(value["scopes"])})

    @property
    def binding_digest(self) -> str:
        encoded = json.dumps(
            self.to_json(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def require_live(self, *, application_id: str | None = None,
                     now_ms: int | None = None) -> None:
        if application_id is not None and application_id != self.application_id:
            raise PrincipalError("principal belongs to another application")
        now = int(time.time() * 1000) if now_ms is None else _integer(now_ms, "now_ms")
        if now < self.issued_at:
            raise PrincipalError("principal context is not active yet")
        if now >= self.expires_at:
            raise PrincipalError("principal context has expired")

    def audit_projection(self) -> dict:
        def digest_id(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        return {
            "schema_version": self.schema_version,
            "application_id": self.application_id,
            "principal_digest": self.binding_digest,
            "tenant_digest": digest_id(self.tenant_id),
            "workspace_digest": digest_id(self.workspace_id),
            "actor_digest": digest_id(self.actor_id),
            "delegation_digest": digest_id(self.delegation_id),
            "scopes": list(self.scopes),
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class OperationContext:
    """One principal-bound operation inside an authenticated product turn.

    Raw tenant, workspace, actor, and delegation identifiers remain available only to
    trusted server components through ``principal``. Browser messages and durable audit
    records use the canonical digests returned by :meth:`audit_projection`.
    """

    principal: PrincipalContext
    session_id: str
    turn_id: str
    operation_id: str
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.principal, PrincipalContext):
            raise PrincipalError("operation principal is invalid")
        for name in ("session_id", "turn_id", "operation_id"):
            _opaque_id(getattr(self, name), name)
        issued_at = _integer(self.issued_at, "issued_at")
        expires_at = _integer(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise PrincipalError("operation context must expire after it is issued")
        if issued_at < self.principal.issued_at or expires_at > self.principal.expires_at:
            raise PrincipalError("operation lifetime must be contained by its principal")

    @classmethod
    def create(cls, principal: PrincipalContext, *, session_id: str, turn_id: str,
               operation_id: str, deadline_ms: int, now_ms: int | None = None
               ) -> "OperationContext":
        now = int(time.time() * 1000) if now_ms is None else _integer(now_ms, "now_ms")
        deadline = _integer(deadline_ms, "deadline_ms")
        principal.require_live(now_ms=now)
        expires_at = min(deadline, principal.expires_at)
        return cls(principal, session_id, turn_id, operation_id, now, expires_at)

    @property
    def binding_digest(self) -> str:
        encoded = json.dumps({
            "schema_version": 1,
            "principal_digest": self.principal.binding_digest,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "operation_id": self.operation_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def require_live(self, *, application_id: str | None = None,
                     required_scope: str | None = None,
                     now_ms: int | None = None) -> None:
        now = int(time.time() * 1000) if now_ms is None else _integer(now_ms, "now_ms")
        if now < self.issued_at or now >= self.expires_at:
            raise PrincipalError("operation context has expired")
        self.principal.require_live(application_id=application_id, now_ms=now)
        if required_scope is not None and required_scope not in self.principal.scopes:
            raise PrincipalError("principal lacks the required operation scope")

    def audit_projection(self) -> dict:
        def digest_id(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        return {
            "schema_version": 1,
            "application_id": self.principal.application_id,
            "principal_digest": self.principal.binding_digest,
            "operation_context_digest": self.binding_digest,
            "session_digest": digest_id(self.session_id),
            "turn_digest": digest_id(self.turn_id),
            "operation_digest": digest_id(self.operation_id),
            "scopes": list(self.principal.scopes),
            "expires_at": self.expires_at,
        }
