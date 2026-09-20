"""Trusted Odoo-session handoff for an ordinary browser deployment.

The browser is only a courier.  Odoo resolves its own HttpOnly session, signs the bounded
identifiers below, and the Agent verifies that signature before it constructs a
``page_session`` assertion.  No cookie, API key, raw user name, or permission list crosses
this protocol.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from web_operation_agent.state.identity import IdentityAssertion, PrincipalError


APPLICATION_ID = "odoo"
SESSION_EVENT_VERSION = 1
MAX_EVENT_BYTES = 4096
MAX_EVENT_LIFETIME_MS = 60_000
MAX_CLOCK_SKEW_MS = 5_000
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}\Z")
_INSTANCE = re.compile(r"[a-f0-9]{32}\Z")
_NONCE = re.compile(r"[a-f0-9]{32,64}\Z")
_SIGNATURE = re.compile(r"[a-f0-9]{64}\Z")


class OdooSessionEventError(PrincipalError):
    """An Odoo session event is malformed, forged, expired, or replayed."""


def _opaque(value: object, name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise OdooSessionEventError(f"{name} is invalid")
    return value


def _millis(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OdooSessionEventError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class OdooSessionEvent:
    """One short-lived statement made by Odoo's authenticated server boundary."""

    schema_version: int
    event: str
    database: str
    company_id: str
    actor_id: str
    runtime_instance_id: str
    issued_at: int
    expires_at: int
    nonce: str

    def __post_init__(self) -> None:
        if self.schema_version != SESSION_EVENT_VERSION:
            raise OdooSessionEventError("session event version is unsupported")
        if self.event not in {"bootstrap", "revoke"}:
            raise OdooSessionEventError("session event kind is unsupported")
        for name in ("database", "company_id", "actor_id"):
            _opaque(getattr(self, name), name)
        if self.event == "bootstrap":
            if not isinstance(self.runtime_instance_id, str) \
                    or not _INSTANCE.fullmatch(self.runtime_instance_id):
                raise OdooSessionEventError("runtime_instance_id is invalid")
        elif self.runtime_instance_id != "":
            raise OdooSessionEventError("a revoke event must cover all actor sessions")
        issued = _millis(self.issued_at, "issued_at")
        expires = _millis(self.expires_at, "expires_at")
        if expires <= issued or expires - issued > MAX_EVENT_LIFETIME_MS:
            raise OdooSessionEventError("session event lifetime is invalid")
        if not isinstance(self.nonce, str) or not _NONCE.fullmatch(self.nonce):
            raise OdooSessionEventError("session event nonce is invalid")

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "database": self.database,
            "company_id": self.company_id,
            "actor_id": self.actor_id,
            "runtime_instance_id": self.runtime_instance_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @classmethod
    def from_json(cls, value: object) -> "OdooSessionEvent":
        fields = {
            "schema_version", "event", "database", "company_id", "actor_id",
            "runtime_instance_id", "issued_at", "expires_at", "nonce",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise OdooSessionEventError("session event fields do not match schema 1")
        return cls(**value)

    def page_identity(self, *, lifetime_s: int = 300) -> IdentityAssertion:
        if self.event != "bootstrap":
            raise OdooSessionEventError("only a bootstrap event establishes page identity")
        if (isinstance(lifetime_s, bool) or not isinstance(lifetime_s, int)
                or not 1 <= lifetime_s <= 3600):
            raise OdooSessionEventError("page identity lifetime must be from 1 to 3600 seconds")
        return IdentityAssertion(
            schema_version=1,
            source="page_session",
            application_id=APPLICATION_ID,
            tenant_id=self.database,
            workspace_id=self.company_id,
            actor_id=self.actor_id,
            issued_at=self.issued_at,
            expires_at=self.issued_at + lifetime_s * 1000,
        )


def canonical_event(event: OdooSessionEvent) -> bytes:
    return json.dumps(
        event.to_json(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sign_session_event(event: OdooSessionEvent, secret: bytes) -> dict:
    """Test/provisioning helper matching the independent Odoo addon producer."""
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("session signing secret must contain at least 32 bytes")
    signature = hmac.new(secret, canonical_event(event), hashlib.sha256).hexdigest()
    return {"event": event.to_json(), "signature": signature}


def load_session_secret(path: str | Path) -> bytes:
    """Load a root-owned style secret without accepting group/world-readable files."""
    target = Path(path)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        secret = target.read_bytes()
    except OSError as exc:
        raise OdooSessionEventError("session signing secret is unreadable") from exc
    if os.name == "posix" and mode & 0o077:
        raise OdooSessionEventError("session signing secret must have mode 0600 or stricter")
    if len(secret) < 32 or len(secret) > 4096:
        raise OdooSessionEventError(
            "session signing secret must contain between 32 and 4096 bytes")
    return secret


class SignedOdooSessionVerifier:
    """Verify freshness and consume every signed event nonce exactly once."""

    def __init__(self, secret: bytes, *, max_clock_skew_ms: int = MAX_CLOCK_SKEW_MS,
                 nonce_consumer: Callable[[str, int, int], bool] | None = None):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("session signing secret must contain at least 32 bytes")
        if (isinstance(max_clock_skew_ms, bool) or not isinstance(max_clock_skew_ms, int)
                or not 0 <= max_clock_skew_ms <= 60_000):
            raise ValueError("max_clock_skew_ms must be an integer from 0 to 60000")
        self._secret = secret
        self._max_clock_skew_ms = max_clock_skew_ms
        if nonce_consumer is not None and not callable(nonce_consumer):
            raise TypeError("nonce_consumer must be callable or None")
        self._nonce_consumer = nonce_consumer
        self._consumed: dict[str, int] = {}
        self._lock = threading.Lock()

    def verify(self, envelope: object, *, now_ms: int | None = None) -> OdooSessionEvent:
        if not isinstance(envelope, dict) or set(envelope) != {"event", "signature"}:
            raise OdooSessionEventError("signed session envelope is malformed")
        event = OdooSessionEvent.from_json(envelope["event"])
        signature = envelope["signature"]
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            raise OdooSessionEventError("session event signature is invalid")
        expected = hmac.new(
            self._secret, canonical_event(event), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise OdooSessionEventError("session event signature did not verify")
        now = int(time.time() * 1000) if now_ms is None else _millis(now_ms, "now_ms")
        if now + self._max_clock_skew_ms < event.issued_at or now >= event.expires_at:
            raise OdooSessionEventError("session event is not currently valid")
        if self._nonce_consumer is not None:
            try:
                consumed = self._nonce_consumer(event.nonce, event.expires_at, now)
            except Exception as exc:
                raise OdooSessionEventError("session event nonce could not be persisted") from exc
            if not consumed:
                raise OdooSessionEventError("session event nonce was already consumed")
        else:
            with self._lock:
                self._consumed = {
                    nonce: expiry for nonce, expiry in self._consumed.items()
                    if expiry > now
                }
                if event.nonce in self._consumed:
                    raise OdooSessionEventError("session event nonce was already consumed")
                self._consumed[event.nonce] = event.expires_at
        return event


@dataclass(frozen=True, slots=True)
class OdooDelegation:
    api_key_path: Path
    scopes: tuple[str, ...]
    allow_mutations: bool


class OdooDelegationDirectory:
    """Resolve a signed actor to that actor's own server-side API key and grant."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @staticmethod
    def _private_file(path: Path, label: str) -> bytes:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            body = path.read_bytes()
        except OSError as exc:
            raise OdooSessionEventError(f"{label} is unreadable") from exc
        if os.name == "posix" and mode & 0o077:
            raise OdooSessionEventError(f"{label} must have mode 0600 or stricter")
        return body

    def resolve(self, event: OdooSessionEvent) -> OdooDelegation:
        if not isinstance(event, OdooSessionEvent):
            raise TypeError("event must be an OdooSessionEvent")
        manifest_path = self.root / "delegations.json"
        raw = self._private_file(manifest_path, "delegation manifest")
        if len(raw) > 256 * 1024:
            raise OdooSessionEventError("delegation manifest is oversized")
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OdooSessionEventError("delegation manifest is invalid JSON") from exc
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema_version", "principals"}
                or manifest.get("schema_version") != 1
                or not isinstance(manifest.get("principals"), dict)):
            raise OdooSessionEventError("delegation manifest does not match schema 1")
        key = f"{event.database}:{event.actor_id}"
        selected = manifest["principals"].get(key)
        if (not isinstance(selected, dict)
                or set(selected) != {"api_key_file", "scopes", "allow_mutations"}):
            raise OdooSessionEventError("this Odoo actor has no configured delegation")
        relative = selected["api_key_file"]
        if (not isinstance(relative, str) or not relative
                or Path(relative).is_absolute()):
            raise OdooSessionEventError("delegation api_key_file is invalid")
        key_path = (self.root / relative).resolve()
        if not key_path.is_relative_to(self.root) or key_path == self.root:
            raise OdooSessionEventError("delegation api_key_file escapes its directory")
        key_body = self._private_file(key_path, "delegated API key")
        if not key_body or len(key_body) > 64 * 1024:
            raise OdooSessionEventError("delegated API key has an invalid size")
        scopes = selected["scopes"]
        if (not isinstance(scopes, list) or not scopes or len(scopes) != len(set(scopes))
                or any(not isinstance(scope, str) for scope in scopes)):
            raise OdooSessionEventError("delegation scopes are invalid")
        allow_mutations = selected["allow_mutations"]
        if not isinstance(allow_mutations, bool):
            raise OdooSessionEventError("delegation allow_mutations must be a boolean")
        return OdooDelegation(
            api_key_path=key_path,
            scopes=tuple(sorted(scopes)),
            allow_mutations=allow_mutations,
        )
