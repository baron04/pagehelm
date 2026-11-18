#!/usr/bin/env python3
"""Explicit local Docker target; verified export and offline, one-shot MySQL import."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zlib
from pathlib import Path

DATABASE = "ruoyi-vue-pro"
_CLEAR_OAUTH2_SQL = (
    "DELETE FROM system_oauth2_access_token; "
    "DELETE FROM system_oauth2_refresh_token;"
)


class SnapshotRefused(RuntimeError):
    """Fixed operator reason; never expose Docker errors, SQL or credentials."""


class Docker:
    def __init__(self, host: str, project: str):
        if not re.fullmatch(r"unix:///[^\s]+", host):
            raise SnapshotRefused("explicit_local_docker_socket_required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
            raise SnapshotRefused("invalid_project")
        self.prefix = ["docker", "--host", host]
        self.project = project

    def containers(self, service: str) -> list[str]:
        result = subprocess.run(
            [
                *self.prefix,
                "ps",
                "--filter",
                "status=running",
                "--filter",
                "label=com.docker.compose.project=" + self.project,
                "--filter",
                "label=com.docker.compose.service=" + service,
                "--no-trunc",
                "--format",
                "{{.ID}}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise SnapshotRefused("docker_inventory_failed")
        ids = result.stdout.decode("ascii").splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{64}", identity) for identity in ids):
            raise SnapshotRefused("invalid_container_identity")
        return ids

    def mysql(self) -> str:
        ids = self.containers("mysql")
        if len(ids) != 1:
            raise SnapshotRefused("exactly_one_running_project_mysql_required")
        return ids[0]

    def redis(self) -> str:
        ids = self.containers("redis")
        if len(ids) != 1:
            raise SnapshotRefused("exactly_one_running_project_redis_required")
        return ids[0]

    def command(self, identity: str, *, export: bool) -> list[str]:
        # The password stays inside the selected container. Never inspect its env
        # or put a password in the host command line, receipt or error output.
        command = (
            'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" '
            "--single-transaction --routines --events --triggers --set-gtid-purged=OFF "
            "--add-drop-database --databases " + DATABASE
            if export
            else 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD"'
        )
        return [
            *self.prefix,
            "exec",
            "-i",
            identity,
            "sh",
            "-c",
            'test -n "${MYSQL_ROOT_PASSWORD:-}" || exit 64; ' + command,
        ]

    def clear_oauth2_tokens_command(self, identity: str) -> list[str]:
        """Revoke persisted Yudao OAuth2 access and refresh tokens after SQL restore."""
        return [
            *self.prefix,
            "exec",
            "-i",
            identity,
            "sh",
            "-c",
            'test -n "${MYSQL_ROOT_PASSWORD:-}" || exit 64; '
            f'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" {DATABASE} -e {_shell_quote(_CLEAR_OAUTH2_SQL)}',
        ]

    def flush_redis_command(self, identity: str) -> list[str]:
        """Clear Yudao Redis auth, session, and permission caches after SQL restore."""
        return [*self.prefix, "exec", identity, "redis-cli", "FLUSHDB"]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_gzip_nonempty(path: Path) -> int:
    total = 0
    try:
        with gzip.GzipFile(filename=path, mode="rb") as uncompressed:
            while True:
                chunk = uncompressed.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
    except (OSError, EOFError, zlib.error) as error:
        raise SnapshotRefused("snapshot_compression_invalid") from error
    if total == 0:
        raise SnapshotRefused("empty_dump")
    return total


def export_snapshot(docker: Docker, destination: Path) -> dict:
    identity = docker.mysql()
    if destination.exists() or destination.is_symlink():
        raise SnapshotRefused("destination_already_exists")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise SnapshotRefused("existing_private_destination_directory_required")
    if destination.parent.stat().st_mode & 0o077:
        raise SnapshotRefused("destination_directory_must_be_private")
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=destination.parent)
    process = None
    try:
        with os.fdopen(fd, "wb") as output:
            process = subprocess.Popen(
                docker.command(identity, export=True),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                with process.stdout, gzip.GzipFile(
                    fileobj=output, mode="wb", mtime=0, filename=""
                ) as compressed:
                    shutil.copyfileobj(process.stdout, compressed)
                    size = compressed.tell()
                if process.wait() != 0:
                    raise SnapshotRefused("dump_failed")
                if size == 0:
                    raise SnapshotRefused("empty_dump")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
            output.flush()
            os.fsync(output.fileno())
        path = Path(temporary)
        _verify_gzip_nonempty(path)
        # Publish without replacing another operator's export, including a file
        # created after the initial check. Both names are on the same filesystem.
        os.link(path, destination)
        path.unlink()
        _sync_directory(destination.parent)
        return {
            "mode": "snapshot_created",
            "project": docker.project,
            "database": DATABASE,
            "mysql_container": identity,
            "bytes": destination.stat().st_size,
            "sha256": _digest(destination),
        }
    finally:
        Path(temporary).unlink(missing_ok=True)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def restore_snapshot(
    docker: Docker,
    source: Path,
    *,
    expect: str,
    writer_isolated: bool,
    apply: bool,
) -> dict:
    if (
        apply is not True
        or writer_isolated is not True
        or not re.fullmatch(r"[0-9a-f]{64}", expect)
    ):
        raise SnapshotRefused("restore_requires_apply_isolation_and_reviewed_digest")
    # Private copy binds verification and decompression to the same bytes. Validate
    # the entire gzip (including its CRC) before the first database mutation.
    with tempfile.TemporaryFile() as compressed, tempfile.TemporaryFile() as sql:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as incoming:
            info = os.fstat(incoming.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SnapshotRefused("ordinary_single_link_snapshot_required")
            digest = hashlib.sha256()
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                compressed.write(block)
                digest.update(block)
        if digest.hexdigest() != expect:
            raise SnapshotRefused("snapshot_digest_mismatch")
        compressed.seek(0)
        try:
            with gzip.GzipFile(fileobj=compressed, mode="rb") as uncompressed:
                shutil.copyfileobj(uncompressed, sql)
        except (OSError, EOFError, zlib.error) as error:
            raise SnapshotRefused("snapshot_compression_invalid") from error
        if sql.tell() == 0:
            raise SnapshotRefused("empty_snapshot")
        identity = docker.mysql()
        redis_id = docker.redis()
        if docker.containers("server"):
            raise SnapshotRefused("project_server_must_be_stopped")
        if docker.containers("agent"):
            raise SnapshotRefused("project_agent_must_be_stopped")
        sql.seek(0)
        result = subprocess.run(
            docker.command(identity, export=False),
            stdin=sql,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            # An acknowledged prefix may already have committed. Never retry,
            # restart the application, erase the source or call this a rollback.
            raise SnapshotRefused("import_failed_or_indeterminate_keep_entry_closed")
        token_clear = subprocess.run(
            docker.clear_oauth2_tokens_command(identity),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if token_clear.returncode:
            raise SnapshotRefused("oauth2_token_clear_failed_keep_entry_closed")
        redis_flush = subprocess.run(
            docker.flush_redis_command(redis_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if redis_flush.returncode:
            raise SnapshotRefused("redis_cache_flush_failed_keep_entry_closed")
    return {
        "mode": "import_completed",
        "project": docker.project,
        "database": DATABASE,
        "mysql_container": identity,
        "redis_container": redis_id,
        "sha256": expect,
        "oauth2_tokens_cleared": True,
        "redis_flushed": True,
        "application_started": False,
        "application_postconditions_verified": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--project", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("destination", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("--expect-sha256", required=True)
    restore.add_argument("--writer-isolated", action="store_true")
    restore.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        docker = Docker(args.docker_host, args.project)
        if args.command == "export":
            result = export_snapshot(docker, args.destination)
        else:
            result = restore_snapshot(
                docker,
                args.source,
                expect=args.expect_sha256,
                writer_isolated=args.writer_isolated,
                apply=args.apply,
            )
    except SnapshotRefused as error:
        print(json.dumps({"mode": "refused_or_incomplete", "reason": str(error)}))
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"mode": "refused_or_incomplete", "reason": "snapshot_operation_failed"}))
        return 1
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "mode": "refused_or_incomplete",
                    "reason": "interrupted_outcome_requires_review",
                }
            )
        )
        return 130
    print(json.dumps({"schema_version": 1, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
