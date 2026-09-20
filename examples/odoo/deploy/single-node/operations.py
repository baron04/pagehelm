#!/usr/bin/env python3
"""Backup, verify, and restore the private single-node deployment.

The command operates only on the explicitly named Compose project. It never copies the
deployment's credential directory into a backup and it never restores browser capabilities
or approvals. Credential backup and rotation remain the deployment owner's secret-manager
responsibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
COMPOSE = ROOT / "examples" / "odoo" / "deploy" / "single-node" / "docker-compose.yml"
SCHEMA_VERSION = 2
PROJECT = re.compile(r"woa-[a-z0-9][a-z0-9_-]{2,62}\Z")


class OperationsError(RuntimeError):
    pass


def _alert(kind: str) -> None:
    """Best-effort, content-free recovery alert; never masks the original failure."""
    target = os.environ.get("WOA_ALERT_WEBHOOK_URL", "")
    if not target:
        return
    body = json.dumps({
        "schema_version": 1,
        "service": "web-operation-agent-odoo",
        "kind": kind,
        "observed_at_ms": int(time.time() * 1000),
    }, separators=(",", ":")).encode("utf-8")
    try:
        request = urllib.request.Request(
            target, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read(1024)
    except Exception:
        pass


def _run(argv: list[str], *, env: dict[str, str], input_bytes: bytes | None = None,
         timeout: int = 900) -> bytes:
    try:
        completed = subprocess.run(
            argv, cwd=ROOT, env=env, input=input_bytes, capture_output=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationsError(f"{argv[0]} could not be run: {type(exc).__name__}") from exc
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:1200]
        raise OperationsError(
            f"{' '.join(argv[:4])} failed with exit {completed.returncode}: {detail}")
    return completed.stdout


def _environment(project: str) -> dict[str, str]:
    if not PROJECT.fullmatch(project):
        raise OperationsError("project name must be an explicit woa-* identifier")
    env = dict(os.environ)
    env.pop("COMPOSE_PROJECT_NAME", None)
    env.pop("COMPOSE_PROFILES", None)
    env["WOA_PROJECT_NAME"] = project
    return env


def _compose(env: dict[str, str], *args: str, timeout: int = 900) -> bytes:
    return _run(
        ["docker", "compose", "-p", env["WOA_PROJECT_NAME"],
         "-f", str(COMPOSE), *args], env=env, timeout=timeout)


#: What this deployment already decided, and what every later operation must keep using.
#: Compose resolves the file against the caller's environment, so an operator who runs a
#: backup or a restore from a shell that does not export these gets the file's defaults:
#: another image for the one-off containers this command starts, and another private
#: directory to mount. They are read from the running deployment instead of guessed.
_DEPLOYMENT_SETTINGS = ("WOA_ODOO_IMAGE", "WOA_BUILD_REVISION", "WOA_PRIVATE_DIR")


def _inherit_running_settings(env: dict[str, str]) -> dict[str, str]:
    """Fill the deployment settings the caller did not export from its own containers."""
    if all(env.get(name) for name in _DEPLOYMENT_SETTINGS):
        return env
    # Best effort: a command that needs the deployment fails on its own service check, with
    # a better message than this one could give.
    try:
        container = _compose(env, "ps", "-q", "odoo").decode().strip()
        if not container or "\n" in container:
            return env
        image = _run(["docker", "inspect", "--format", "{{.Config.Image}}",
                      container], env=env).decode().strip()
        mounts = _run([
            "docker", "inspect", "--format",
            "{{range .Mounts}}{{.Source}}|{{.Destination}}\n{{end}}", container],
            env=env).decode()
    except OperationsError:
        return env
    repository, _, revision = image.rpartition(":")
    private = ""
    for line in mounts.splitlines():
        source, _, destination = line.partition("|")
        if destination.strip() == "/run/woa-input/session-secret":
            private = str(Path(source.strip()).parent)
    inherited = {"WOA_ODOO_IMAGE": repository, "WOA_BUILD_REVISION": revision,
                 "WOA_PRIVATE_DIR": private}
    for name in _DEPLOYMENT_SETTINGS:
        if not env.get(name) and inherited[name]:
            env[name] = inherited[name]
    return env


def _assert_project_services(env: dict[str, str], *services: str) -> None:
    """Refuse destructive work unless Compose and Docker agree on every target."""
    project = env["WOA_PROJECT_NAME"]
    for service in services:
        container = _compose(env, "ps", "-q", service).decode().strip()
        if not container or "\n" in container:
            raise OperationsError(f"service {service} is not a single running project target")
        actual = _run([
            "docker", "inspect", "--format",
            '{{ index .Config.Labels "com.docker.compose.project" }}', container,
        ], env=env).decode().strip()
        if actual != project:
            raise OperationsError(f"service {service} belongs to a different Compose project")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _unresolved_mutations(path: Path) -> int:
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                             uri=True, timeout=5) as database:
            row = database.execute(
                "SELECT count(*) FROM mutation_intents "
                "WHERE state IN ('dispatching','indeterminate')"
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise OperationsError("mutation journal could not be inspected") from exc
    return int(row[0])


def _live_unresolved_mutations(env: dict[str, str]) -> int:
    output = _compose(
        env, "run", "--rm", "--no-deps", "--entrypoint", "python", "agent",
        "-m", "web_operation_agent.composition.operations", "inspect", "--mutations",
        "/var/lib/woa/mutation-journal.sqlite3")
    try:
        payload = json.loads(output.decode("utf-8").strip().splitlines()[-1])
        return int(payload["mutation_journal"]["unresolved"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationsError("live mutation preflight returned an invalid result") from exc


def _manifest(root: Path, *, project: str, created_at_ms: int) -> dict:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()
                       and item != root / "manifest.json"):
        files.append({
            "file": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _digest(path),
        })
    if not files:
        raise OperationsError("backup produced no files")
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "created_at_ms": created_at_ms,
        "execution_authority_included": False,
        "bearer_sessions_included": False,
        "credential_verifiers_included": True,
        "secret_material_included": False,
        "files": files,
    }


def verify(bundle: str | Path, *, expected_project: str | None = None) -> dict:
    root = Path(bundle).expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise OperationsError("backup manifest is unreadable") from exc
    if (not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "project", "created_at_ms",
            "execution_authority_included", "bearer_sessions_included",
            "credential_verifiers_included", "secret_material_included", "files"}
            or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("execution_authority_included") is not False
            or manifest.get("bearer_sessions_included") is not False
            or manifest.get("credential_verifiers_included") is not True
            or manifest.get("secret_material_included") is not False
            or not PROJECT.fullmatch(str(manifest.get("project") or ""))
            or not isinstance(manifest.get("created_at_ms"), int)
            or not isinstance(manifest.get("files"), list)
            or not manifest["files"] or len(manifest["files"]) > 32):
        raise OperationsError("backup manifest does not match schema 2")
    if expected_project is not None and manifest["project"] != expected_project:
        raise OperationsError("backup belongs to a different Compose project")
    expected = {"manifest.json"}
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"file", "bytes", "sha256"}
                or not isinstance(entry["file"], str)
                or entry["file"].startswith("/") or ".." in Path(entry["file"]).parts
                or not isinstance(entry["bytes"], int) or entry["bytes"] <= 0
                or not re.fullmatch(r"[a-f0-9]{64}", str(entry["sha256"]))):
            raise OperationsError("backup file entry is invalid")
        path = root / entry["file"]
        if (not path.is_file() or path.is_symlink()
                or path.stat().st_size != entry["bytes"]
                or _digest(path) != entry["sha256"]):
            raise OperationsError(f"backup file {entry['file']} failed verification")
        expected.add(entry["file"])
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != expected:
        raise OperationsError("backup contains undeclared files")
    required = {
        "agent-state/manifest.json", "agent-state/session-store.sqlite3",
        "agent-state/mutation-journal.sqlite3",
        "agent-state/conversation-state.sqlite3", "odoo-database.pgdump",
        "odoo-data.tar.gz", "agent-state/agent-ledgers.tar.gz",
    }
    if not required <= actual:
        raise OperationsError("backup is missing required deployment state")
    return manifest


def backup(*, project: str, destination: str | Path) -> dict:
    env = _inherit_running_settings(_environment(project))
    _assert_project_services(env, "agent", "odoo", "db")
    target = Path(destination).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise OperationsError("backup destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    stopped = False
    try:
        # No browser request can dispatch while the three writers are stopped. PostgreSQL
        # stays online only to produce one MVCC-consistent pg_dump.
        _compose(env, "stop", "agent", "odoo")
        stopped = True
        agent = temporary / "agent-state"
        agent.mkdir(mode=0o700)
        owner = f"{os.getuid()}:{os.getgid()}"
        _compose(
            env, "run", "--rm", "--no-deps", "--entrypoint", "bash",
            "-v", f"{agent}:/backup", "agent",
            "-lc", "python -m web_operation_agent.composition.operations backup "
            "--sessions /var/lib/woa/session-store.sqlite3 "
            "--mutations /var/lib/woa/mutation-journal.sqlite3 "
            "--conversations /var/lib/woa/conversation-state.sqlite3 "
            "--destination /backup/state && "
            "mkdir -p /var/lib/woa/runs && "
            "tar -C /var/lib/woa -czf /backup/agent-ledgers.tar.gz runs && "
            f"chown -R {owner} /backup")
        produced = agent / "state"
        for item in produced.iterdir():
            item.rename(agent / item.name)
        produced.rmdir()
        database = _compose(
            env, "exec", "-T", "db", "pg_dump", "-Fc", "-U", "odoo",
            "-d", "woa_trial")
        (temporary / "odoo-database.pgdump").write_bytes(database)
        _compose(
            env, "run", "--rm", "--no-deps", "--entrypoint", "bash",
            "-v", f"{temporary}:/backup", "odoo", "-lc",
            "tar -C /var/lib/odoo --exclude='./sessions' "
            "--exclude='./sessions/**' " + _SCRATCH_EXCLUDES
            + " -czf /backup/odoo-data.tar.gz . && "
            f"chown {owner} /backup/odoo-data.tar.gz")
        created = int(time.time() * 1000)
        manifest = _manifest(temporary, project=project, created_at_ms=created)
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        for path in temporary.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        verify(temporary, expected_project=project)
        temporary.rename(target)
        return manifest
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise OperationsError("backup filesystem operation failed") from exc
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if stopped:
            _compose(env, "start", "odoo", "agent", timeout=1200)


#: The directories `restore` stages into and keeps the previous state in. A restore that
#: fails part-way leaves them in the data volume on purpose, and a backup taken afterwards
#: used to archive them: restoring that bundle then extracted a second `.woa-restore-stage`
#: inside the first, and the cutover `mv` refused to overwrite it with Agent and Odoo
#: stopped. Neither is ever archived, and neither is extracted from a bundle taken before.
_SCRATCH_EXCLUDES = " ".join(
    f"--exclude='./{name}' --exclude='./{name}/**'"
    for name in (".woa-restore-stage", ".woa-restore-previous"))


def _core_restore_script(backup: str, state: str) -> str:
    """Restore the core's own state bundle into ``{state}/.woa-restore-stage``.

    The deployment bundle keeps the run ledgers, ``agent-ledgers.tar.gz``, inside the
    ``agent-state`` directory, beside the core bundle. The core restore refuses any file its
    own manifest does not declare, so it is handed a copy of that directory without the
    ledgers; they are extracted separately afterwards.
    """
    stage, core = f"{state}/.woa-restore-stage", f"{state}/.woa-restore-bundle"
    return (
        f"rm -rf {stage} {core} && mkdir -p {core} && "
        f"find {backup}/agent-state -maxdepth 1 -type f ! -name agent-ledgers.tar.gz "
        f"-exec cp -p {{}} {core}/ \\; && "
        f"python -m web_operation_agent.composition.operations restore {core} "
        f"--destination {stage} --writer-isolated && "
        f"rm -rf {core}")


def restore(*, project: str, bundle: str | Path) -> dict:
    env = _inherit_running_settings(_environment(project))
    root = Path(bundle).expanduser().resolve()
    manifest = verify(root, expected_project=project)
    _assert_project_services(env, "agent", "odoo", "db")
    if _unresolved_mutations(root / "agent-state" / "mutation-journal.sqlite3"):
        raise OperationsError("backup contains unresolved mutations and requires reconciliation")
    if _live_unresolved_mutations(env):
        raise OperationsError("current deployment has unresolved mutations; restore refused")
    safety = root.parent / (
        f"{project}-pre-restore-{int(time.time() * 1000)}")
    backup(project=project, destination=safety)
    _assert_project_services(env, "agent", "odoo", "db")
    started = time.monotonic()
    suffix = uuid.uuid4().hex[:12]
    staging_database = f"woa_restore_{suffix}"
    previous_database = f"woa_previous_{suffix}"
    try:
        _compose(env, "stop", "agent", "odoo")
        # Restore and validate every component in a staging namespace. The current
        # deployment remains intact until all three candidates are readable.
        _compose(env, "exec", "-T", "db", "dropdb", "--if-exists", "-U", "odoo",
                 staging_database)
        _compose(env, "exec", "-T", "db", "createdb", "-U", "odoo",
                 staging_database)
        dump = (root / "odoo-database.pgdump").read_bytes()
        _run([
            "docker", "compose", "-p", project, "-f", str(COMPOSE),
            "exec", "-T", "db",
            "pg_restore", "--no-owner", "--no-privileges", "-U", "odoo",
            "-d", staging_database], env=env, input_bytes=dump, timeout=1200)
        _compose(env, "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1",
                 "-U", "odoo", "-d", staging_database, "-tAc", "SELECT 1")
        _compose(
            env, "run", "--rm", "--no-deps", "--entrypoint", "bash",
            "-v", f"{root}:/backup:ro", "odoo", "-lc",
            "rm -rf /var/lib/odoo/.woa-restore-stage && "
            "mkdir -p /var/lib/odoo/.woa-restore-stage && "
            "tar -C /var/lib/odoo/.woa-restore-stage " + _SCRATCH_EXCLUDES
            + " -xzf /backup/odoo-data.tar.gz && "
            "rm -rf /var/lib/odoo/.woa-restore-stage/sessions")
        # Core restore validates SQLite again and explicitly strips execution authority.
        _compose(
            env, "run", "--rm", "--no-deps", "--entrypoint", "bash",
            "-v", f"{root}:/backup:ro", "agent", "-lc",
            _core_restore_script("/backup", "/var/lib/woa") + " && "
            "mkdir -p /var/lib/woa/.woa-restore-stage/runs && "
            "tar -C /var/lib/woa/.woa-restore-stage --skip-old-files "
            "-xzf /backup/agent-state/agent-ledgers.tar.gz && "
            "if [ -d /var/lib/woa/runs ]; then "
            "tar -C /var/lib/woa/runs -cf - . | "
            "tar -C /var/lib/woa/.woa-restore-stage/runs --skip-old-files -xf -; fi && "
            "chown -R 10001:10001 /var/lib/woa/.woa-restore-stage")

        # Short cutover begins only after staging validation. Previous state stays beside
        # the candidate until both services pass health checks.
        _compose(env, "exec", "-T", "db", "dropdb", "--if-exists", "-U", "odoo",
                 previous_database)
        _compose(env, "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1",
                 "-U", "odoo", "-d", "postgres", "-c",
                 f"ALTER DATABASE woa_trial RENAME TO {previous_database}")
        _compose(env, "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1",
                 "-U", "odoo", "-d", "postgres", "-c",
                 f"ALTER DATABASE {staging_database} RENAME TO woa_trial")
        for service, state_dir in (("odoo", "/var/lib/odoo"),
                                   ("agent", "/var/lib/woa")):
            _compose(
                env, "run", "--rm", "--no-deps", "--entrypoint", "bash",
                service, "-lc",
                f"rm -rf {state_dir}/.woa-restore-previous && "
                f"mkdir -p {state_dir}/.woa-restore-previous && "
                f"find {state_dir} -mindepth 1 -maxdepth 1 "
                "! -name .woa-restore-stage ! -name .woa-restore-previous "
                f"-exec mv -t {state_dir}/.woa-restore-previous {{}} + && "
                f"find {state_dir}/.woa-restore-stage -mindepth 1 -maxdepth 1 "
                f"-exec mv -t {state_dir} {{}} + && "
                f"rmdir {state_dir}/.woa-restore-stage")
        _compose(env, "start", "odoo", "agent", timeout=1200)
        _assert_project_services(env, "agent", "odoo", "db")
        _compose(env, "exec", "-T", "db", "dropdb", "--if-exists", "-U", "odoo",
                 previous_database)
        for service, state_dir in (("odoo", "/var/lib/odoo"),
                                   ("agent", "/var/lib/woa")):
            _compose(env, "run", "--rm", "--no-deps", "--entrypoint", "bash",
                     service, "-lc", f"rm -rf {state_dir}/.woa-restore-previous")
    except OSError as exc:
        raise OperationsError(
            f"restore failed and services remain stopped; safety backup: {safety}") from exc
    except BaseException as exc:
        if isinstance(exc, OperationsError):
            raise OperationsError(
                f"{exc}; services remain stopped; safety backup: {safety}") from exc
        raise
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "backup_created_at_ms": manifest["created_at_ms"],
        "recovery_seconds": round(time.monotonic() - started, 3),
        "execution_authority_restored": False,
        "bearer_sessions_restored": False,
        "safety_backup": str(safety),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--destination", required=True)
    check = commands.add_parser("verify")
    check.add_argument("bundle")
    recover = commands.add_parser("restore")
    recover.add_argument("bundle")
    recover.add_argument("--confirm-project", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "backup":
            result = backup(project=arguments.project, destination=arguments.destination)
        elif arguments.command == "verify":
            result = verify(arguments.bundle, expected_project=arguments.project)
        else:
            if arguments.confirm_project != arguments.project:
                raise OperationsError("restore confirmation does not match the project")
            result = restore(project=arguments.project, bundle=arguments.bundle)
    except OperationsError as exc:
        if arguments.command in {"backup", "restore"}:
            _alert(f"{arguments.command}_failure")
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    print(json.dumps({"status": "READY", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
