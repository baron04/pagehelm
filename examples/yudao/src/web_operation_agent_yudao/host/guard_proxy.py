"""Native API proxy for a deployment that opts into the page-guard integration.

The adapter supplies identity verification and reviewed read routes. This server does
not decide natural-language tasks, create approvals or retry upstream requests. It must
be the browser's only API ingress; exposing begin/end without routing the API here does
not enable native operations safely. No token, cookie, body or URL is logged.
"""
from __future__ import annotations

import http.client
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import urllib.request

from web_operation_agent.privacy import redact_sensitive_assignments, redact_session_handoffs

from .page_guard import GuardRefused, NativePageGuard, WINDOW_KEY

GUARD_PATH = "/woa/page-guard"
#: What the page's own API requests carry while the assistant is working that page
#: (frontend/src/integrations/yudao/page-facts.js). Only these are fenced: another tab,
#: another device and the person's own actions never carry it and are never held.
ASSISTANT_HEADER = "X-WOA-Assistant"
#: Which of the actor's windows a marked request belongs to. It scopes guard evidence
#: to the page that made it; it never travels upstream.
WINDOW_HEADER = "X-WOA-Window"
_COOKIE = "woa_native_window"
_MAX_BODY = 20 * 1024 * 1024
_PUBLIC = frozenset({"/admin-api/system/auth/login", "/admin-api/system/auth/refresh-token",
                    "/admin-api/system/captcha/get", "/admin-api/system/captcha/check",
                    "/admin-api/system/tenant/get-id-by-name", "/admin-api/system/tenant/get-by-website"})


class NativeGuardProxy:
    def __init__(self, *, upstream: str, origin: str, guard: NativePageGuard,
                 resolve_actor, host="127.0.0.1", port=0, timeout_s=30,
                 application_window: bool = False, ingress_base: str | None = None):
        target = urlsplit(upstream)
        site = urlsplit(origin)
        if (target.scheme not in {"http", "https"} or not target.hostname or target.username
                or target.password or target.query or target.fragment or target.path not in {"", "/"}
                or site.scheme not in {"http", "https"} or not site.netloc
                or site.username or site.password
                or site.path not in {"", "/"} or site.query or site.fragment
                or not callable(resolve_actor) or isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 120):
            raise ValueError("the proxy requires fixed upstream, origin and identity verifier")
        self.upstream, self.origin = target, origin.rstrip("/")
        # Cookies share a host across ports. Scope the window owner to the published
        # origin so two local deployments cannot overwrite each other's owner cookie.
        self.cookie_name = _COOKIE + "_" + hashlib.sha256(self.origin.encode()).hexdigest()[:16]
        ingress = urlsplit(ingress_base or self.origin)
        if (ingress.scheme not in {"http", "https"} or not ingress.hostname or ingress.username
                or ingress.password or ingress.query or ingress.fragment or ingress.path not in {"", "/"}):
            raise ValueError("the ingress probe requires a fixed HTTP origin")
        # A container may reach the same routing front internally while browsers use
        # its published port. Origin enforcement remains bound to the public origin.
        self.ingress_base = ingress_base.rstrip("/") if ingress_base else None
        self.guard, self.resolve_actor, self.timeout_s = guard, resolve_actor, timeout_s
        #: The application refuses its own writes while a window is open (the server's
        #: `/admin-api/woa/page-guard`); a window it does not confirm is not opened.
        self.application_window = application_window is True
        self._probe = secrets.token_hex(32)
        self.http = ThreadingHTTPServer((host, port), self._handler())
        self._thread = None
        self._ingress_verified = False

    @property
    def url(self):
        return f"http://{self.http.server_address[0]}:{self.http.server_port}"

    def start(self):
        self._thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._ingress_verified = False
        if self._thread is not None:
            self.http.shutdown()
            self._thread.join(3)
        self.http.server_close()

    def verify_ingress(self) -> None:
        """Require both public routes to reach this process before enabling UI actions."""
        self._ingress_verified = False
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        for path in ("/admin-api/woa-native-guard/probe", GUARD_PATH + "/probe"):
            request = urllib.request.Request((self.ingress_base or self.origin) + path,
                headers={"X-WOA-Guard-Probe": self._probe, "Host": urlsplit(self.origin).netloc})
            try:
                with opener.open(request, timeout=5) as response:
                    body = json.loads(response.read(1024))
                    if body != {"guard_proxy": 1, "nonce": self._probe}:
                        raise GuardRefused("guard_ingress_unverified")
            except Exception:
                raise GuardRefused("guard_ingress_unverified") from None
        self._ingress_verified = True

    @property
    def method_fence_ready(self) -> bool:
        return (self._ingress_verified and self._thread is not None
                and self._thread.is_alive() and self.guard.writing_gets is not None)

    def _handler(self):
        proxy = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(proxy.timeout_s)

            def log_message(self, *_args):
                pass

            def reply(self, status, payload, *, cookie=""):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    secure = "; Secure" if proxy.origin.startswith("https:") else ""
                    self.send_header("Set-Cookie", f"{proxy.cookie_name}={cookie}; Path=/; HttpOnly; SameSite=Strict{secure}")
                self.end_headers()
                self.wfile.write(body)

            def actor(self):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or not auth[7:].strip():
                    raise GuardRefused("identity_required")
                return proxy.resolve_actor(auth[7:].strip(), self.headers.get("tenant-id", ""))

            def body(self, limit=_MAX_BODY):
                if self.headers.get("Transfer-Encoding"):
                    raise GuardRefused("unsupported_transfer")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise GuardRefused("invalid_length") from None
                if length < 0 or length > limit:
                    raise GuardRefused("body_too_large")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise GuardRefused("incomplete_body")
                return body

            def window(self):
                if self.command != "POST" or self.headers.get("Origin") != proxy.origin:
                    raise GuardRefused("origin_refused")
                actor = self.actor()
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                except Exception:
                    raise GuardRefused("invalid_browser") from None
                owner = cookie[proxy.cookie_name].value if proxy.cookie_name in cookie else ""
                # The cookie is this proxy's own generated token; a value it never issued
                # is refused rather than trusted as a window name.
                if owner and not re.fullmatch(r"[a-f0-9]{64}", owner):
                    raise GuardRefused("invalid_browser")
                body = json.loads(self.body(8192) or b"{}")
                if not isinstance(body, dict):
                    raise GuardRefused("invalid_window")
                named = body.get("window")
                if named is not None and (not isinstance(named, str) or not WINDOW_KEY.fullmatch(named)):
                    raise GuardRefused("invalid_window")
                if self.path == GUARD_PATH + "/begin":
                    # A page that names its window gets its own: one tab's begin/end never
                    # opens or closes another tab's, as one shared cookie made them do.
                    # It gets no window cookie either -- a name is what it already carries,
                    # and a planted copy of it would fail this handler's own token check.
                    issued = named is None
                    owner = named or owner or secrets.token_hex(32)
                    ttl = proxy.guard.begin(actor, owner, ttl_s=body.get("ttl_s", 120))
                    state = {"active": True, "expires_at": time.time() + ttl, "window": owner}
                    if proxy.guard.permit_active(actor):
                        # An approved click, not a read-only window: the application's own
                        # statement guard must be off for the request the card shows. The
                        # permit still decides every write; nothing else can be sent.
                        if proxy.application_window:
                            ended = self.application("end", {})
                            if not ended or ended.get("active") is True:
                                proxy.guard.disarm_permit(actor)
                                raise GuardRefused("application_window_unavailable")
                        state["guard"] = "approved_submit"
                    elif proxy.application_window:
                        confirmed = self.application("begin", {"ttl_s": math.ceil(ttl)})
                        if confirmed.get("active") is not True or confirmed.get("guard") != "read_only_statements":
                            proxy.guard.end(actor, owner)
                            raise GuardRefused("application_window_unavailable")
                        state["guard"] = "read_only_statements"
                    self.reply(200, state, cookie=owner if issued else "")
                elif self.path == GUARD_PATH + "/end":
                    owner = named or owner
                    if not owner:
                        raise GuardRefused("invalid_browser")
                    proxy.guard.end(actor, owner)
                    if proxy.application_window and not proxy.guard.protected(actor):
                        # The application's window also ends by its own clock.
                        self.application("end", {})
                    # "Released" is about the window this page held: another page's still
                    # open one keeps the actor fenced but never blocks this page's release.
                    self.reply(200, {"active": proxy.guard.active(actor, owner)})
                else:
                    raise GuardRefused("unknown_guard_operation")

            def application(self, operation, payload):
                """The application's answer to opening or closing its own read-only window."""
                connect = http.client.HTTPSConnection if proxy.upstream.scheme == "https" else http.client.HTTPConnection
                connection = connect(proxy.upstream.hostname, proxy.upstream.port, timeout=proxy.timeout_s)
                headers = {key: self.headers[key] for key in ("Authorization", "tenant-id") if key in self.headers}
                headers["Content-Type"] = "application/json"
                try:
                    connection.request("POST", f"/admin-api/woa/page-guard/{operation}",
                                       body=json.dumps(payload).encode(), headers=headers)
                    response = connection.getresponse()
                    answer = json.loads(response.read(8192) or b"{}")
                except Exception:
                    return {}
                finally:
                    connection.close()
                data = answer.get("data") if isinstance(answer, dict) and answer.get("code") == 0 else None
                return data if isinstance(data, dict) else {}

            def forward(self):
                path = urlsplit(self.path).path
                if not self.path.startswith("/admin-api/") or "#" in self.path:
                    raise GuardRefused("route_refused")
                visit = self.headers.get("visit-tenant-id", "")
                if visit and visit != self.headers.get("tenant-id", ""):
                    raise GuardRefused("native_visit_tenant_unsupported")
                body = self.body()
                # Public authentication is still upstream-verified. A request carrying
                # a bearer is always actor-bound, even if its route also accepts guests.
                anonymous = path in _PUBLIC and not self.headers.get("Authorization")
                actor = None if anonymous else self.actor()
                if actor is None or self.headers.get(ASSISTANT_HEADER) != "1":
                    if actor is not None and self.command == "GET":
                        try:
                            proxy.guard.note_read(actor, self.path)
                        except GuardRefused:
                            pass          # a full actor table must not stop the person's own read
                    status, payload = self.upstream_request(body)
                    if actor is not None and self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                        accepted = _accepted_user_write(status, payload)
                        app_code, error_msg = _user_write_error(status, payload)
                        wanted, created = _write_ids(body, payload, self.path)
                        context = _write_context(body, self.path)
                        try:
                            proxy.guard.note_user_write(
                                actor,
                                self.command,
                                self.path,
                                wanted,
                                created,
                                status=status,
                                accepted=accepted,
                                app_code=app_code,
                                error_msg=error_msg,
                                context=context,
                            )
                        except GuardRefused:
                            pass          # a full actor table must not drop the person's own write
                else:
                    named = self.headers.get(WINDOW_HEADER) or ""
                    window = named if WINDOW_KEY.fullmatch(named) else ""
                    with proxy.guard.request(actor, self.command, self.path, body,
                                             window=window) as ticket:
                        ticket.status, ticket.response = self.upstream_request(body)

            def upstream_request(self, body):
                connect = http.client.HTTPSConnection if proxy.upstream.scheme == "https" else http.client.HTTPConnection
                connection = connect(proxy.upstream.hostname, proxy.upstream.port, timeout=proxy.timeout_s)
                headers = {key: self.headers[key] for key in ("Authorization", "tenant-id", "Content-Type",
                                                              "Accept", ASSISTANT_HEADER)
                           if key in self.headers}
                # The window cookie never leaves the application distribution.
                try:
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    payload = response.read(_MAX_BODY + 1)
                    if len(payload) > _MAX_BODY:
                        raise GuardRefused("upstream_body_too_large")
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return response.status, payload[:65536]
                finally:
                    connection.close()

            def dispatch(self):
                try:
                    if self.path in {"/admin-api/woa-native-guard/probe", GUARD_PATH + "/probe"}:
                        probe = self.headers.get("X-WOA-Guard-Probe", "")
                        if self.command != "GET" or not secrets.compare_digest(probe, proxy._probe):
                            raise GuardRefused("probe_refused")
                        self.reply(200, {"guard_proxy": 1, "nonce": proxy._probe})
                    elif self.path.startswith(GUARD_PATH + "/"):
                        self.window()
                    else:
                        self.forward()
                except GuardRefused as exc:
                    if exc.code == "identity_required" and self.path.startswith("/admin-api/"):
                        # Yudao refreshes an expired access token from its own code=401
                        # envelope. An HTTP 401 reaches Axios's transport-error path and
                        # leaves the page signed out without attempting that refresh.
                        # The refused request is never forwarded or executed.
                        self.reply(200, {"code": 401, "msg": "账号未登录", "data": None})
                    else:
                        self.reply(401 if exc.code == "identity_required" else 403, {"error": exc.code})
                except (ValueError, json.JSONDecodeError):
                    self.reply(400, {"error": "invalid_request"})
                except Exception:
                    self.reply(503, {"error": "upstream_unavailable"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = dispatch
        return Handler


__all__ = ["NativeGuardProxy", "GUARD_PATH"]


def _accepted_user_write(status, payload) -> bool:
    """This application's own "accepted": HTTP success plus its success code in the body."""
    if not (isinstance(status, int) and 200 <= status < 300 and payload):
        return False
    try:
        answer = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(answer, dict) and answer.get("code") == 0


def _write_ids(request_body, payload, request_path) -> tuple[str, str]:
    """(id the write named, id the application returned), each "" when absent."""
    wanted = created = ""
    try:
        body = json.loads(request_body)
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("id"), (str, int)) \
            and not isinstance(body.get("id"), bool):
        wanted = str(body["id"])
    if not wanted:
        match = re.search(r"(?:^|[?&])id=(\d{1,19})(?:&|$)", request_path)
        if match:
            wanted = match.group(1)
    try:
        data = json.loads(payload).get("data")
    except (ValueError, UnicodeDecodeError, AttributeError):
        data = None
    if isinstance(data, (str, int)) and not isinstance(data, bool):
        created = str(data)
    elif isinstance(data, dict) and isinstance(data.get("id"), (str, int)) \
            and not isinstance(data.get("id"), bool):
        created = str(data["id"])
    return wanted, created


_CONTEXT_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_SECRET_CONTEXT_KEY = re.compile(
    r"(?:pass|secret|token|key|auth|cookie|credential|salt|sign)", re.IGNORECASE
)


def _user_write_error(status: int, payload: bytes) -> tuple[int | None, str]:
    """(app_code, sanitized_error_msg) for a rejected user write, else (None, '')."""
    if _accepted_user_write(status, payload):
        return None, ""
    app_code: int | None = None
    raw_msg = ""
    if payload:
        try:
            answer = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            answer = None
        if isinstance(answer, dict):
            code = answer.get("code")
            if isinstance(code, int) and not isinstance(code, bool):
                app_code = code
            for field in ("msg", "message", "error", "detail"):
                val = answer.get(field)
                if isinstance(val, str) and val.strip():
                    raw_msg = val
                    break
    if not raw_msg:
        return app_code, ""
    redacted = str(redact_session_handoffs(redact_sensitive_assignments(raw_msg)))
    cleaned = "".join(" " if ch in "\r\n\t" else ch for ch in redacted if ch in "\r\n\t" or ord(ch) >= 32)
    cleaned = " ".join(cleaned.split())
    return app_code, cleaned[:200]


def _write_context(request_body: bytes, request_path: str) -> dict[str, str]:
    """Bounded non-secret scalar context parameters from a write request's query/body."""
    context: dict[str, str] = {}
    excluded = {"id", "pageNo", "pageSize"}

    def _keep(key: str, value) -> None:
        if len(context) >= 8 or not isinstance(key, str) or key in excluded:
            return
        if not _CONTEXT_KEY.fullmatch(key) or _SECRET_CONTEXT_KEY.search(key):
            return
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return
        text = str(value).strip()
        if not text or len(text) > 80:
            return
        context[key] = text

    query = urlsplit(request_path).query
    if query:
        for key, value in parse_qsl(query, keep_blank_values=False):
            _keep(key, value)
    if request_body:
        try:
            body = json.loads(request_body)
        except (ValueError, UnicodeDecodeError):
            body = None
        if isinstance(body, dict):
            for key, value in body.items():
                if isinstance(key, str) and (key.endswith("Id") or key.endswith("_id")):
                    if key not in context:
                        _keep(key, value)
    return context
