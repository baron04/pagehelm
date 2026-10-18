"""Same-origin local front composed with the product's host and native guard proxy."""
from __future__ import annotations

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import sys
import threading
from urllib.parse import urlsplit

import configuration as cfg

sys.path[:0] = [str(cfg.ROOT / "src"), str(cfg.ROOT / "examples/yudao/src")]
from web_operation_agent_yudao import native_server as native  # noqa: E402
from web_operation_agent_yudao.guard_proxy import GUARD_PATH  # noqa: E402

MAX_BODY = 20 * 1024 * 1024


class Front(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", cfg.PORTS[0]), Handler)
        self.ready = False
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
        path = urlsplit(self.path).path
        if path == "/__local__/health":
            payload = {"schema_version": 1, "ready": self.server.ready,
                       "checkout_id": cfg.checkout_id(), "pid": os.getpid(),
                       "runtime_sha256": self.server.runtime_sha256,
                       "source_digest": self.server.source_digest}
            self.reply(200 if self.server.ready else 503, json.dumps(payload).encode())
            return
        if (self.command in {"GET", "HEAD"} and self.headers.get("Host") == f"127.0.0.1:{cfg.PORTS[0]}"
                and "text/html" in self.headers.get("Accept", "")):
            self.send_response(302)
            self.send_header("Location", cfg.ORIGIN + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/woa/") and not path.startswith(GUARD_PATH + "/"):
            name = path.removeprefix("/woa/")
            assets = {"yudao-loader.js": cfg.ROOT / "examples/yudao/deploy/cloud/yudao-loader.js",
                      **{n: cfg.ROOT / "frontend/src/integrations/yudao" / n
                         for n in ("page-facts.js", "yudao-page-controller.js")}}
            policy = json.loads((cfg.ROOT / "src/web_operation_agent/policies/page_controller.json").read_text())
            assets.update({n: cfg.CACHE / "woa" / n for n in policy["served_files"]})
            file = assets.get(name)
            if file is None or self.command not in {"GET", "HEAD"}:
                self.reply(404, b'{"error":"not_found"}')
                return
            self.reply(200, file.read_bytes(), "text/plain" if name == "LICENSE" else "text/javascript")
            return
        target = self.path
        if path.startswith("/admin-api/") or path.startswith(GUARD_PATH + "/"):
            port = cfg.PORTS[3]
        elif path == "/woa-agent-boot/bootstrap":
            port, target = cfg.PORTS[2], "/bootstrap"
        elif path.startswith("/woa-agent/"):
            port, target = cfg.PORTS[1], self.path.removeprefix("/woa-agent")
        elif path.startswith(("/app-api/", "/infra/ws")):
            self.reply(403, b'{"error":"not_available_in_local_acceptance"}')
            return
        else:
            port = urlsplit(cfg.UPSTREAM).port
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
            headers["Origin"] = cfg.ORIGIN
        body = self.rfile.read(size)
        if len(body) != size:
            self.reply(400, b'{"error":"incomplete_body"}')
            return
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
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
            if port == urlsplit(cfg.UPSTREAM).port and "text/html" in content_type:
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


def main():
    os.environ.update(NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    env = cfg.load_model_environment()
    cfg.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    front = Front()
    threading.Thread(target=front.serve_forever, daemon=True).start()
    host = boot = None
    pidfile = cfg.STATE / "server.pid"
    try:
        host = native.YudaoHost(native.YudaoHostConfig(
            api_base=cfg.UPSTREAM, public_origin=cfg.ORIGIN,
            public_agent_url=cfg.ORIGIN + "/woa-agent", state_dir=cfg.STATE / "state",
            gateway_host="127.0.0.1", gateway_port=cfg.PORTS[1],
            bootstrap_host="127.0.0.1", bootstrap_port=cfg.PORTS[2],
            native_guard_port=cfg.PORTS[3], ui_source=cfg.CACHE / "ui",
            source_release=cfg.RELEASE, native_read_policy=cfg.POLICIES / "fence.json",
            native_page_policy=cfg.POLICIES / "pages.json",
            native_filter_semantics=cfg.POLICIES / "filters.json", native_writes=False,
            native_edit_prefill=False,
            model=env["AGENT_OPENAI_MODEL"], model_env=env, daily_allowance_cny=20.0))
        host.server.start()
        boot = ThreadingHTTPServer(("127.0.0.1", cfg.PORTS[2]), native._handler(host))
        threading.Thread(target=boot.serve_forever, daemon=True).start()
        threading.Thread(target=host._warm_api_docs, daemon=True).start()
        pidfile.write_text(str(os.getpid()))
        pidfile.chmod(0o600)
        front.ready = True
        print(f"Local acceptance ready: {cfg.ORIGIN}", flush=True)
        while not stop.wait(60):
            host.forget_closed()
    finally:
        front.ready = False
        if boot:
            boot.shutdown()
            boot.server_close()
        if host:
            host.close()
        front.shutdown()
        front.server_close()
        if pidfile.is_file() and pidfile.read_text().strip() == str(os.getpid()):
            pidfile.unlink()


if __name__ == "__main__":
    main()
