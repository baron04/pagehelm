"""Loopback HTTP/SSE gateway for the embedded Web Runtime.

This is a logical gateway inside the server Agent process, not a separately deployed
service.  It uses only the Python standard library and a session-scoped capability token.
SSE is deliberately server-to-browser only; observations, results and user messages return
over authenticated POST requests.
"""
from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .remote import RemoteProtocolError, RemoteSessionRegistry, SessionGrant
from .schemas import (ActionResultEnvelope, ApprovalDecision, ClientHello,
                      ObservationEnvelope, SchemaError, canonical_json)

MAX_BODY_BYTES = 1024 * 1024
SESSION_PATH = re.compile(
    r"^/v1/sessions/([A-Za-z0-9_-]{1,128})/"
    r"(hello|observations|claims|results|messages|approvals|events)$"
)
RUNTIME_PATH = Path(__file__).resolve().parent / "web_runtime" / "embedded.js"


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("expected an absolute http(s) URL")
    port = parsed.port
    if port == (80 if scheme == "http" else 443):
        port = None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}{f':{port}' if port is not None else ''}"


def _handler(registry: RemoteSessionRegistry, *, sse_wait_s: float):
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
            self.wfile.write(body)

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
                return channel, ""
            lease_id = self.headers.get("X-WOA-Lease", "")
            channel.validate_active_lease(lease_id)
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
            if not match or match.group(2) == "events":
                self._json(404, {"error": "unknown endpoint", "code": "not_found"})
                return
            session_id, endpoint = match.groups()
            try:
                channel, lease_id = self._authenticate(
                    session_id, require_lease=endpoint != "hello")
                body = self._body()
                if endpoint == "hello":
                    hello = ClientHello.from_json(body)
                    page = urlsplit(hello.page_origin)
                    if f"{page.scheme}://{page.netloc}" != self._origin():
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
                else:
                    reply = channel.accept_message(body, lease_id=lease_id)
                self._json(200, reply, session_id=session_id)
            except Exception as exc:  # schema and protocol exceptions become typed HTTP errors
                self._error(exc, session_id=session_id)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._json(200, {"ok": True, "protocol": 1})
                return
            if path == "/v1/runtime/embedded.js":
                try:
                    body = RUNTIME_PATH.read_bytes()
                except OSError as exc:
                    self._error(exc)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return
            match = self._route()
            if not match or match.group(2) != "events":
                self._json(404, {"error": "unknown endpoint", "code": "not_found"})
                return
            session_id = match.group(1)
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
                 host: str = "127.0.0.1", port: int = 0, sse_wait_s: float = 15):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("embedded gateway may bind only to loopback")
        self.registry = registry or RemoteSessionRegistry()
        self._server = ThreadingHTTPServer((host, port),
                                           _handler(self.registry, sse_wait_s=sse_wait_s))
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        return f"http://{shown}:{port}"

    @property
    def runtime_url(self) -> str:
        return self.base_url + "/v1/runtime/embedded.js"

    def bootstrap_config(self, grant: SessionGrant, *,
                         page_controller_module: str,
                         navigation_mode: str = "document") -> dict:
        """Return data for the first-party shell to place in ``window.__WOA_EMBED_CONFIG__``."""
        if not isinstance(page_controller_module, str):
            raise ValueError("page_controller_module must be an absolute http(s) URL")
        parsed = urlsplit(page_controller_module)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("page_controller_module must be an absolute http(s) URL")
        module_origin = _normalized_origin(page_controller_module)
        allowed_module_origins = {_normalized_origin(origin)
                                  for origin in grant.allowed_origins}
        if module_origin not in allowed_module_origins:
            raise ValueError(
                "page_controller_module origin must be an allowed session origin")
        if navigation_mode not in {"document", "spa"}:
            raise ValueError("navigation_mode must be document or spa")
        return {
            "endpoint": self.base_url,
            "sessionId": grant.session_id,
            "clientToken": grant.client_token,
            "pageControllerModule": page_controller_module,
            "navigationMode": navigation_mode,
        }

    def bootstrap_script(self, grant: SessionGrant, *,
                         page_controller_module: str,
                         navigation_mode: str = "document") -> str:
        """Build the two-tag first-party shell integration as one ephemeral script.

        The returned source contains the session capability and must be injected directly
        into the authenticated page; callers must never persist or log it.  Keeping this
        construction here prevents every application adapter from inventing a different
        runtime boot contract.
        """
        config = self.bootstrap_config(
            grant, page_controller_module=page_controller_module,
            navigation_mode=navigation_mode)
        runtime_url = json.dumps(self.runtime_url)
        allowed_origins = json.dumps(sorted(grant.allowed_origins), separators=(",", ":"))
        safe_config = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")
        return f"""
          (() => {{
            if (window.top !== window || window.__WOA_EMBEDDED_RUNTIME_STARTED__) return;
            if (!new Set({allowed_origins}).has(location.origin)) return;
            const start = () => {{
              window.__WOA_RUNTIME_READY__ = import({runtime_url}).then(
                module => module.startEmbeddedRuntime({safe_config}));
            }};
            if (document.readyState === 'loading') {{
              document.addEventListener('DOMContentLoaded', start, {{once: true}});
            }} else {{ start(); }}
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
