"""Offline config and mock-endpoint probe tests for `web_operation_agent.modeling.preflight`."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

from web_operation_agent.modeling.preflight import main, run_preflight


class _MockModelServer:
    def __init__(self, handler_fn):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                status, payload = outer._handler_fn(self.path, self.headers, body)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                return

        self._handler_fn = handler_fn
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return f"http://127.0.0.1:{self._httpd.server_port}/v1"

    def __exit__(self, *_args):
        self._httpd.shutdown()
        self._httpd.server_close()


class PreflightOfflineTests(unittest.TestCase):
    def test_offline_config_and_pricing_checks_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "synthetic-secret-token-value-123456"
            (root / ".env").write_text(f"DEEPSEEK_API_KEY={secret}\nWOA_READ_ONLY=true\n", encoding="utf-8")
            report = run_preflight(root=root, env_path=root / ".env")
            self.assertTrue(report["ok"])
            self.assertTrue(report["auth_configured"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["self_hosted"])
            self.assertIsNone(report["probe"])
            self.assertNotIn(secret, json.dumps(report))

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--env", str(root / ".env"), "--json"])
            self.assertEqual(rc, 0)
            self.assertNotIn(secret, buf.getvalue())

    def test_offline_ollama_and_unpriced_cloud_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("AGENT_PROVIDER=ollama\nAGENT_OPENAI_MODEL=qwen3:14b\n", encoding="utf-8")
            report = run_preflight(root=root, env_path=root / ".env")
            self.assertTrue(report["ok"])
            self.assertTrue(report["self_hosted"])
            self.assertFalse(report["auth_configured"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "OPENAI_API_KEY=synthetic-key\nAGENT_OPENAI_MODEL=unregistered-model\n",
                encoding="utf-8",
            )
            report = run_preflight(root=root, env_path=root / ".env")
            self.assertFalse(report["ok"])
            self.assertFalse(report["checks"]["pricing"]["ok"])

    def test_missing_explicit_files_and_invalid_timeout_fail_cleanly(self):
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_env = root / "missing.env"
            buf_env = io.StringIO()
            with redirect_stdout(buf_env):
                rc_env = main(["--env", str(missing_env), "--json"])
            self.assertEqual(rc_env, 1)
            rep_env = json.loads(buf_env.getvalue())
            self.assertFalse(rep_env["ok"])
            self.assertIn("env file not found", rep_env["checks"]["config"]["detail"])

            (root / ".env").write_text("DEEPSEEK_API_KEY=synthetic-key\n", encoding="utf-8")
            missing_cfg = root / "missing.toml"
            buf_cfg = io.StringIO()
            with redirect_stdout(buf_cfg):
                rc_cfg = main(["--env", str(root / ".env"), "--config", str(missing_cfg), "--json"])
            self.assertEqual(rc_cfg, 1)
            rep_cfg = json.loads(buf_cfg.getvalue())
            self.assertFalse(rep_cfg["ok"])
            self.assertIn("config file not found", rep_cfg["checks"]["config"]["detail"])

            for bad_timeout in ("0", "-1", "nan", "inf"):
                err = io.StringIO()
                with self.subTest(bad_timeout=bad_timeout), redirect_stderr(err), \
                        self.assertRaises(SystemExit) as ex:
                    main(["--timeout", bad_timeout])
                self.assertEqual(ex.exception.code, 2)


class PreflightProbeTests(unittest.TestCase):
    def _run_probe(self, handler_fn, *, api_key: str = ""):
        with _MockModelServer(handler_fn) as url, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                f"AGENT_OPENAI_API_KEY={api_key}\n" if api_key else "",
                encoding="utf-8",
            )
            (root / "pagehelm.toml").write_text(
                f'provider = "openai-compatible"\nbase_url = "{url}"\n'
                'model = "qwen3:14b"\nself_hosted = true\n',
                encoding="utf-8",
            )
            return run_preflight(
                root=root,
                toml_path=root / "pagehelm.toml",
                env_path=root / ".env",
                probe=True,
                timeout_s=5.0,
            )

    def test_probe_succeeds_with_usage_and_required_tool_call(self):
        seen_auth = []

        def handler(_path, headers, _body):
            seen_auth.append(headers.get("authorization"))
            return 200, {
                "usage": {"prompt_tokens": 18, "completion_tokens": 9},
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "preflight_ping", "arguments": '{"status":"ok"}'},
                        }],
                    },
                }],
            }

        report = self._run_probe(handler)
        self.assertTrue(report["ok"])
        self.assertEqual(seen_auth, [None])
        self.assertTrue(report["checks"]["endpoint_and_usage"]["ok"])
        self.assertTrue(report["checks"]["native_tool_calling"]["ok"])
        self.assertFalse(report["read_only"])
        self.assertEqual(report["probe"]["estimated_cost_cny"], 0.0)

    def test_probe_diagnoses_missing_usage_missing_tool_call_and_401(self):
        missing_usage = self._run_probe(lambda *_a: (200, {
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        }))
        self.assertFalse(missing_usage["ok"])
        self.assertEqual(missing_usage["probe"]["error_code"], "provider_usage")
        self.assertFalse(missing_usage["checks"]["endpoint_and_usage"]["ok"])

        no_tool = self._run_probe(lambda *_a: (200, {
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            "choices": [{"finish_reason": "stop", "message": {"content": "plain text"}}],
        }))
        self.assertFalse(no_tool["ok"])
        self.assertEqual(no_tool["probe"]["error_code"], "provider_tool_response")
        self.assertTrue(no_tool["checks"]["endpoint_and_usage"]["ok"])
        self.assertFalse(no_tool["checks"]["native_tool_calling"]["ok"])

        auth_fail = self._run_probe(lambda *_a: (401, {"error": "bad key"}), api_key="wrong")
        self.assertFalse(auth_fail["ok"])
        self.assertEqual(auth_fail["probe"]["error_code"], "provider_authentication")


if __name__ == "__main__":
    unittest.main()
