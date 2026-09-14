from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "deploy" / "odoo-single-node" / "operations.py"
SPEC = importlib.util.spec_from_file_location("single_node_operations", MODULE_PATH)
OPS = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(OPS)
REGISTER_PATH = ROOT / "deploy" / "odoo-single-node" / "register.py"
CADDYFILE = ROOT / "deploy" / "odoo-single-node" / "Caddyfile"
REGISTER_SPEC = importlib.util.spec_from_file_location("single_node_register", REGISTER_PATH)
REGISTER = importlib.util.module_from_spec(REGISTER_SPEC)
assert REGISTER_SPEC is not None and REGISTER_SPEC.loader is not None
REGISTER_SPEC.loader.exec_module(REGISTER)


class SingleNodeOperationsTests(unittest.TestCase):
    def test_same_origin_gateway_gets_a_trusted_application_origin(self):
        config = CADDYFILE.read_text(encoding="utf-8")
        gateway = config.split("handle_path /assistant/* {", 1)[1].split(
            "\n\t}", 1)[0]
        self.assertIn("not header Origin *", gateway)
        self.assertIn(
            "request_header @same_origin_gateway Origin "
            "{$WOA_PUBLIC_ORIGIN:https://localhost:8443}",
            gateway,
        )
        self.assertNotIn("header_up Origin", gateway)

    def _bundle(self, root: Path):
        for relative in (
                "agent-state/manifest.json", "agent-state/session-store.sqlite3",
                "agent-state/mutation-journal.sqlite3",
                "agent-state/conversation-state.sqlite3", "odoo-database.pgdump",
                "odoo-data.tar.gz", "agent-state/agent-ledgers.tar.gz"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((relative + "\n").encode())
        manifest = OPS._manifest(root, project="woa-test-stack", created_at_ms=123)
        (root / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def test_manifest_covers_every_file_and_excludes_execution_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._bundle(root)
            actual = OPS.verify(root, expected_project="woa-test-stack")
        self.assertEqual(actual, expected)
        self.assertFalse(actual["execution_authority_included"])
        self.assertFalse(actual["bearer_sessions_included"])
        self.assertTrue(actual["credential_verifiers_included"])
        self.assertFalse(actual["secret_material_included"])

    def test_compose_project_environment_cannot_redirect_an_operation(self):
        with patch.dict(os.environ, {
                "COMPOSE_PROJECT_NAME": "woa-wrong-stack",
                "COMPOSE_PROFILES": "qualification"}):
            env = OPS._environment("woa-test-stack")
        self.assertNotIn("COMPOSE_PROJECT_NAME", env)
        self.assertNotIn("COMPOSE_PROFILES", env)
        with patch.object(OPS, "_run", return_value=b"") as run:
            OPS._compose(env, "stop", "agent")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-p") + 1], "woa-test-stack")

    def test_backup_excludes_browser_sessions_and_preserves_agent_ledgers(self):
        calls = []

        def compose(_env, *args, **_kwargs):
            calls.append(args)
            if args[:3] == ("exec", "-T", "db") and "pg_dump" in args:
                return b"database dump"
            if args and args[0] == "run" and "-v" in args:
                host = Path(args[args.index("-v") + 1].split(":", 1)[0])
                if "agent" in args:
                    state = host / "state"
                    state.mkdir()
                    for name in ("manifest.json", "session-store.sqlite3",
                                 "mutation-journal.sqlite3",
                                 "conversation-state.sqlite3"):
                        (state / name).write_text(name)
                    (host / "agent-ledgers.tar.gz").write_text("ledgers")
                elif "odoo" in args:
                    (host / "odoo-data.tar.gz").write_text("odoo data")
            return b""

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(OPS, "_assert_project_services"), \
                patch.object(OPS, "_compose", side_effect=compose):
            result = OPS.backup(
                project="woa-test-stack", destination=Path(tmp) / "backup")

        self.assertEqual(result["schema_version"], 2)
        scripts = "\n".join(str(item) for call in calls for item in call)
        self.assertIn("--exclude='./sessions'", scripts)
        self.assertIn("agent-ledgers.tar.gz", scripts)

    def test_restore_refuses_unresolved_state_and_failure_stays_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            bundle.mkdir()
            manifest = {"created_at_ms": 1}
            with patch.object(OPS, "verify", return_value=manifest), \
                    patch.object(OPS, "_assert_project_services"), \
                    patch.object(OPS, "_unresolved_mutations", return_value=1), \
                    patch.object(OPS, "backup") as backup:
                with self.assertRaisesRegex(OPS.OperationsError, "reconciliation"):
                    OPS.restore(project="woa-test-stack", bundle=bundle)
            backup.assert_not_called()

            calls = []

            def fail_after_stop(_env, *args, **_kwargs):
                calls.append(args)
                if args[:3] == ("exec", "-T", "db") and "dropdb" in args:
                    raise OPS.OperationsError("injected restore failure")
                return b""

            with patch.object(OPS, "verify", return_value=manifest), \
                    patch.object(OPS, "_assert_project_services"), \
                    patch.object(OPS, "_unresolved_mutations", return_value=0), \
                    patch.object(OPS, "_live_unresolved_mutations", return_value=0), \
                    patch.object(OPS, "backup"), \
                    patch.object(OPS, "_compose", side_effect=fail_after_stop):
                with self.assertRaisesRegex(OPS.OperationsError, "remain stopped"):
                    OPS.restore(project="woa-test-stack", bundle=bundle)
            self.assertFalse(any(call[:2] == ("up", "-d") for call in calls))

    def test_tamper_extra_file_and_wrong_project_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._bundle(root)
            with self.assertRaisesRegex(OPS.OperationsError, "different"):
                OPS.verify(root, expected_project="woa-other-stack")
            (root / "unexpected").write_text("x")
            with self.assertRaisesRegex(OPS.OperationsError, "undeclared"):
                OPS.verify(root, expected_project="woa-test-stack")
            (root / "unexpected").unlink()
            (root / "odoo-data.tar.gz").write_text("changed")
            with self.assertRaisesRegex(OPS.OperationsError, "verification"):
                OPS.verify(root, expected_project="woa-test-stack")

    def test_registration_copies_one_secret_and_updates_the_manifest_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            private.mkdir()
            (private / "delegations.json").write_text(
                '{"schema_version":1,"principals":{}}')
            key = root / "key.json"
            key.write_text(json.dumps({
                "database": "trial", "login": "reader@example.test",
                "api_key": "a" * 32,
            }))
            result = REGISTER.register(
                private_dir=private, actor_id="42", api_key_file=key,
                scopes=("customers:read",), allow_mutations=False)
            manifest = json.loads((private / "delegations.json").read_text())
            stored = private / result["credential_file"]

            self.assertEqual(result["principal"], "trial:42")
            self.assertEqual(manifest["principals"]["trial:42"]["scopes"],
                             ["customers:read"])
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("api_key", json.dumps(result))
            with self.assertRaisesRegex(REGISTER.RegistrationError, "already"):
                REGISTER.register(
                    private_dir=private, actor_id="42", api_key_file=key,
                    scopes=("customers:read",), allow_mutations=False)

    def test_write_scope_requires_the_matching_mutation_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "delegations.json").write_text(
                '{"schema_version":1,"principals":{}}')
            key = root / "key.json"
            key.write_text(json.dumps({
                "database": "trial", "login": "writer@example.test",
                "api_key": "a" * 32,
            }))
            with self.assertRaisesRegex(REGISTER.RegistrationError, "write scopes"):
                REGISTER.register(
                    private_dir=root, actor_id="7", api_key_file=key,
                    scopes=("customers:write",), allow_mutations=False)

    def test_restore_cli_failure_emits_only_a_content_free_alert(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def read(self, _limit):
                return b""

        def send(request, timeout):
            requests.append(json.loads(request.data))
            return Response()

        with patch.dict(os.environ, {"WOA_ALERT_WEBHOOK_URL":
                                     "http://alerts.test/events"}), \
                patch.object(OPS.urllib.request, "urlopen", side_effect=send), \
                redirect_stdout(io.StringIO()):
            status = OPS.main([
                "--project", "woa-test-stack", "restore", "/missing",
                "--confirm-project", "woa-different-stack",
            ])
        self.assertEqual(status, 2)
        self.assertEqual(requests, [{
            "schema_version": 1, "service": "web-operation-agent-odoo",
            "kind": "restore_failure", "observed_at_ms":
            requests[0]["observed_at_ms"],
        }])


if __name__ == "__main__":
    unittest.main()
