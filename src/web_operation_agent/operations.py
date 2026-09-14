"""Consistent backup and isolated restore for local operational SQLite state.

The bundle contains databases and content digests, never credentials or execution
capabilities.  Restoring a bundle creates a new directory and cannot replace a live
deployment in place.  A caller must also attest that writers are isolated: an old mutation
journal is evidence to reconcile, not authority to replay work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path


BACKUP_SCHEMA_VERSION = 1
_NAME = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_MANIFEST = "manifest.json"


class OperationalStateError(RuntimeError):
    """Operational state cannot be backed up, verified, or safely restored."""


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(path, 0o700)


def _regular_file(path: Path, description: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise OperationalStateError(f"{description} is unavailable") from exc
    if not stat.S_ISREG(details.st_mode):
        raise OperationalStateError(f"{description} must be a regular file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _check_database(connection: sqlite3.Connection, description: str) -> None:
    rows = tuple(row[0] for row in connection.execute("PRAGMA quick_check"))
    if rows != ("ok",):
        raise OperationalStateError(f"{description} failed SQLite integrity checking")


def _open_immutable(path: Path) -> sqlite3.Connection:
    """Open a closed snapshot without creating WAL/SHM sidecars in its bundle."""
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)


def _online_copy(source: Path, destination: Path) -> None:
    _regular_file(source, "source database")
    try:
        with sqlite3.connect(source, timeout=5) as live:
            _check_database(live, "source database")
            with sqlite3.connect(destination, timeout=5) as snapshot:
                live.backup(snapshot)
                # The source normally uses WAL.  A self-contained backup must not depend
                # on undeclared sidecars, so close the copied snapshot in rollback mode.
                snapshot.execute("PRAGMA journal_mode=DELETE")
                _check_database(snapshot, "backup database")
    except sqlite3.Error as exc:
        raise OperationalStateError("SQLite online backup failed") from exc
    os.chmod(destination, 0o600)


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _validated_sources(sources: Mapping[str, str | Path]) -> tuple[tuple[str, Path], ...]:
    if not isinstance(sources, Mapping) or not sources:
        raise OperationalStateError("at least one SQLite source is required")
    result: list[tuple[str, Path]] = []
    seen_paths: set[Path] = set()
    for name, raw_path in sorted(sources.items()):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise OperationalStateError("backup source names must be lowercase identifiers")
        path = Path(raw_path).expanduser().resolve(strict=False)
        _regular_file(path, f"source database {name}")
        if path in seen_paths:
            raise OperationalStateError("one database cannot appear twice in a backup")
        seen_paths.add(path)
        result.append((name, path))
    return tuple(result)


def create_state_backup(sources: Mapping[str, str | Path], destination: str | Path,
                        *, now_ms: int | None = None) -> dict:
    """Create an atomic, integrity-checked SQLite backup directory.

    SQLite's backup API takes the consistent snapshot, including committed WAL state.  The
    manifest deliberately records logical names rather than source paths so moving a bundle
    cannot disclose a host's directory layout.
    """
    entries = _validated_sources(sources)
    target = Path(destination).expanduser().resolve(strict=False)
    if target.exists() or target.is_symlink():
        raise OperationalStateError("backup destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
    _private_directory(temporary)
    try:
        files = []
        for name, source in entries:
            filename = f"{name}.sqlite3"
            snapshot = temporary / filename
            _online_copy(source, snapshot)
            files.append({
                "name": name,
                "file": filename,
                "bytes": snapshot.stat().st_size,
                "sha256": _sha256(snapshot),
            })
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at_ms": int(time.time() * 1000) if now_ms is None else int(now_ms),
            "files": files,
            "execution_authority_included": False,
        }
        if manifest["created_at_ms"] < 0:
            raise OperationalStateError("backup time must be non-negative")
        manifest_path = temporary / _MANIFEST
        manifest_path.write_bytes(_canonical_json(manifest))
        os.chmod(manifest_path, 0o600)
        temporary.rename(target)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_state_backup(bundle: str | Path) -> dict:
    """Verify manifest shape, file bounds, digests, and SQLite integrity."""
    root = Path(bundle).expanduser().resolve(strict=False)
    _regular_file(root / _MANIFEST, "backup manifest")
    try:
        manifest = json.loads((root / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise OperationalStateError("backup manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "created_at_ms", "files", "execution_authority_included"}:
        raise OperationalStateError("backup manifest fields do not match schema 1")
    if manifest["schema_version"] != BACKUP_SCHEMA_VERSION:
        raise OperationalStateError("backup schema version is unsupported")
    if (not isinstance(manifest["created_at_ms"], int)
            or isinstance(manifest["created_at_ms"], bool)
            or manifest["created_at_ms"] < 0):
        raise OperationalStateError("backup creation time is invalid")
    if manifest["execution_authority_included"] is not False:
        raise OperationalStateError("a backup must not contain execution authority")
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) > 16:
        raise OperationalStateError("backup file list is invalid")
    names: set[str] = set()
    expected_files = {_MANIFEST}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"name", "file", "bytes", "sha256"}:
            raise OperationalStateError("backup file entry is invalid")
        name = entry["name"]
        filename = entry["file"]
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or filename != f"{name}.sqlite3" or name in names):
            raise OperationalStateError("backup file identity is invalid")
        names.add(name)
        expected_files.add(filename)
        path = root / filename
        _regular_file(path, f"backup database {name}")
        if (not isinstance(entry["bytes"], int) or isinstance(entry["bytes"], bool)
                or entry["bytes"] <= 0 or path.stat().st_size != entry["bytes"]):
            raise OperationalStateError(f"backup database {name} size does not match")
        if entry["sha256"] != _sha256(path):
            raise OperationalStateError(f"backup database {name} digest does not match")
        try:
            with _open_immutable(path) as database:
                _check_database(database, f"backup database {name}")
        except sqlite3.Error as exc:
            raise OperationalStateError(f"backup database {name} cannot be opened") from exc
    actual_files = {item.name for item in root.iterdir()}
    if actual_files != expected_files:
        raise OperationalStateError("backup directory contains undeclared files")
    return manifest


def _mutation_summary(path: Path) -> dict:
    try:
        with _open_immutable(path) as database:
            table = database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mutation_intents'"
            ).fetchone()
            if table is None:
                return {"present": False, "unresolved": 0}
            unresolved = database.execute(
                "SELECT count(*) FROM mutation_intents "
                "WHERE state IN ('dispatching','indeterminate')"
            ).fetchone()[0]
            return {"present": True, "unresolved": int(unresolved)}
    except sqlite3.Error as exc:
        raise OperationalStateError("restored mutation journal cannot be inspected") from exc


def restore_state_backup(bundle: str | Path, destination: str | Path, *,
                         writer_isolated: bool) -> dict:
    """Restore verified databases into a new directory while all writers are stopped."""
    if writer_isolated is not True:
        raise OperationalStateError("restore requires an isolated, stopped writer")
    source_root = Path(bundle).expanduser().resolve(strict=False)
    manifest = verify_state_backup(source_root)
    target = Path(destination).expanduser().resolve(strict=False)
    if target.exists() or target.is_symlink():
        raise OperationalStateError("restore destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
    _private_directory(temporary)
    try:
        restored = {}
        for entry in manifest["files"]:
            source = source_root / entry["file"]
            output = temporary / entry["file"]
            _online_copy(source, output)
            restored[entry["name"]] = output
        mutation = (_mutation_summary(restored["mutation-journal"])
                    if "mutation-journal" in restored
                    else {"present": False, "unresolved": 0})
        temporary.rename(target)
        return {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "restored": sorted(restored),
            "execution_authority_restored": False,
            "writer_isolated": True,
            "mutation_journal": mutation,
            "requires_reconciliation": mutation["unresolved"] > 0,
        }
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m web_operation_agent.operations")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create a consistent operational backup")
    backup.add_argument("--sessions", required=True)
    backup.add_argument("--mutations", required=True)
    backup.add_argument("--conversations")
    backup.add_argument("--destination", required=True)
    verify = commands.add_parser("verify", help="verify a backup without restoring it")
    verify.add_argument("bundle")
    restore = commands.add_parser("restore", help="restore into a new isolated directory")
    restore.add_argument("bundle")
    restore.add_argument("--destination", required=True)
    restore.add_argument("--writer-isolated", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "backup":
            sources = {
                "session-store": arguments.sessions,
                "mutation-journal": arguments.mutations,
            }
            if arguments.conversations:
                sources["conversation-state"] = arguments.conversations
            result = create_state_backup(sources, arguments.destination)
        elif arguments.command == "verify":
            result = verify_state_backup(arguments.bundle)
        else:
            result = restore_state_backup(
                arguments.bundle, arguments.destination,
                writer_isolated=arguments.writer_isolated)
    except OperationalStateError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "READY", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
