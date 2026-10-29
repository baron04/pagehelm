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
    sys.modules[spec.name] = module
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
        patch.object(cfg, "STATE_ROOT", self.root / "instances").start()
        patch.object(cfg, "RUNTIME", self.root / "assistant.js").start()
        cfg.RUNTIME.write_bytes(b"synthetic-runtime")
        self.config = cfg.Configuration()
        self.config.state.mkdir(parents=True)

    def test_running_clock_reuses_one_offset_and_is_local_only(self):
        library = self.root / "libfaketimeMT.so.1"
        library.write_bytes(b"\x7fELFsynthetic")
        with patch.object(command.test_clock.time, "time_ns", return_value=1_800_000_000_000_000_000):
            clock = command.test_clock.settings("2026-10-25T09:00:00+08:00", str(library))
        with patch.object(command.test_clock.time, "time_ns", side_effect=AssertionError("must not reset")):
            self.assertEqual(clock, command.test_clock.settings(clock["target"], str(library), clock))
        services = command.test_clock.composition(clock)
        self.assertEqual({"mysql", "server"}, set(services))
        self.assertEqual("mysql", services["mysql"]["user"])
        for service in services.values():
            self.assertEqual("1", service["environment"]["FAKETIME_DONT_FAKE_MONOTONIC"])
            self.assertTrue(service["volumes"][0]["read_only"])
        for service in command.test_clock.composition(None).values():
            self.assertNotIn("environment", service)
            self.assertNotIn("volumes", service)
            self.assertNotIn("user", service)

    def test_clock_rejects_unzoned_time_and_non_library_before_start(self):
        library = self.root / "library"
        library.write_bytes(b"not an ELF")
        for target in ("2026-10-25", "2026-10-25T09:00:00Z", "2026-10-25T09:00:00+08:00"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                command.test_clock.settings(target, str(library))

    def test_clock_off_clears_private_setting_and_compose_injection(self):
        (self.config.state / "application.env").write_text("YUDAO_DB_PASSWORD=synthetic")
        (self.config.state / "test-clock.json").write_text('{"offset_ms":-100}')
        with patch.object(cfg, "load_model_environment", return_value={"AGENT_OPENAI_BASE_URL":"http://model.invalid", "AGENT_OPENAI_API_KEY":"synthetic"}):
            command.prepare(self.config)
        self.assertFalse((self.config.state / "test-clock.json").exists())
        services = json.loads((self.config.state / "source.compose.json").read_text())["services"]
        self.assertNotIn("environment", services["mysql"])
        self.assertNotIn("volumes", services["server"])

    def current(self, config=None):
        config = config or self.config
        return {"running": True, "image": cfg.image_name(), "ports": {"48163/tcp": [{"HostPort": str(config.port)}]}, "labels": {
            "org.web-operation-agent.checkout": cfg.checkout_id(),
            "org.web-operation-agent.environment": str(config.number)}}

    def test_default_and_explicit_environment_commands(self):
        for argv, number in (([], 1), (["--env", "2"], 2), (["restart", "--env", "2"], 2)):
            with self.subTest(argv=argv), patch.object(command, "check") as check, patch.object(command, "start") as start:
                self.assertEqual(0, command.main(argv))
                check.assert_called_once_with(cfg.Configuration(number))
                start.assert_called_once_with(cfg.Configuration(number), restart="restart" in argv)

    def test_environment_isolation_and_fixed_internal_ports(self):
        a, b = cfg.Configuration(), cfg.Configuration(2)
        self.assertEqual((40001, 40002), (a.port, b.port))
        self.assertNotEqual(a.project, b.project)
        self.assertNotEqual(a.state, b.state)
        self.assertEqual((48163, 48164, 48165, 48166), cfg.PORTS)
        self.assertEqual("http://localhost:40002", cfg.process_environment(b)["LOCAL_PUBLIC_ORIGIN"])
        for number in (0, 3, True):
            with self.assertRaises(cfg.LocalError):
                cfg.Configuration(number)

    def test_check_does_not_create_runtime_state(self):
        with patch.object(command, "check") as check:
            before = set(self.root.rglob("*"))
            self.assertEqual(0, command.main(["--env", "2", "check"]))
            check.assert_called_once_with(cfg.Configuration(2))
            self.assertEqual(before, set(self.root.rglob("*")))

    def test_mounts_follow_the_launcher_directory_and_exclude_private_sources(self):
        mounts = cfg.source_mounts(self.config)
        self.assertIn(str(cfg.ROOT / "src"), [m["source"] for m in mounts])
        for mount in mounts:
            source = Path(mount["source"])
            self.assertNotIn(source.name, {".env", ".workbench", "node_modules", "test-results"})
            if mount["target"] != "/var/lib/woa-yudao":
                self.assertTrue(mount["read_only"])
                self.assertTrue(source.is_relative_to(cfg.ROOT))
        a, b = cfg.source_mounts(self.config), cfg.source_mounts(cfg.Configuration(2))
        self.assertNotEqual(a[-1]["source"], b[-1]["source"])

    def test_existing_volume_without_original_configuration_is_never_reset(self):
        with patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "existing_volume\n")), \
                patch.object(cfg, "load_model_environment") as model:
            with self.assertRaises(cfg.LocalError):
                command.prepare(self.config)
            self.assertFalse((self.config.state / "application.env").exists())
            model.assert_not_called()

    def test_configuration_files_are_private_and_host_model_url_is_translated(self):
        values = {"AGENT_OPENAI_BASE_URL": "http://localhost:12345/v1", "AGENT_OPENAI_API_KEY": "synthetic-key-$#", "AGENT_OPENAI_MODEL": "synthetic-model"}
        with patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")), \
                patch.object(cfg, "load_model_environment", return_value=values):
            self.assertTrue(command.prepare(self.config))
            self.assertFalse(command.prepare(self.config))
        text = (self.config.state / "agent.env").read_text()
        self.assertIn("http://host.docker.internal:12345/v1", text)
        self.assertIn("synthetic-key-$#", text)
        self.assertEqual(0o600, (self.config.state / "agent.env").stat().st_mode & 0o777)
        self.assertIn("/workspace/src", (self.config.state / "source.compose.json").read_text())

    def test_foreign_checkout_cannot_be_stopped_or_restarted(self):
        current = self.current()
        current["labels"]["org.web-operation-agent.checkout"] = "foreign-checkout"
        with patch.object(command, "container", return_value=current), patch.object(cfg, "compose") as compose, \
                patch.object(command.assets, "build_frontend") as build:
            for action in (command.stop, command.start):
                with self.assertRaises(cfg.LocalError):
                    action(self.config)
            compose.assert_not_called()
            build.assert_not_called()

    def test_failed_update_does_not_mark_new_model_configuration_as_applied(self):
        model = {"AGENT_OPENAI_BASE_URL": "https://model.invalid/v1", "AGENT_OPENAI_API_KEY": "synthetic-old-key", "AGENT_OPENAI_MODEL": "synthetic-model"}
        with patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")), \
                patch.object(cfg, "load_model_environment", side_effect=lambda: dict(model)):
            command.prepare(self.config)
            def applied():
                return json.loads((self.config.state / "source.compose.json").read_text())["services"]["agent"]["labels"]["org.web-operation-agent.deployment"]
            current = self.current()
            current["labels"]["org.web-operation-agent.deployment"] = applied()
            self.assertFalse(command.prepare(self.config, current))
            model["AGENT_OPENAI_API_KEY"] = "synthetic-new-key"
            self.assertTrue(command.prepare(self.config, current))
            # The next attempt must still apply it when the first never reached a new container.
            self.assertTrue(command.prepare(self.config, current))
            current["labels"]["org.web-operation-agent.deployment"] = applied()
            self.assertFalse(command.prepare(self.config, current))

    def test_stop_targets_only_selected_assistant(self):
        config = cfg.Configuration(2)
        with patch.object(command, "container", return_value=self.current(config)), patch.object(cfg, "compose") as compose, \
                redirect_stdout(io.StringIO()):
            command.stop(config)
        compose.assert_called_once_with(config, "stop", "agent")

    def test_healthy_unchanged_start_does_not_restart(self):
        value = {"ready": True, "source_digest": "same", "runtime_sha256": hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()}
        with patch.object(command, "container", return_value=self.current()), patch.object(command, "prepare", return_value=False), \
                patch.object(command.assets, "build_frontend"), patch.object(command.assets, "ensure_image"), \
                patch.object(command, "ensure_application"), patch.object(command, "health", return_value=value), \
                patch.object(cfg, "source_digest", return_value="same"), patch.object(command, "status") as status, \
                patch.object(cfg, "compose") as compose:
            command.start(self.config)
            status.assert_called_once_with(self.config)
            compose.assert_not_called()

    def test_source_update_only_restarts_selected_assistant(self):
        config = cfg.Configuration(2)
        with patch.object(command, "container", return_value=self.current(config)), patch.object(command, "prepare", return_value=False), \
                patch.object(command.assets, "build_frontend"), patch.object(command.assets, "ensure_image"), \
                patch.object(command, "ensure_application"), patch.object(command, "health", side_effect=[None, {"ready": True}]), \
                patch.object(command, "status"), patch.object(cfg, "compose") as compose:
            command.start(config)
        compose.assert_called_once_with(config, "restart", "agent")

    def test_deployment_change_recreates_only_selected_assistant(self):
        config = cfg.Configuration(2)
        with patch.object(command, "container", return_value=self.current(config)), patch.object(command, "prepare", return_value=True), \
                patch.object(command.assets, "build_frontend"), patch.object(command.assets, "ensure_image"), \
                patch.object(command, "ensure_application"), patch.object(command, "health", side_effect=[None, {"ready": True}]), \
                patch.object(command, "status"), patch.object(cfg, "compose") as compose:
            command.start(config)
        compose.assert_called_once_with(config, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "agent")

    def test_port_change_does_not_replace_a_working_container_when_port_is_taken(self):
        current = self.current()
        current["ports"]["48163/tcp"][0]["HostPort"] = "49999"
        with patch.object(command, "container", return_value=current), patch.object(command.socket, "socket") as socket, \
                patch.object(cfg, "compose") as compose:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaises(cfg.LocalError):
                command.start(self.config)
            compose.assert_not_called()

    def test_build_failure_keeps_running_host_intact(self):
        with patch.object(command, "container", return_value=self.current()), \
                patch.object(command.assets, "build_frontend", side_effect=subprocess.CalledProcessError(1, "build")), \
                patch.object(cfg, "compose") as compose:
            with self.assertRaises(subprocess.CalledProcessError):
                command.start(self.config, restart=True)
            compose.assert_not_called()

    def test_unchanged_frontend_reuses_legacy_receipt_without_overwriting_it(self):
        directory = self.root / ".q0-cache/yudao-local"
        directory.mkdir(parents=True)
        receipt = json.dumps({"frontend": "same", "runtime": hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()})
        (directory / "build.json").write_text(receipt)
        with patch.object(cfg, "ROOT", self.root), patch.object(cfg, "source_digest", return_value="same"), \
                patch.object(command.assets.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            command.assets.build_frontend()
            command.assets.build_frontend()
            run.assert_not_called()
        self.assertEqual(receipt, (directory / "build.json").read_text())
        self.assertEqual(json.loads(receipt), json.loads((directory / "compose-build.json").read_text()))

    def test_clock_change_quiesces_assistant_before_recreating_application(self):
        (self.config.state / "source.compose.json").write_text(json.dumps({"services": command.test_clock.composition(None)}))
        with patch.object(cfg, "compose", return_value="mysql\nredis\nserver\nweb\nagent") as compose, \
                patch.object(command, "application_ready", return_value=True), \
                patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "old-offset\nold-offset\n")):
            command.ensure_application(self.config)
        self.assertEqual([("ps", "--services", "--status", "running"), ("stop", "agent"),
            ("up", "-d", "--no-build", "mysql", "redis", "server", "web")],
            [c.args[1:] for c in compose.call_args_list])

    def test_healthy_application_does_not_run_compose_up(self):
        (self.config.state / "source.compose.json").write_text(json.dumps({"services": command.test_clock.composition(None)}))
        with patch.object(cfg, "compose", return_value="mysql\nredis\nserver\nweb\nagent") as compose, \
                patch.object(command, "application_ready", return_value=True), \
                patch.object(command.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "<no value>\n<no value>\n")), redirect_stdout(io.StringIO()):
            command.ensure_application(self.config)
        compose.assert_called_once_with(self.config, "ps", "--services", "--status", "running", capture=True)

    def test_health_rejects_other_environment_and_legacy_schema(self):
        for data in ({"schema_version": 1, "checkout_id": cfg.checkout_id()},
                     {"schema_version": 2, "environment": 2, "checkout_id": cfg.checkout_id()}):
            with patch.object(command, "read_json", return_value=data):
                self.assertIsNone(command.health(self.config))

    def test_authentication_error_is_not_application_readiness(self):
        with patch.object(command, "read_json", return_value={"code": 401}):
            self.assertFalse(command.application_ready(self.config))
        with patch.object(command, "read_json", side_effect=cfg.LocalError("HTML")):
            self.assertIsNone(command.health(self.config))

    def test_status_rejects_stale_served_runtime(self):
        asset = MagicMock()
        asset.__enter__.return_value.read.return_value = b"obsolete-runtime"
        with patch.object(command, "health", return_value={"ready": True, "source_digest": "same"}), \
                patch.object(command, "application_ready", return_value=True), patch.object(command, "read_json", return_value={"ready": True}), \
                patch.object(cfg, "source_digest", return_value="same"), patch.object(command.OPENER, "open", return_value=asset):
            with self.assertRaises(cfg.LocalError):
                command.status(self.config)

    def test_operator_logs_redact_credentials_and_bearer_tokens(self):
        (self.config.state / "application.env").write_text("YUDAO_DB_PASSWORD=synthetic-local-password\n")
        text = command.log_text("database=synthetic-local-password Authorization: Bearer synthetic-token api_key=synthetic-key", self.config)
        for secret in ("synthetic-local-password", "synthetic-token", "synthetic-key"):
            self.assertNotIn(secret, text)

    def test_source_changes_do_not_rebuild_dependency_image(self):
        fake = self.root / "checkout"
        local = fake / "examples/yudao/deploy/local"
        local.mkdir(parents=True)
        for name in ("agent.Dockerfile", "agent.Dockerfile.dockerignore"):
            (local / name).write_text("synthetic dependency input")
        (fake / "requirements.lock").write_text("synthetic closure")
        (fake / "src").mkdir()
        with patch.object(cfg, "ROOT", fake), patch.object(cfg, "HERE", local):
            before = cfg.dependency_digest()
            (fake / "src/example.py").write_text("changed source")
            self.assertEqual(before, cfg.dependency_digest())
            (fake / "requirements.lock").write_text("changed closure")
            self.assertNotEqual(before, cfg.dependency_digest())

    def test_python_changes_invalidate_host_but_not_frontend_or_secret_files(self):
        fake = self.root / "checkout"
        with patch.object(cfg, "ROOT", fake), patch.object(cfg, "HERE", fake / "examples/yudao/deploy/local"), \
                patch.object(cfg, "POLICIES", fake / "examples/yudao/deploy/cloud/native-policy"):
            for name in ("scripts/build_panel.py", "scripts/build_runtime.py", "requirements.lock",
                         "examples/yudao/deploy/local/agent.Dockerfile", "examples/yudao/deploy/local/agent.Dockerfile.dockerignore",
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
        self.public_origin = "http://localhost:40002"
        settings = serve.Settings(2, "synthetic-checkout", self.public_origin,
            upstream=f"http://127.0.0.1:{upstream.server_port}",
            ports=(0, upstream.server_port, upstream.server_port, upstream.server_port))
        self.front = serve.Front(settings)
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
            payload = json.load(response)
        self.assertTrue(payload["ready"])
        self.assertEqual((2, 2, "synthetic-checkout"),
                         (payload["schema_version"], payload["environment"], payload["checkout_id"]))

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
        self.assertEqual(self.public_origin, seen("/woa-agent/v1/sessions/s/origin"))
        self.assertEqual(self.public_origin, seen("/woa-agent-boot/bootstrap"))
        self.assertEqual("https://elsewhere.test", seen("/woa-agent/v1/origin", "https://elsewhere.test"))
        self.assertIsNone(seen("/admin-api/origin"))

    def test_asset_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.origin + "/woa/../.env", timeout=2)
        self.assertEqual(404, error.exception.code)


if __name__ == "__main__":
    unittest.main()
