"""Local deployment contracts over temporary files and synthetic loopback servers."""
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

DIRECTORY = Path(__file__).resolve().parents[5] / "examples/yudao/deploy/local"
sys.path.insert(0, str(DIRECTORY))


def load(name):
    spec = importlib.util.spec_from_file_location(f"local_deploy_{name}", DIRECTORY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


command = load("command")
serve = load("serve")
cfg = command.cfg


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(cfg, "STATE", self.root).start()
        patch.object(cfg, "RUNTIME", self.root / "assistant.js").start()
        cfg.RUNTIME.write_bytes(b"synthetic-runtime")

    def test_no_arguments_start_the_configured_local_environment(self):
        with patch.object(command, "check", return_value=cfg.Configuration()) as check, \
                patch.object(command, "start") as start:
            self.assertEqual(0, command.main([]))
            check.assert_called_once()
            start.assert_called_once_with(cfg.Configuration(), restart=False)

    def test_check_does_not_start_stop_build_or_create_runtime_state(self):
        with patch.object(command, "check") as check, patch.object(command, "start") as start, \
                patch.object(command, "stop") as stop:
            before = set(self.root.iterdir())
            self.assertEqual(0, command.main(["check"]))
            check.assert_called_once()
            start.assert_not_called()
            stop.assert_not_called()
            self.assertEqual(before, set(self.root.iterdir()))

    def test_unchanged_healthy_start_does_not_restart_process(self):
        value = {"ready": True, "source_digest": "current", "runtime_sha256": hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()}
        with patch.object(command, "ensure_application"), patch.object(command, "build_assets"), \
                patch.object(command, "health", return_value=value), patch.object(cfg, "source_digest", return_value="current"), \
                patch.object(command, "status") as status, patch.object(command, "owned_pid") as pid:
            command.start(cfg.Configuration())
            status.assert_called_once()
            pid.assert_not_called()

    def test_build_failure_keeps_running_host_intact(self):
        with patch.object(command, "ensure_application"), \
                patch.object(command, "build_assets", side_effect=subprocess.CalledProcessError(1, "build")), \
                patch.object(command, "stop") as stop:
            with self.assertRaises(subprocess.CalledProcessError):
                command.start(cfg.Configuration(), restart=True)
            stop.assert_not_called()

    def test_unchanged_frontend_and_verified_runtime_do_not_run_a_build(self):
        marker = {"frontend": "same", "runtime": hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()}
        (self.root / "build.json").write_text(json.dumps(marker))
        with patch.object(cfg, "source_digest", return_value="same"), \
                patch.object(command.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            command.build_assets()
            run.assert_not_called()

    def test_python_changes_invalidate_host_but_not_frontend_or_secret_files(self):
        fake = self.root / "checkout"
        with patch.object(cfg, "ROOT", fake), patch.object(cfg, "HERE", fake / "examples/yudao/deploy/local"), \
                patch.object(cfg, "POLICIES", fake / "examples/yudao/deploy/cloud/native-policy"):
            for name in ("scripts/build_panel.py", "scripts/build_runtime.py",
                         "examples/yudao/deploy/cloud/yudao-loader.js", "src/web_operation_agent/example.py"):
                file = fake / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("synthetic input")
            frontend = cfg.source_digest(frontend_only=True)
            original = cfg.source_digest()
            (fake / "src/web_operation_agent/example.py").write_text("changed server")
            self.assertEqual(frontend, cfg.source_digest(frontend_only=True))
            self.assertNotEqual(original, cfg.source_digest())
            changed = cfg.source_digest()
            (fake / ".env").write_text("SYNTHETIC_SECRET=never-a-real-key")
            self.assertEqual(changed, cfg.source_digest())

    def test_stale_pid_cannot_terminate_an_unrelated_process(self):
        (self.root / "server.pid").write_text("12345")
        with patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "unrelated-service")), \
                patch.object(command.os, "kill") as kill, redirect_stdout(io.StringIO()):
            command.stop()
            kill.assert_not_called()

    def test_existing_volume_without_original_configuration_is_never_reset(self):
        with patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "existing_mysql\n")), \
                patch.object(cfg, "compose") as compose:
            with self.assertRaises(cfg.LocalError):
                command.ensure_application(cfg.Configuration())
            self.assertFalse((self.root / "application.env").exists())
            compose.assert_not_called()

    def test_running_application_is_reused_without_compose_up(self):
        (self.root / "application.env").touch()
        with patch.object(cfg, "compose", return_value="mysql\nredis\nserver\nweb") as compose, \
                patch.object(command, "application_ready", return_value=True), redirect_stdout(io.StringIO()):
            command.ensure_application(cfg.Configuration())
            self.assertEqual(1, compose.call_count)
            self.assertNotIn("up", compose.call_args.args)

    def test_http_200_with_authentication_error_is_not_application_readiness(self):
        with patch.object(command, "read_json", return_value={"code": 401}):
            self.assertFalse(command.application_ready())

    def test_html_success_page_is_not_a_health_result(self):
        with patch.object(command, "read_json", side_effect=cfg.LocalError("HTML")):
            self.assertIsNone(command.health())

    def test_status_rejects_stale_served_runtime(self):
        ready = MagicMock()
        ready.__enter__.return_value.read.return_value = b'{"ready":true}'
        asset = MagicMock()
        asset.__enter__.return_value.read.return_value = b"obsolete-runtime"
        with patch.object(command, "health", return_value={"ready": True, "source_digest": "same"}), \
                patch.object(command, "application_ready", return_value=True), \
                patch.object(cfg, "source_digest", return_value="same"), \
                patch.object(command.OPENER, "open", side_effect=[ready, asset]):
            with self.assertRaises(cfg.LocalError):
                command.status()

    def test_operator_logs_redact_configured_credentials_and_bearer_tokens(self):
        (self.root / "application.env").write_text("YUDAO_DB_PASSWORD=synthetic-local-password\n")
        text = command.log_text("database=synthetic-local-password Authorization: Bearer synthetic-token api_key=synthetic-key")
        for secret in ("synthetic-local-password", "synthetic-token", "synthetic-key"):
            self.assertNotIn(secret, text)


class FrontTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.finish_stream = threading.Event()
        finished = self.finish_stream

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                if self.path.endswith("/origin") or self.path == "/bootstrap":
                    body = json.dumps({"origin": self.headers.get("Origin")}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"data: first\n\n")
                    self.wfile.flush()
                    finished.wait(3)
                    return
                body = b'{"code":401,"msg":"synthetic login required"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        patch.object(cfg, "PORTS", (0, upstream.server_port, upstream.server_port, upstream.server_port)).start()
        patch.object(cfg, "source_digest", return_value="synthetic-source").start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        runtime = Path(self.tmp.name) / "assistant.js"
        runtime.write_bytes(b"synthetic-runtime")
        patch.object(cfg, "RUNTIME", runtime).start()
        self.front = serve.Front()
        threading.Thread(target=self.front.serve_forever, daemon=True).start()
        self.addCleanup(self.front.server_close)
        self.addCleanup(self.front.shutdown)
        self.addCleanup(finished.set)
        self.origin = f"http://127.0.0.1:{self.front.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def test_health_is_typed_and_fails_until_host_is_ready(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.origin + "/__local__/health")
        self.assertEqual(503, error.exception.code)
        self.front.ready = True
        with self.opener.open(self.origin + "/__local__/health") as response:
            self.assertTrue(json.load(response)["ready"])

    def test_upstream_authentication_status_and_body_are_preserved(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.origin + "/admin-api/protected", timeout=2)
        self.assertEqual(401, error.exception.code)
        self.assertEqual(401, json.load(error.exception)["code"])

    def test_stream_reaches_client_before_upstream_completes(self):
        with self.opener.open(self.origin + "/woa-agent/stream", timeout=1) as response:
            self.assertEqual(b"data: first\n", response.readline())
        self.assertFalse(self.finish_stream.is_set())

    def test_assistant_requests_without_origin_get_the_site_origin_as_on_the_server(self):
        def seen(path, origin=None):
            request = urllib.request.Request(self.origin + path, headers={"Origin": origin} if origin else {})
            with self.opener.open(request, timeout=2) as response:
                return json.load(response)["origin"]
        self.assertEqual(cfg.ORIGIN, seen("/woa-agent/v1/sessions/s/origin"))
        self.assertEqual(cfg.ORIGIN, seen("/woa-agent-boot/bootstrap"))
        self.assertEqual("https://elsewhere.test", seen("/woa-agent/v1/origin", "https://elsewhere.test"))
        self.assertIsNone(seen("/admin-api/origin"))

    def test_asset_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.origin + "/woa/../.env", timeout=2)
        self.assertEqual(404, error.exception.code)


if __name__ == "__main__":
    unittest.main()
