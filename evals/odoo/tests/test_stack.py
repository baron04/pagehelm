import http.server
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from evals.odoo.stack import AGENT_STATE_PROGRAM, Stack, StackError


class RecordingStack(Stack):
    def run(self, argv, **kwargs):
        self.__dict__.setdefault("calls", []).append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class StackTests(unittest.TestCase):
    def test_only_explicit_evaluation_projects_are_accepted(self):
        for project in ("woa-odoo-pilot", "woa-odoo-reference", "p74", "woa-p74-"):
            with self.subTest(project=project):
                with self.assertRaises(StackError):
                    Stack(private_dir=Path("/tmp/p74"), project=project)
        Stack(private_dir=Path("/tmp/p74"), project="woa-p74-odoo-v1")

    def test_compose_always_names_project_base_overlay_and_extras(self):
        stack = RecordingStack(private_dir=Path("/tmp/p74"),
                               extra_compose=(Path("/tmp/p74/mutation.yml"),))
        stack.compose("ps")
        argv = stack.calls[-1]
        self.assertEqual(argv[:4], ["docker", "compose", "-p", "woa-p74-odoo-v1"])
        self.assertTrue(argv[5].endswith("deploy/odoo-single-node/docker-compose.yml"))
        self.assertTrue(argv[7].endswith("evals/odoo/compose.p74.yml"))
        self.assertEqual(argv[8:10], ["-f", "/tmp/p74/mutation.yml"])
        env = stack.env()
        self.assertEqual(env["WOA_PROJECT_NAME"], "woa-p74-odoo-v1")
        self.assertNotIn("COMPOSE_PROJECT_NAME", env)

    def test_container_refuses_a_missing_or_ambiguous_service(self):
        stack = RecordingStack(private_dir=Path("/tmp/p74"))
        with self.assertRaises(StackError):
            stack.container("agent")

    def test_model_env_token_shape_is_enforced(self):
        stack = Stack(private_dir=Path("/tmp/p74"))
        with self.assertRaises(StackError):
            stack.write_model_env("short")


class _Metrics(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"active_sessions": 0, "unresolved_mutations": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class AgentStateProgramTests(unittest.TestCase):
    """The in-container program, run locally against a state directory and metrics stub."""

    def run_program(self, directory):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Metrics)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        program = (AGENT_STATE_PROGRAM.replace("/var/lib/woa", directory)
                   .replace("http://127.0.0.1:8767", f"http://127.0.0.1:{server.server_port}"))
        return subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                              timeout=60, check=False)

    def test_reports_prepared_digests_and_reads_a_missing_table_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = sqlite3.connect(Path(directory) / "mutation-journal.sqlite3")
            journal.execute("CREATE TABLE mutation_intents (proposal_digest TEXT, state TEXT)")
            journal.executemany("INSERT INTO mutation_intents VALUES (?, ?)", [
                ("b", "prepared"), ("a", "prepared"), ("c", "completed")])
            journal.commit()
            journal.close()
            conversations = sqlite3.connect(Path(directory) / "conversation-state.sqlite3")
            conversations.execute("CREATE TABLE unrelated (x)")
            conversations.close()
            completed = self.run_program(directory)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "active_sessions": 0, "unresolved_mutations": 1, "prepared_proposals": ["a", "b"],
            "conversation_rows": 0})

    def test_an_unreadable_journal_fails_instead_of_reading_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "mutation-journal.sqlite3").write_bytes(b"not a database" * 100)
            completed = self.run_program(directory)
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
