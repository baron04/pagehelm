"""Loopback HTTP/SSE gateway for the embedded Web Runtime.

This is a logical gateway inside the server Agent process, not a separately deployed
service.  It uses only the Python standard library and a session-scoped capability token.
SSE is deliberately server-to-browser only; observations, results and user messages return
over authenticated POST requests.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .origins import origin_of
from .panel_view import label_keys, panel_limits
from .remote import RemoteProtocolError, RemoteSessionRegistry, SessionGrant
from .schemas import (ActionResultEnvelope, ApprovalDecision, CancellationRequest, ClientHello,
                      ObservationEnvelope, SchemaError, canonical_json)

MAX_BODY_BYTES = 1024 * 1024
SESSION_PATH = re.compile(
    r"^/v1/sessions/([A-Za-z0-9_-]{1,128})/"
    r"(hello|observations|claims|results|messages|approvals|cancellations|events|timeline|conversation)$"
)
WEB_RUNTIME = Path(__file__).resolve().parent / "web_runtime"
#: The one script a host page includes to have the assistant: the boot supervisor and the
#: Runtime, built from `frontend/src/runtime/` by `scripts/build_runtime.py`. The panel is a
#: separate module under `PANEL_PREFIX` that the Runtime loads once the server has spoken.
#: An explicit map keeps the directory from becoming a served tree by accident.
RUNTIME_PATH = WEB_RUNTIME / "assistant.js"
RUNTIME_MODULES = {
    "/v1/runtime/assistant.js": RUNTIME_PATH,
}
PANEL_ROOT = WEB_RUNTIME / "panel"
PANEL_PREFIX = "/v1/runtime/panel/"
_PANEL_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,80})?$")


@lru_cache(maxsize=1)
def _panel_manifest() -> dict:
    """What the built panel ships, by published path.

    A checkout without Node has no panel.  That is reported as a named prerequisite rather
    than served from whatever happens to be lying in the directory -- the same rule the
    PageAgent bundle gate applies.
    """
    try:
        manifest = json.loads((PANEL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    assets = manifest.get("assets")
    return assets if isinstance(assets, dict) else {}


_SENSITIVE_QUERY_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,63}\Z")


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppress transport noise when a document closes its long-poll sockets."""

    def handle_error(self, request, client_address) -> None:
        import sys

        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _panel_labels(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("panel_labels must be a string mapping")
    if not all(isinstance(key, str) for key in value):
        raise TypeError("panel_labels keys must be strings")
    unknown = set(value) - label_keys()
    if unknown:
        raise ValueError(f"unknown panel label: {sorted(unknown)[0]}")
    maximum = panel_limits()["label_max_chars"]
    labels = {}
    for key, label in value.items():
        if (not isinstance(label, str) or not label.strip() or len(label) > maximum
                or any(ord(character) < 32 for character in label)):
            raise ValueError(
                f"panel label {key} must contain 1-{maximum} display characters")
        labels[key] = label
    return labels


def _sensitive_query_keys(value) -> list[str]:
    if value is None:
        return []
    if (not isinstance(value, (tuple, list, set, frozenset)) or len(value) > 32
            or any(not isinstance(key, str) or not _SENSITIVE_QUERY_KEY.fullmatch(key)
                   for key in value)):
        raise ValueError("sensitive_query_keys must contain at most 32 safe query names")
    return sorted({key.casefold() for key in value})


def _readiness_payload(value: Mapping) -> dict:
    """Project a trusted status callback onto a content-free public schema."""
    if not isinstance(value, Mapping):
        raise TypeError("readiness provider must return a mapping")

    def boolean(name: str) -> bool:
        selected = value.get(name)
        if not isinstance(selected, bool):
            raise ValueError(f"readiness {name} must be a boolean")
        return selected

    def count(source: Mapping, name: str, maximum: int = 1_000_000) -> int:
        selected = source.get(name)
        if (isinstance(selected, bool) or not isinstance(selected, int)
                or not 0 <= selected <= maximum):
            raise ValueError(f"readiness {name} must be a bounded non-negative integer")
        return selected

    payload = {
        "schema_version": 1,
        "protocol": 1,
        "ready": boolean("ready"),
        "accepting_sessions": boolean("accepting_sessions"),
        "session_count": count(value, "session_count"),
    }
    if "session_capacity" in value:
        capacity = count(value, "session_capacity")
        if capacity < 1 or payload["session_count"] > capacity:
            raise ValueError("readiness session capacity is inconsistent")
        payload["session_capacity"] = capacity
    provider = value.get("provider")
    if provider is not None:
        if not isinstance(provider, Mapping):
            raise ValueError("readiness provider status must be a mapping")
        retry_after_s = provider.get("retry_after_s")
        if (isinstance(retry_after_s, bool)
                or not isinstance(retry_after_s, (int, float))
                or not math.isfinite(retry_after_s)
                or not 0 <= retry_after_s <= 86_400):
            raise ValueError("readiness retry_after_s must be a bounded finite number")
        circuit_open = provider.get("circuit_open")
        if not isinstance(circuit_open, bool):
            raise ValueError("readiness circuit_open must be a boolean")
        payload["provider"] = {
            "max_concurrent_requests": count(provider, "max_concurrent_requests", 10_000),
            "in_flight": count(provider, "in_flight", 10_000),
            "consecutive_transient_failures": count(
                provider, "consecutive_transient_failures", 10_000),
            "circuit_open": circuit_open,
            "retry_after_s": round(float(retry_after_s), 3),
        }
        if payload["provider"]["in_flight"] > payload["provider"]["max_concurrent_requests"]:
            raise ValueError("readiness provider concurrency is inconsistent")
        payload["ready"] = payload["ready"] and not circuit_open
    payload["accepting_sessions"] = payload["accepting_sessions"] and payload["ready"]
    return payload


def _bounded_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        return panel_limits()["history_page_size"]
    return min(500, max(1, value))


def _handler(registry: RemoteSessionRegistry, *, sse_wait_s: float,
             readiness_provider: Callable[[], Mapping] | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WOAEmbeddedGateway/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            # Request paths contain a session id and Authorization contains a capability.
            # The product ledger records semantic events explicitly; default access logs do not.
            return

        def _route(self):
            return SESSION_PATH.fullmatch(urlsplit(self.path).path)

        def _origin(self) -> str:
            return self.headers.get("Origin", "")

        def _cors(self, session_id: str | None = None) -> None:
            origin = self._origin()
            if session_id and registry.origin_allowed(session_id, origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _json(self, status: int, payload: dict, *, session_id: str | None = None) -> None:
            body = canonical_json(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors(session_id)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A page reload replaces the lease and closes its pending request. The
                # semantic disconnect is recorded by the session; the old socket is not a
                # gateway failure and must not produce a server traceback.
                return

        def _static(self, source: Path, media_type: str, *, cache: str) -> None:
            try:
                body = source.read_bytes()
            except OSError as exc:
                self._error(exc)
                return
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            # These are public, non-secret assets that a first-party page loads as modules,
            # so they need CORS.  Nothing session-bound is ever served from this path.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _error(self, exc: Exception, *, session_id: str | None = None) -> None:
            if isinstance(exc, RemoteProtocolError):
                status, code = exc.status, exc.code
            elif isinstance(exc, (SchemaError, KeyError, TypeError, ValueError, json.JSONDecodeError)):
                status, code = HTTPStatus.BAD_REQUEST, "invalid_message"
            else:
                status, code = HTTPStatus.INTERNAL_SERVER_ERROR, "gateway_error"
            detail = str(exc) if status < 500 else "gateway could not process the request"
            self._json(int(status), {"error": detail, "code": code}, session_id=session_id)

        def _authenticate(self, session_id: str, *, require_lease: bool = True):
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                raise RemoteProtocolError("missing session credential",
                                          code="unauthorized", status=401)
            origin = self._origin()
            if not origin:
                raise RemoteProtocolError("browser Origin header is required",
                                          code="origin_refused", status=403)
            channel = registry.authenticate(session_id, authorization[7:], origin)
            if not require_lease:
                channel.touch_client_activity()
                return channel, ""
            lease_id = self.headers.get("X-WOA-Lease", "")
            channel.validate_active_lease(lease_id)
            channel.touch_client_activity()
            return channel, lease_id

        def _body(self) -> dict:
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise RemoteProtocolError("Content-Type must be application/json",
                                          code="unsupported_media_type", status=415)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise RemoteProtocolError("Content-Length is required", status=411)
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise RemoteProtocolError("Content-Length must be an integer", status=400) from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise RemoteProtocolError("request body exceeds 1 MiB", code="payload_too_large", status=413)
            body = self.rfile.read(length)
            return json.loads(body.decode("utf-8"))

        def do_OPTIONS(self):
            match = self._route()
            if not match:
                self._json(404, {"error": "unknown endpoint", "code": "not_found"})
                return
            session_id = match.group(1)
            if not registry.origin_allowed(session_id, self._origin()):
                self._json(403, {"error": "request origin is outside this session",
                                 "code": "origin_refused"})
                return
            self.send_response(204)
            self._cors(session_id)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, Last-Event-ID, X-WOA-Lease")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            match = self._route()
            if not match or match.group(2) in {"events", "timeline"}:
                self._json(404, {"error": "unknown endpoint", "code": "not_found"})
                return
            session_id, endpoint = match.groups()
            try:
                channel, lease_id = self._authenticate(
                    session_id, require_lease=endpoint != "hello")
                body = self._body()
                if endpoint == "hello":
                    hello = ClientHello.from_json(body)
                    if origin_of(hello.page_origin) != origin_of(self._origin()):
                        raise RemoteProtocolError(
                            "hello page_origin does not match the request Origin",
                            code="origin_refused",
                            status=403,
                        )
                    reply = channel.accept_hello(hello)
                elif endpoint == "observations":
                    reply = channel.accept_observation(
                        ObservationEnvelope.from_json(body), lease_id=lease_id)
                elif endpoint == "claims":
                    reply = channel.claim_action(body, lease_id=lease_id)
                elif endpoint == "results":
                    reply = channel.accept_result(
                        ActionResultEnvelope.from_json(body), lease_id=lease_id)
                elif endpoint == "approvals":
                    reply = channel.accept_approval(
                        ApprovalDecision.from_json(body), lease_id=lease_id)
                elif endpoint == "cancellations":
                    reply = channel.accept_cancellation(
                        CancellationRequest.from_json(body), lease_id=lease_id)
                elif endpoint == "conversation":
                    if body != {"action": "new"}:
                        raise RemoteProtocolError(
                            "conversation takes exactly {\"action\": \"new\"}", status=400)
                    reply = channel.request_new_conversation(lease_id=lease_id)
                else:
                    reply = channel.accept_message(body, lease_id=lease_id)
                self._json(200, reply, session_id=session_id)
            except Exception as exc:  # schema and protocol exceptions become typed HTTP errors
                self._error(exc, session_id=session_id)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._json(200, {"ok": True, "protocol": 1, "schema_version": 1})
                return
            if path == "/readyz":
                try:
                    raw = (readiness_provider() if readiness_provider is not None else {
                        "ready": True,
                        "accepting_sessions": True,
                        "session_count": registry.session_count,
                    })
                    status = _readiness_payload(raw)
                except Exception:
                    status = {
                        "schema_version": 1,
                        "protocol": 1,
                        "ready": False,
                        "accepting_sessions": False,
                        "session_count": registry.session_count,
                        "status_error": True,
                    }
                self._json(200 if status["ready"] else 503, status)
                return
            if path in RUNTIME_MODULES:
                source = RUNTIME_MODULES[path]
                if not source.is_file():
                    # A build artifact, like the panel: named as a prerequisite rather than
                    # answered with a server error, or with some older copy.
                    self._json(404, {
                        "error": ("the assistant runtime is not built in this installation; "
                                  "run scripts/build_runtime.py"),
                        "code": "runtime_unavailable"})
                    return
                self._static(source, "text/javascript; charset=utf-8", cache="no-cache")
                return
            if path.startswith(PANEL_PREFIX):
                relative = path[len(PANEL_PREFIX):]
                entry = _panel_manifest().get(relative)
                if entry is None or not _PANEL_ASSET.fullmatch(relative):
                    self._json(404, {
                        "error": ("the assistant panel is not built in this installation; "
                                  "run scripts/build_panel.py"),
                        "code": "panel_unavailable"})
                    return
                # Content-hashed by the build, so it is safe to keep for a year.
                self._static(PANEL_ROOT / relative, entry["media_type"],
                             cache="public, max-age=31536000, immutable")
                return
            match = self._route()
            if not match or match.group(2) not in {"events", "timeline"}:
                self._json(404, {"error": "unknown endpoint", "code": "not_found"})
                return
            session_id, endpoint = match.groups()
            if endpoint == "timeline":
                try:
                    channel, lease_id = self._authenticate(session_id)
                    query = parse_qs(urlsplit(self.path).query)
                    self._json(200, channel.panel_timeline(
                        lease_id=lease_id,
                        after=query.get("after", [""])[0][:64],
                        before=query.get("before", [""])[0][:64],
                        limit=_bounded_limit(query.get("limit", [""])[0]),
                        wait_s=sse_wait_s if query.get("wait") else 0,
                    ), session_id=session_id)
                except Exception as exc:
                    self._error(exc, session_id=session_id)
                return
            try:
                channel, lease_id = self._authenticate(session_id)
                cursor = self.headers.get("Last-Event-ID", "")
                events = channel.events_after(cursor, lease_id=lease_id,
                                              wait_s=sse_wait_s)
                frames = []
                if not cursor:
                    frames.append("retry: 500\n")
                for event in events:
                    frames.append(
                        f"id: {event.event_id}\n"
                        f"event: {event.kind}\n"
                        f"data: {canonical_json(event.to_json())}\n\n"
                    )
                if not events:
                    frames.append(": keepalive\n\n")
                body = "".join(frames).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Accel-Buffering", "no")
                self._cors(session_id)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                self._error(exc, session_id=session_id)

    return Handler


class EmbeddedGateway:
    """Background HTTP listener sharing a ``RemoteSessionRegistry`` with the Agent."""

    def __init__(self, registry: RemoteSessionRegistry | None = None, *,
                 host: str = "127.0.0.1", port: int = 0, sse_wait_s: float = 15,
                 public_base_url: str | None = None,
                 allow_private_network_bind: bool = False,
                 readiness_provider: Callable[[], Mapping] | None = None):
        if not isinstance(allow_private_network_bind, bool):
            raise TypeError("allow_private_network_bind must be boolean")
        if (host not in {"127.0.0.1", "localhost", "::1"}
                and not (allow_private_network_bind and host == "0.0.0.0")):
            raise ValueError("embedded gateway may bind only to loopback")
        if readiness_provider is not None and not callable(readiness_provider):
            raise TypeError("readiness_provider must be callable or None")
        self.registry = registry or RemoteSessionRegistry()
        self._public_base_url = self._validate_public_base_url(public_base_url)
        self._server = _QuietThreadingHTTPServer(
            (host, port),
            _handler(
                self.registry, sse_wait_s=sse_wait_s,
                readiness_provider=readiness_provider),
        )
        self._thread: threading.Thread | None = None

    @staticmethod
    def _validate_public_base_url(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or value != value.strip() or not value:
            raise ValueError("public_base_url must be an absolute HTTPS URL")
        parsed = urlsplit(value)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if ((parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
                or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or value.endswith("/") or "/../" in parsed.path or "/./" in parsed.path):
            raise ValueError(
                "public_base_url must be HTTPS (or loopback HTTP), without credentials, "
                "query, fragment, trailing slash, or relative path segments")
        return value

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        return f"http://{shown}:{port}"

    @property
    def runtime_url(self) -> str:
        return self.public_base_url + "/v1/runtime/assistant.js"

    @property
    def public_base_url(self) -> str:
        """Browser-visible URL; the listener itself remains loopback-only."""
        return self._public_base_url or self.base_url

    def bootstrap_config(self, grant: SessionGrant, *,
                         page_controller_module: str,
                         navigation_mode: str = "document",
                         panel_labels: Mapping[str, str] | None = None,
                         sensitive_query_keys=()) -> dict:
        """Return data for the first-party shell to place in ``window.__WOA_EMBED_CONFIG__``."""
        if not isinstance(page_controller_module, str):
            raise ValueError("page_controller_module must be an absolute http(s) URL")
        parsed = urlsplit(page_controller_module)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("page_controller_module must be an absolute http(s) URL")
        module_origin = origin_of(page_controller_module, name="page_controller_module")
        allowed_module_origins = {origin_of(origin) for origin in grant.allowed_origins}
        if module_origin not in allowed_module_origins:
            raise ValueError(
                "page_controller_module origin must be an allowed session origin")
        if navigation_mode not in {"document", "spa"}:
            raise ValueError("navigation_mode must be document or spa")
        config = {
            "endpoint": self.public_base_url,
            "sessionId": grant.session_id,
            "clientToken": grant.client_token,
            "sessionGeneration": hashlib.sha256(
                grant.client_token.encode("utf-8")).hexdigest()[:32],
            "pageControllerModule": page_controller_module,
            "navigationMode": navigation_mode,
            "applicationId": grant.application_id,
            "principalBinding": grant.principal_digest,
            "principalExpiresAt": grant.expires_at,
            "sessionExpiresAt": grant.session_expires_at,
            "sensitiveQueryKeys": _sensitive_query_keys(sensitive_query_keys),
        }
        labels = _panel_labels(panel_labels)
        if labels:
            config["panelLabels"] = labels
        return config

    def bootstrap_script(self, grant: SessionGrant, *,
                         page_controller_module: str,
                         navigation_mode: str = "document",
                         panel_labels: Mapping[str, str] | None = None,
                         sensitive_query_keys=()) -> str:
        """Build a harness integration as one ephemeral script: the session, then `assistant.js`.

        The returned source contains the session capability and must be injected directly
        into the authenticated page; callers must never persist or log it. It hands the page
        a whole configuration, which cannot be renewed, so it is for development hosts such
        as the demos' Playwright `add_init_script`. A production host declares
        `<script type="module" src=RUNTIME_URL data-woa-bootstrap=...>` instead and lets the
        assistant obtain and renew sessions from its own endpoint.
        """
        config = self.bootstrap_config(
            grant, page_controller_module=page_controller_module,
            navigation_mode=navigation_mode, panel_labels=panel_labels,
            sensitive_query_keys=sensitive_query_keys)
        runtime_url = json.dumps(self.runtime_url)
        allowed_origins = json.dumps(sorted(grant.allowed_origins), separators=(",", ":"))
        safe_config = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")
        return f"""
          (() => {{
            if (window.top !== window || window.__WOA_ASSISTANT__) return;
            if (!new Set({allowed_origins}).has(location.origin)) return;
            window.__WOA_EMBED_CONFIG__ = {safe_config};
            import({runtime_url}).catch(error => console.warn('[woa] assistant.js:', error));
          }})();
        """

    def start(self) -> "EmbeddedGateway":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="woa-embedded-gateway", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> "EmbeddedGateway":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()
