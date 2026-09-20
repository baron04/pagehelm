"""Wire hygiene for the remote transport: protocol errors, payload safety, copies.

Nothing here holds session state. It is the shared vocabulary the channel, the registry
and the browser client all read from, so a rejected message means the same thing at
every hop of the gateway.
"""
from __future__ import annotations

import json
import time

from ..origins import OriginError, origin_of
from ..privacy import (SENSITIVE_CREDENTIAL_NAME, SESSION_HANDOFF_QUERY_KEYS,
                      has_unredacted_sensitive_assignment,
                      has_unredacted_session_handoff, redact_sensitive_assignments,
                      redact_session_handoffs)
from ..schemas import SchemaError, canonical_json, parse_event_id


class RemoteProtocolError(ValueError):
    """A remote message conflicts with established session state."""

    def __init__(self, message: str, *, code: str = "protocol_error", status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


class RemoteSessionClosed(RemoteProtocolError):
    def __init__(self, message: str = "remote session is closed"):
        super().__init__(message, code="session_closed", status=410)


_MAX_EVENT_BYTES = 256 * 1024
_MAX_MESSAGE_ID_LENGTH = 128
_MAX_ORIGIN_LENGTH = 2048
_MAX_PAGE_URL_LENGTH = 4096


def _now_ms() -> int:
    return int(time.time() * 1000)


def _origin(value: str) -> str:
    """The origin a wire value belongs to -- a page URL as readily as an origin itself."""
    try:
        return origin_of(value, name="origin")
    except OriginError as exc:
        raise RemoteProtocolError(str(exc), status=400) from exc


def _event_sequence(event_id: str) -> int:
    try:
        return parse_event_id(event_id, name="Last-Event-ID", allow_empty=True)
    except SchemaError as exc:
        raise RemoteProtocolError(str(exc), status=400) from exc


def _remote_args_echo(value, key: str = ""):
    """Mirror the Web Runtime's outbound argument redaction for receipt verification."""
    if (key == "value" or SENSITIVE_CREDENTIAL_NAME.search(key)
            or key.lower() in SESSION_HANDOFF_QUERY_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {child: _remote_args_echo(item, str(child))
                for child, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_remote_args_echo(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_assignments(redact_session_handoffs(value))
    return value


def _safe_payload(value) -> None:
    if (has_unredacted_session_handoff(value, url_decode_depth=3)
            or has_unredacted_sensitive_assignment(value, url_decode_depth=3)):
        raise RemoteProtocolError(
            "browser payload contains an unredacted credential value",
            code="privacy_boundary",
            status=400,
        )


def ensure_safe_browser_payload(value) -> None:
    """Validate data before a product component records and publishes it to the browser."""
    _safe_payload(value)


def _copy_json(value):
    """Detach a JSON value from its caller before it enters shared session state."""
    return json.loads(canonical_json(value))


def _copy_wire(value):
    """Round-trip one wire value so frozen dataclasses do not retain mutable children."""
    return type(value).from_json(_copy_json(value.to_json()))
