"""Native API proxy for a deployment that opts into the page-guard integration.

The adapter supplies identity verification and reviewed read routes. This server does
not decide natural-language tasks, create approvals or retry upstream requests. It must
be the browser's only API ingress; exposing begin/end without routing the API here does
not enable native operations safely. No token, cookie, body or URL is logged.
"""
from __future__ import annotations

import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import secrets
import threading
import time
from urllib.parse import urlsplit
import urllib.request

from .page_guard import GuardRefused, NativePageGuard

GUARD_PATH = "/woa/page-guard"
_COOKIE = "woa_native_window"
_MAX_BODY = 20 * 1024 * 1024
_PUBLIC = frozenset({"/admin-api/system/auth/login", "/admin-api/system/auth/refresh-token",
                    "/admin-api/system/captcha/get", "/admin-api/system/captcha/check",
                    "/admin-api/system/tenant/get-id-by-name", "/admin-api/system/tenant/get-by-website"})


class NativeGuardProxy:
    def __init__(self, *, upstream: str, origin: str, guard: NativePageGuard,
                 resolve_actor, host="127.0.0.1", port=0, timeout_s=30,
                 application_window: bool = False):
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
        self.guard, self.resolve_actor, self.timeout_s = guard, resolve_actor, timeout_s
        #: The application refuses its own writes while a window is open (the server's
        #: `/admin-api/woa/page-guard`); a window it does not confirm is not opened.
        self.application_window = application_window is True
        self._probe = secrets.token_hex(32)
        self.http = ThreadingHTTPServer((host, port), self._handler())
        self._thread = None

    @property
    def url(self):
        return f"http://{self.http.server_address[0]}:{self.http.server_port}"

    def start(self):
        self._thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._thread is not None:
            self.http.shutdown()
            self._thread.join(3)
        self.http.server_close()

    def verify_ingress(self) -> None:
        """Require both public routes to reach this process before enabling UI actions."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        for path in ("/admin-api/woa-native-guard/probe", GUARD_PATH + "/probe"):
            request = urllib.request.Request(self.origin + path,
                headers={"X-WOA-Guard-Probe": self._probe})
            try:
                with opener.open(request, timeout=5) as response:
                    body = json.loads(response.read(1024))
                    if body != {"guard_proxy": 1, "nonce": self._probe}:
                        raise GuardRefused("guard_ingress_unverified")
            except Exception:
                raise GuardRefused("guard_ingress_unverified") from None

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
                    self.send_header("Set-Cookie", f"{_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Strict{secure}")
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
                owner = cookie[_COOKIE].value if _COOKIE in cookie else ""
                body = json.loads(self.body(8192) or b"{}")
                if not isinstance(body, dict):
                    raise GuardRefused("invalid_window")
                if self.path == GUARD_PATH + "/begin":
                    owner = owner or secrets.token_hex(32)
                    ttl = proxy.guard.begin(actor, owner, ttl_s=body.get("ttl_s", 120))
                    state = {"active": True, "expires_at": time.time() + ttl}
                    if proxy.application_window:
                        confirmed = self.application("begin", {"ttl_s": math.ceil(ttl)})
                        if confirmed.get("active") is not True or confirmed.get("guard") != "read_only_statements":
                            proxy.guard.end(actor, owner)
                            raise GuardRefused("application_window_unavailable")
                        state["guard"] = "read_only_statements"
                    self.reply(200, state, cookie=owner)
                elif self.path == GUARD_PATH + "/end":
                    if not owner:
                        raise GuardRefused("invalid_browser")
                    proxy.guard.end(actor, owner)
                    if proxy.application_window and not proxy.guard.protected(actor):
                        # The application's window also ends by its own clock.
                        self.application("end", {})
                    self.reply(200, {"active": proxy.guard.protected(actor)})
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
                if actor is None:
                    self.upstream_request(body)
                else:
                    with proxy.guard.request(actor, self.command, self.path):
                        self.upstream_request(body)

            def upstream_request(self, body):
                connect = http.client.HTTPSConnection if proxy.upstream.scheme == "https" else http.client.HTTPConnection
                connection = connect(proxy.upstream.hostname, proxy.upstream.port, timeout=proxy.timeout_s)
                headers = {key: self.headers[key] for key in ("Authorization", "tenant-id", "Content-Type", "Accept")
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
                    self.reply(401 if exc.code == "identity_required" else 403, {"error": exc.code})
                except (ValueError, json.JSONDecodeError):
                    self.reply(400, {"error": "invalid_request"})
                except Exception:
                    self.reply(503, {"error": "upstream_unavailable"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = dispatch
        return Handler


__all__ = ["NativeGuardProxy", "GUARD_PATH"]
