"""Small, dependency-free producer for the WOA signed Odoo-session protocol."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time


# A page instance is the runtime's <tab>.<epoch> name; hex-only ids predate it.
_INSTANCE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def session_event(*, event, database, company_id, actor_id,
                  runtime_instance_id="", now_ms=None):
    if event not in {"bootstrap", "revoke"}:
        raise ValueError("unsupported session event")
    if event == "bootstrap" and not _INSTANCE.fullmatch(runtime_instance_id or ""):
        raise ValueError("invalid runtime instance")
    if event == "revoke":
        runtime_instance_id = ""
    issued_at = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return {
        "schema_version": 1,
        "event": event,
        "database": str(database),
        "company_id": str(company_id),
        "actor_id": str(actor_id),
        "runtime_instance_id": runtime_instance_id,
        "issued_at": issued_at,
        "expires_at": issued_at + 30_000,
        "nonce": secrets.token_hex(16),
    }


def signed_envelope(event, secret):
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("session signing secret is too short")
    body = json.dumps(
        event, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return {
        "event": event,
        "signature": hmac.new(secret, body, hashlib.sha256).hexdigest(),
    }
