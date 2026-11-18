"""Disposable SQL bytes and child processes only; no Docker/application/model access."""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
spec = importlib.util.spec_from_file_location("yudao_snapshot", DEPLOY / "snapshot.py")
assert spec is not None and spec.loader is not None
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class _Docker:
    project = "isolated-synthetic"
    identity = "a" * 64
    redis_identity = "c" * 64

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[str] = []
        self.dump = b"SELECT 1; -- synthetic-private\n"
        self.export_code = 0
        self.import_code = 0
        self.clear_tokens_code = 0
        self.redis_flush_code = 0
        self.server_running = False
        self.agent_running = False
        self.redis_running = True

    def mysql(self):
        self.calls.append("mysql")
        return self.identity

    def redis(self):
        self.calls.append("redis")
        if not self.redis_running:
            raise snapshot.SnapshotRefused("exactly_one_running_project_redis_required")
        return self.redis_identity

    def containers(self, service):
        self.calls.append(service)
        if service == "server":
            return ["b" * 64] if self.server_running else []
        if service == "agent":
            return ["d" * 64] if self.agent_running else []
        if service == "redis":
            return [self.redis_identity] if self.redis_running else []
        return []

    def command(self, identity, *, export):
        self.calls.append("export" if export else "import")
        if export:
            return [
                sys.executable,
                "-c",
                f"import sys;sys.stdout.buffer.write({self.dump!r});sys.exit({self.export_code})",
            ]
        return [
            sys.executable,
            "-c",
            "import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read());"
            "sys.exit(int(sys.argv[2]))",
            str(self.root / "received.sql"),
            str(self.import_code),
        ]

    def clear_oauth2_tokens_command(self, identity):
        self.calls.append("clear_oauth2_tokens")
        return [
            sys.executable,
            "-c",
            f"import pathlib;pathlib.Path({str(self.root / 'tokens_cleared')!r}).write_text('1');"
            f"raise SystemExit({self.clear_tokens_code})",
        ]

    def flush_redis_command(self, identity):
        self.calls.append("flush_redis")
        return [
            sys.executable,
            "-c",
            f"import pathlib;pathlib.Path({str(self.root / 'redis_flushed')!r}).write_text('1');"
            f"raise SystemExit({self.redis_flush_code})",
        ]


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.docker = _Docker(self.root)
        self.path = self.root / "backup.sql.gz"

    def restore(self, **changes):
        return snapshot.restore_snapshot(
            self.docker,
            self.path,
            **{
                "expect": snapshot._digest(self.path),
                "writer_isolated": True,
                "apply": True,
                **changes,
            },
        )

    def test_explicit_local_target_and_exact_project_labels_are_required(self):
        for host, project in [
            ("tcp://host:2375", "test"),
            ("unix:///tmp/socket", "../other"),
            ("unix:///tmp/socket", ""),
            ("default", "test"),
        ]:
            with self.assertRaises(snapshot.SnapshotRefused):
                snapshot.Docker(host, project)
        docker = snapshot.Docker("unix:///tmp/socket", "synthetic")
        for ids in [b"", b"a" * 64 + b"\n" + b"b" * 64 + b"\n", b"bad-id\n"]:
            with patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ids)
            ) as run:
                with self.assertRaises(snapshot.SnapshotRefused):
                    docker.mysql()
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["docker", "--host", "unix:///tmp/socket"])
            self.assertIn("label=com.docker.compose.project=synthetic", command)
            self.assertIn("label=com.docker.compose.service=mysql", command)
            with patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ids)
            ) as run_redis:
                with self.assertRaises(snapshot.SnapshotRefused):
                    docker.redis()
            self.assertIn("label=com.docker.compose.service=redis", run_redis.call_args.args[0])

    def test_export_round_trip_is_private_and_existing_file_is_never_overwritten(self):
        receipt = snapshot.export_snapshot(self.docker, self.path)
        self.assertEqual(gzip.decompress(self.path.read_bytes()), self.docker.dump)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt["sha256"], snapshot._digest(self.path))
        before = self.path.read_bytes()
        with self.assertRaises(snapshot.SnapshotRefused):
            snapshot.export_snapshot(self.docker, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".snapshot-*")))

    def test_dump_and_token_and_redis_commands_keep_credentials_inside_container(self):
        docker = snapshot.Docker("unix:///tmp/socket", "synthetic")
        command = docker.command("a" * 64, export=True)
        self.assertIn("--set-gtid-purged=OFF", command[-1])
        self.assertIn('test -n "${MYSQL_ROOT_PASSWORD:-}"', command[-1])
        self.assertIn("--single-transaction", command[-1])
        self.assertEqual(command[5], "a" * 64)

        clear_cmd = docker.clear_oauth2_tokens_command("a" * 64)
        self.assertIn("DELETE FROM system_oauth2_access_token", clear_cmd[-1])
        self.assertIn("DELETE FROM system_oauth2_refresh_token", clear_cmd[-1])
        self.assertIn('test -n "${MYSQL_ROOT_PASSWORD:-}"', clear_cmd[-1])

        redis_cmd = docker.flush_redis_command("c" * 64)
        self.assertEqual(redis_cmd[-3:], ["c" * 64, "redis-cli", "FLUSHDB"])

    def test_failed_partial_and_empty_dumps_never_publish_a_backup(self):
        for payload, code in [(b"partial SQL; synthetic-private", 42), (b"", 0)]:
            self.docker.dump = payload
            self.docker.export_code = code
            with self.assertRaises(snapshot.SnapshotRefused):
                snapshot.export_snapshot(self.docker, self.path)
            self.assertFalse(self.path.exists())
            self.assertFalse(list(self.root.glob(".snapshot-*")))

    def test_export_refuses_shared_or_linked_destination_directory(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        with self.assertRaises(snapshot.SnapshotRefused):
            snapshot.export_snapshot(self.docker, shared / "backup")
        alias = self.root / "alias"
        alias.symlink_to(self.root)
        with self.assertRaises(snapshot.SnapshotRefused):
            snapshot.export_snapshot(self.docker, alias / "backup")

    def test_restore_requires_exact_reviewed_bytes_and_isolation_before_any_import(self):
        self.path.write_bytes(gzip.compress(self.docker.dump))
        for changes in [{"expect": "0" * 64}, {"apply": False}, {"writer_isolated": False}]:
            with self.assertRaises(snapshot.SnapshotRefused):
                self.restore(**changes)
            self.assertEqual(self.docker.calls, [])
        self.assertFalse((self.root / "received.sql").exists())

    def test_corrupt_truncated_and_empty_gzip_fail_before_any_database_access(self):
        for data in [
            b"not gzip synthetic-private",
            gzip.compress(self.docker.dump)[:-4],
            gzip.compress(b""),
        ]:
            self.path.write_bytes(data)
            with self.assertRaises(snapshot.SnapshotRefused):
                self.restore()
            self.assertEqual(self.docker.calls, [])

    def test_linked_and_special_restore_sources_fail_without_blocking_or_import(self):
        source = self.root / "ordinary"
        source.write_bytes(gzip.compress(self.docker.dump))
        for kind in ["symbolic", "hard", "fifo"]:
            if kind == "symbolic":
                self.path.symlink_to(source)
            elif kind == "hard":
                os.link(source, self.path)
            else:
                os.mkfifo(self.path)
            try:
                with self.assertRaises((OSError, snapshot.SnapshotRefused)):
                    snapshot.restore_snapshot(
                        self.docker,
                        self.path,
                        expect="0" * 64,
                        writer_isolated=True,
                        apply=True,
                    )
                self.assertEqual(self.docker.calls, [])
            finally:
                self.path.unlink()

    def test_running_server_or_agent_blocks_restore_and_tool_never_stops_or_restarts_it(self):
        self.path.write_bytes(gzip.compress(self.docker.dump))
        self.docker.server_running = True
        with self.assertRaises(snapshot.SnapshotRefused):
            self.restore()
        self.assertEqual(self.docker.calls, ["mysql", "redis", "server"])
        self.assertFalse((self.root / "received.sql").exists())

        self.docker.calls.clear()
        self.docker.server_running = False
        self.docker.agent_running = True
        with self.assertRaises(snapshot.SnapshotRefused):
            self.restore()
        self.assertEqual(self.docker.calls, ["mysql", "redis", "server", "agent"])
        self.assertFalse((self.root / "received.sql").exists())

    def test_success_imports_clears_oauth2_tokens_and_flushes_redis_once(self):
        self.path.write_bytes(gzip.compress(self.docker.dump))
        result = self.restore()
        self.assertEqual((self.root / "received.sql").read_bytes(), self.docker.dump)
        self.assertTrue((self.root / "tokens_cleared").is_file())
        self.assertTrue((self.root / "redis_flushed").is_file())
        self.assertEqual(
            self.docker.calls,
            ["mysql", "redis", "server", "agent", "import", "clear_oauth2_tokens", "flush_redis"],
        )
        self.assertTrue(result["oauth2_tokens_cleared"])
        self.assertTrue(result["redis_flushed"])
        self.assertFalse(result["application_started"])
        self.assertFalse(result["application_postconditions_verified"])
        self.assertNotIn("synthetic-private", json.dumps(result))

    def test_partial_import_or_token_clear_or_redis_flush_failure_never_retries(self):
        self.path.write_bytes(gzip.compress(self.docker.dump))
        self.docker.import_code = 42
        with self.assertRaisesRegex(
            snapshot.SnapshotRefused, "^import_failed_or_indeterminate_keep_entry_closed$"
        ):
            self.restore()
        self.assertEqual(self.docker.calls.count("import"), 1)
        self.assertEqual((self.root / "received.sql").read_bytes(), self.docker.dump)
        self.assertTrue(self.path.is_file())

        self.docker.calls.clear()
        self.docker.import_code = 0
        self.docker.clear_tokens_code = 1
        with self.assertRaisesRegex(
            snapshot.SnapshotRefused, "^oauth2_token_clear_failed_keep_entry_closed$"
        ):
            self.restore()
        self.assertEqual(self.docker.calls.count("clear_oauth2_tokens"), 1)

        self.docker.calls.clear()
        self.docker.clear_tokens_code = 0
        self.docker.redis_flush_code = 1
        with self.assertRaisesRegex(
            snapshot.SnapshotRefused, "^redis_cache_flush_failed_keep_entry_closed$"
        ):
            self.restore()
        self.assertEqual(self.docker.calls.count("flush_redis"), 1)

    def test_cli_failure_receipt_does_not_echo_private_subprocess_errors(self):
        out = io.StringIO()
        with (
            patch.object(snapshot, "Docker", return_value=self.docker),
            patch.object(self.docker, "mysql", side_effect=OSError("synthetic-private")),
            redirect_stdout(out),
        ):
            result = snapshot.main(
                ["--docker-host", "unix:///tmp/socket", "--project", "synthetic", "export", str(self.path)]
            )
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(out.getvalue())["reason"], "snapshot_operation_failed")
        self.assertNotIn("synthetic-private", out.getvalue())

    def test_deploy_readme_has_no_dead_maintenance_doc_link(self):
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("public-demo-maintenance.md", readme)


if __name__ == "__main__":
    unittest.main()
