"""Same-origin local front composed with the product's host and native guard proxy."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import threading
from urllib.parse import urlsplit

import configuration as cfg

sys.path[:0] = [str(cfg.ROOT / "src"), str(cfg.ROOT / "examples/yudao/src")]
from web_operation_agent_yudao.host import native_server as native  # noqa: E402
from web_operation_agent_yudao.host.guard_proxy import GUARD_PATH  # noqa: E402

MAX_BODY = 20 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    environment: int
    checkout_id: str
    origin: str
    upstream: str = "http://web:80"
    state: Path | None = None
    ports: tuple[int, int, int, int] = cfg.PORTS
    listen_host: str = "127.0.0.1"


class Front(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__((settings.listen_host, settings.ports[0]), Handler)
        self.ready = False
        self.api_docs_loaded = False
        self.runtime_sha256 = hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()
        self.source_digest = cfg.source_digest()


class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.response_started = True
        super().end_headers()

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, *_args):
        pass  # Requests may carry authentication, business data or token query parameters.

    def reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def dispatch(self):
        self.response_started = False
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, http.client.HTTPException):
            if not self.response_started:
                self.reply(502, b'{"error":"local_upstream_unavailable"}')
            self.close_connection = True

    def route(self):
        settings = self.server.settings
        path = urlsplit(self.path).path
        if path == "/__local__/health":
            payload = {"schema_version": 2, "ready": self.server.ready,
                       "api_docs_loaded": self.server.api_docs_loaded,
                       "environment": settings.environment, "checkout_id": settings.checkout_id,
                       "runtime_sha256": self.server.runtime_sha256,
                       "source_digest": self.server.source_digest}
            self.reply(200 if self.server.ready else 503, json.dumps(payload).encode())
            return
        if (self.command in {"GET", "HEAD"} and self.headers.get("Host") == urlsplit(settings.origin).netloc.replace("localhost", "127.0.0.1")
                and "text/html" in self.headers.get("Accept", "")):
            self.send_response(302)
            self.send_header("Location", settings.origin + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/woa/") and not path.startswith(GUARD_PATH + "/"):
            name = path.removeprefix("/woa/")
            assets = {"yudao-loader.js": cfg.ROOT / "examples/yudao/deploy/cloud/yudao-loader.js",
                      **{n: cfg.ROOT / "frontend/dist/integrations/yudao" / n
                         for n in ("page-facts.js", "yudao-page-controller.js", "form-surface.js")}}
            policy = json.loads((cfg.ROOT / "src/web_operation_agent/policies/page_controller.json").read_text())
            assets.update({n: cfg.CACHE / "woa" / n for n in policy["served_files"]})
            file = assets.get(name)
            if file is None or self.command not in {"GET", "HEAD"}:
                self.reply(404, b'{"error":"not_found"}')
                return
            self.reply(200, file.read_bytes(), "text/plain" if name == "LICENSE" else "text/javascript")
            return
        target = self.path
        upstream = urlsplit(settings.upstream)
        hostname = "127.0.0.1"
        application = False
        if path.startswith("/admin-api/") or path.startswith(GUARD_PATH + "/"):
            port = settings.ports[3]
        elif path == "/woa-agent-boot/bootstrap":
            port, target = settings.ports[2], "/bootstrap"
        elif path.startswith("/woa-agent/"):
            port, target = settings.ports[1], self.path.removeprefix("/woa-agent")
        elif path.startswith(("/app-api/", "/infra/ws")):
            self.reply(403, b'{"error":"not_available_in_local_acceptance"}')
            return
        else:
            hostname, port, application = upstream.hostname, upstream.port or 80, True
        if self.headers.get("Transfer-Encoding"):
            self.reply(400, b'{"error":"unsupported_transfer"}')
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.reply(400, b'{"error":"invalid_length"}')
            return
        if not 0 <= size <= MAX_BODY:
            self.reply(413, b'{"error":"body_too_large"}')
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in {"host", "connection", "content-length", "accept-encoding"}}
        headers["Accept-Encoding"] = "identity"
        if (path == "/woa-agent-boot/bootstrap" or path.startswith("/woa-agent/")) and not self.headers.get("Origin"):
            # As the site's nginx does: a same-origin GET carries no Origin header, and the
            # assistant's gateway requires one. A request naming another origin keeps it.
            headers["Origin"] = settings.origin
        body = self.rfile.read(size)
        if len(body) != size:
            self.reply(400, b'{"error":"incomplete_body"}')
            return
        connection = http.client.HTTPConnection(hostname, port, timeout=60)
        try:
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "")
            if content_type.startswith("text/event-stream"):
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := response.read1(16384):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return
            body = response.read()
            if application and "text/html" in content_type:
                body = body.replace(b"<head>", b'<head><script type="module" src="/woa/page-facts.js"></script>'
                                    b'<script src="/woa/yudao-loader.js"></script>', 1)
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() in {"content-type", "set-cookie", "cache-control", "etag", "last-modified"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        finally:
            connection.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = dispatch


def warm_documents(host, front):
    # Cold document construction can exceed nginx's ordinary 120 s timeout.
    # This startup-only read uses the same local backend, without that proxy.
    try:
        with host._documents_lock:
            if host._api_documents is None:
                documents = native._api_docs("http://server:48080", wait_s=0, timeout_s=300)
                if isinstance(documents, dict):
                    host._api_documents = documents
        front.api_docs_loaded = host._api_documents is not None
    except (OSError, ValueError):
        pass


def host_config(settings: Settings, env: dict) -> native.YudaoHostConfig:
    """Build the normal local integration; env carries model settings and write/mode opt-in.

    Addresses, ports and state come from Settings; page admission and approved native
    submission are product defaults, shared with deployment, not caller overrides.
    """
    extra: dict[str, object] = {}
    if env.get("WOA_DAILY_ALLOWANCE_CNY"):
        extra["daily_allowance_cny"] = float(env["WOA_DAILY_ALLOWANCE_CNY"])
    if env.get("WOA_DAILY_TOTAL_CAP_CNY"):
        extra["daily_total_cap_cny"] = float(env["WOA_DAILY_TOTAL_CAP_CNY"])
    if env.get("WOA_MONTHLY_TOTAL_CAP_CNY"):
        extra["monthly_total_cap_cny"] = float(env["WOA_MONTHLY_TOTAL_CAP_CNY"])
    if env.get("WOA_MAX_SESSIONS"):
        extra["max_sessions"] = int(env["WOA_MAX_SESSIONS"])
    if env.get("WOA_TURN_TIMEOUT_S"):
        extra["turn_timeout_s"] = int(env["WOA_TURN_TIMEOUT_S"])
    return native.YudaoHostConfig(
        api_base=settings.upstream, public_origin=settings.origin,
        public_agent_url=settings.origin + "/woa-agent", state_dir=settings.state,
        gateway_host="127.0.0.1", gateway_port=settings.ports[1],
        bootstrap_host="127.0.0.1", bootstrap_port=settings.ports[2],
        native_guard_port=settings.ports[3], native_ingress_base=f"http://127.0.0.1:{settings.ports[0]}",
        ui_source=cfg.CACHE / "ui",
        source_root=cfg.CACHE if (cfg.CACHE / "server").is_dir() else None,
        source_release=cfg.RELEASE, native_read_policy=cfg.POLICIES / "fence.json",
        native_page_policy=cfg.POLICIES / "pages.json",
        native_money_policy=cfg.POLICIES / "verified-money.json",
        native_filter_semantics=cfg.POLICIES / "filters.json", **native.mode_write_options(env),
        local_dev_mode=True,
        model=env["AGENT_OPENAI_MODEL"], model_env=env,
        daily_allowance_cny=float(extra.pop("daily_allowance_cny", 20.0)),
        **extra)


def main():
    environment = int(os.environ["WOA_LOCAL_ENVIRONMENT"])
    keys = (
        "AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY", "AGENT_OPENAI_MODEL",
        "AGENT_PROVIDER", "WOA_MODE", "WOA_NATIVE_WRITES",
        "WOA_DAILY_ALLOWANCE_CNY", "WOA_DAILY_TOTAL_CAP_CNY",
        "WOA_MONTHLY_TOTAL_CAP_CNY", "WOA_MAX_SESSIONS", "WOA_TURN_TIMEOUT_S",
    )
    env = {key: os.environ[key] for key in keys if os.environ.get(key)}
    settings = Settings(environment, os.environ["WOA_LOCAL_CHECKOUT_ID"], os.environ["WOA_LOCAL_PUBLIC_ORIGIN"],
                        state=Path("/var/lib/woa-yudao"), listen_host="0.0.0.0")
    settings.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    host = boot = None

    def _on_stop(*_):
        if host is not None:
            host.begin_drain()
        stop.set()

    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    front = Front(settings)
    threading.Thread(target=front.serve_forever, daemon=True).start()
    try:
        host = native.YudaoHost(host_config(settings, env))
        host.server.start()
        boot = ThreadingHTTPServer(("127.0.0.1", settings.ports[2]), native._handler(host))
        threading.Thread(target=boot.serve_forever, daemon=True).start()
        threading.Thread(target=warm_documents, args=(host, front), daemon=True).start()
        front.ready = True
        print(f"环境{settings.environment}就绪：{settings.origin}", flush=True)
        while not stop.wait(60):
            host.forget_closed()
            host.cleanup()
    finally:
        front.ready = False
        if host is not None:
            host.drain()
        if boot:
            boot.shutdown()
            boot.server_close()
        if host:
            host.close()
        front.shutdown()
        front.server_close()


if __name__ == "__main__":
    main()
