"""Guard, snapshot and per-attempt reset for the ``p74_odoo_v1`` fixture.

The guard reads PostgreSQL and the filestore directly, not through any product tool: a trial
that writes a chatter message, a sequence value or an attachment nobody queries must still
show up. It digests whole rows of every business, accounting, chatter and notification table
the supported workflows can touch, the currency rates, the ``ir_sequence`` rows and their
PostgreSQL sequence values, and the SHA-256 of every filestore object named by a guarded
attachment. Rows the running application writes by itself are excluded only by stable,
named rules, and their volume is reported separately so an exclusion can never hide a change
silently.

A reset stops the application writers, recreates the database from the baseline archive,
restores the filestore from its archive, starts Odoo again and compares a fresh guard capture
and the actor-visible projection with the qualified baseline. Uncertainty is recorded before
the destructive step: an interrupted reset is INDETERMINATE on the next read, never retried.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .population import digest_json
from .stack import DATABASE, Stack, StackError

FIXTURE_ID = "p74_odoo_v1"
RECORD_VERSION = 1

GUARDED_TABLES = (
    "account_bank_statement_line", "account_move", "account_move_line", "account_partial_reconcile",
    "account_payment", "ir_cron", "ir_sequence", "mail_activity", "mail_followers", "mail_mail",
    "mail_message", "mail_notification", "mail_tracking_value", "product_pricelist",
    "product_product", "product_template", "res_company", "res_currency_rate", "res_partner",
    "res_users", "res_users_apikeys", "sale_order", "sale_order_line",
)
#: Stable exclusion rules. Each is a property of the running web client, not of a workflow.
EXCLUSION_RULES = {
    "bus_bus": "real-time bus notifications, transient by design",
    "res_users_log": "append-only login audit written by every ordinary login",
    "res_users_settings": "per-user web client preferences",
    "discuss_channel_member": "Discuss read-state bookkeeping updated by opening the home page",
    "ir_attachment[url like /web/assets/%]": "asset bundles regenerated on demand",
}


def _table_sql(table: str) -> str:
    return (f"select '{table}', count(*), md5(coalesce(string_agg(row_to_json(x)::text, '|' "
            f"order by x.id), '')) from {table} x")


class FixtureGuard:
    def __init__(self, stack: Stack):
        self.stack = stack

    def capture(self) -> dict[str, Any]:
        selects = " union all ".join(_table_sql(table) for table in GUARDED_TABLES)
        selects += (" union all select 'ir_attachment', count(*), md5(coalesce(string_agg("
                    "row_to_json(x)::text, '|' order by x.id), '')) from ir_attachment x "
                    "where coalesce(x.url, '') not like '/web/assets/%'")
        raw = self.stack.psql(selects)
        tables: dict[str, dict[str, Any]] = {}
        for line in raw.splitlines():
            if line.strip():
                name, rows, digest = line.split("|")
                tables[name] = {"rows": int(rows), "digest": digest}
        if set(tables) != set(GUARDED_TABLES) | {"ir_attachment"}:
            raise StackError("guard capture is incomplete")
        sequences_raw = self.stack.psql(
            "select sequencename || '|' || coalesce(last_value::text, 'null') from pg_sequences "
            "where schemaname = 'public' and sequencename ~ '^ir_sequence_[0-9]+$' "
            "order by sequencename")
        sequences = dict(line.split("|") for line in sequences_raw.splitlines() if line.strip())
        referenced = [line.strip() for line in self.stack.psql(
            "select store_fname from ir_attachment where store_fname is not null and "
            "coalesce(url, '') not like '/web/assets/%' order by store_fname").splitlines()
            if line.strip()]
        excluded = {}
        for table in ("bus_bus", "res_users_log", "res_users_settings", "discuss_channel_member"):
            excluded[table] = int(self.stack.psql(f"select count(*) from {table}").strip())
        excluded["asset_attachments"] = int(self.stack.psql(
            "select count(*) from ir_attachment where url like '/web/assets/%'").strip())
        files = self._filestore_hashes()
        missing = sorted(name for name in referenced if name not in files)
        guarded_files = {name: files[name] for name in referenced if name in files}
        state = {
            "fixture_id": FIXTURE_ID,
            "tables": tables,
            "sequences": sequences,
            "filestore": {"referenced_objects": len(referenced),
                          "missing_objects": missing,
                          "digest": digest_json(guarded_files)},
        }
        return {**state, "excluded_volumes": excluded, "digest": digest_json(state)}

    def _filestore_hashes(self) -> dict[str, str]:
        output = self.stack.compose(
            "exec", "-T", "odoo", "sh", "-c",
            f"cd /var/lib/odoo/filestore/{DATABASE} 2>/dev/null && "
            "find . -type f -print0 | sort -z | xargs -0 sha256sum || true",
            timeout=300).stdout.decode("utf-8", "replace")
        hashes = {}
        for line in output.splitlines():
            digest, _, name = line.partition("  ")
            if digest and name:
                hashes[name.removeprefix("./")] = digest
        return hashes


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Named guard components that differ; excluded volumes are never compared."""
    changed = sorted(name for name in set(before["tables"]) | set(after["tables"])
                     if before["tables"].get(name) != after["tables"].get(name))
    changed += [f"sequence:{name}" for name in sorted(set(before["sequences"])
                                                      | set(after["sequences"]))
                if before["sequences"].get(name) != after["sequences"].get(name)]
    if before["filestore"] != after["filestore"]:
        changed.append("filestore")
    return changed


def stability_proof(guard: FixtureGuard, *, interval_s: float) -> dict[str, Any]:
    """Two captures separated by at least one full trial wall budget must agree exactly."""
    first = guard.capture()
    started = time.monotonic()
    time.sleep(interval_s)
    second = guard.capture()
    waited = round(time.monotonic() - started, 1)
    changed = differences(first, second)
    return {"kind": "p74_fixture_stability_proof", "interval_s": waited,
            "required_interval_s": interval_s, "first_digest": first["digest"],
            "second_digest": second["digest"], "changed": changed,
            "excluded_volume_changes": {
                key: [first["excluded_volumes"][key], second["excluded_volumes"][key]]
                for key in first["excluded_volumes"]
                if first["excluded_volumes"][key] != second["excluded_volumes"][key]},
            "status": "READY" if not changed and waited >= interval_s else "FAILED"}


# -- snapshot and restore -------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".partial")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _seal(record: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in record.items() if key != "record_digest"}
    return {**body, "record_digest": digest_json(body)}


def _read_record(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("record_digest") != digest_json(
            {key: value for key, value in record.items() if key != "record_digest"}):
        raise StackError("fixture recovery record digest is invalid")
    return record


def prepare_snapshot(stack: Stack, guard: FixtureGuard, directory: Path, *,
                     actor_projection: dict[str, Any]) -> dict[str, Any]:
    """Archive the qualified baseline while the application writers are stopped."""
    stack.compose("stop", "agent", "provider-relay", "odoo", timeout=300)
    try:
        archive = stack.run(["docker", "exec", stack.container("db"), "pg_dump", "-U", "odoo",
                             "-d", DATABASE, "--format=custom", "--no-owner",
                             "--no-privileges"], timeout=900).stdout
        if not archive.startswith(b"PGDMP"):
            raise StackError("baseline archive is not a PostgreSQL custom dump")
        filestore = stack.compose(
            "run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c",
            f"tar -C /var/lib/odoo/filestore -cf - {DATABASE}", timeout=600).stdout
    finally:
        stack.compose("up", "-d", "odoo", timeout=600)
    _wait_odoo(stack)
    state = guard.capture()
    _write_private(directory / "baseline.pgdump", archive)
    _write_private(directory / "baseline-filestore.tar", filestore)
    record = _seal({
        "artifact_version": RECORD_VERSION, "kind": "p74_fixture_recovery", "status": "READY",
        "fixture_id": FIXTURE_ID, "project": stack.project, "database": DATABASE,
        "database_archive": {"sha256": _sha256(archive), "bytes": len(archive)},
        "filestore_archive": {"sha256": _sha256(filestore), "bytes": len(filestore)},
        "baseline_guard": state, "baseline_digest": state["digest"],
        "actor_projection_digest": digest_json(actor_projection),
    })
    _write_private(directory / "recovery.json",
                   (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return record


def _wait_odoo(stack: Stack, timeout_s: float = 180) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = stack.compose("exec", "-T", "odoo", "python3", "-c",
                              "import urllib.request;urllib.request.urlopen("
                              "'http://127.0.0.1:8069/web/login',timeout=3)",
                              check=False, timeout=30)
        if probe.returncode == 0:
            return
        time.sleep(2)
    raise StackError("Odoo did not become ready after the reset")


def restore(stack: Stack, guard: FixtureGuard, directory: Path, *,
            actor_projection: Any, rearm_after_failure: bool = False) -> dict[str, Any]:
    """Put the baseline back and verify it; the caller supplies the actor projection reader.

    ``actor_projection`` is a zero-argument callable returning the actor-visible projection,
    read independently of the product after Odoo is back. A previous FAILED reset blocks
    further resets until an operator re-arms the record deliberately; an uncertain one stays
    uncertain.
    """
    record_path = directory / "recovery.json"
    try:
        record = _read_record(record_path)
    except (OSError, ValueError, StackError):
        return {"status": "INDETERMINATE", "basis": ["recovery_record_unreadable"]}
    if record["status"] in {"RESTORING", "INDETERMINATE"}:
        return {"status": "INDETERMINATE", "basis": ["previous_reset_reported_no_outcome"]}
    if record["status"] == "FAILED" and not rearm_after_failure:
        return {"status": "FAIL", "basis": ["previous_reset_failed_requires_operator"]}
    if record["project"] != stack.project or record["database"] != DATABASE:
        return {"status": "FAIL", "basis": ["recovery_record_names_another_fixture"]}
    try:
        archive = (directory / "baseline.pgdump").read_bytes()
        filestore = (directory / "baseline-filestore.tar").read_bytes()
    except OSError:
        return {"status": "FAIL", "basis": ["baseline_archive_missing"]}
    if (_sha256(archive) != record["database_archive"]["sha256"]
            or _sha256(filestore) != record["filestore_archive"]["sha256"]):
        return {"status": "FAIL", "basis": ["baseline_archive_digest_mismatch"]}
    try:
        stack.container("db")
        stack.container("odoo")
    except StackError:
        return {"status": "FAIL", "basis": ["target_is_not_the_evaluation_project"]}
    active = _seal({**record, "status": "RESTORING"})
    _write_private(record_path, (json.dumps(active, indent=2, sort_keys=True) + "\n").encode())
    try:
        stack.compose("stop", "agent", "provider-relay", "odoo", timeout=300)
        db = stack.container("db")
        stack.run(["docker", "exec", "-i", db, "psql", "-U", "odoo", "-d", "postgres",
                   "-v", "ON_ERROR_STOP=1", "-c", f'drop database if exists "{DATABASE}" '
                   "with (force)"], timeout=300)
        stack.run(["docker", "exec", "-i", db, "psql", "-U", "odoo", "-d", "postgres",
                   "-v", "ON_ERROR_STOP=1", "-c", f'create database "{DATABASE}" owner odoo'],
                  timeout=300)
        stack.run(["docker", "exec", "-i", db, "pg_restore", "-U", "odoo", "-d", DATABASE,
                   "--no-owner", "--no-privileges", "--exit-on-error"],
                  input_bytes=archive, timeout=900)
        stack.compose("run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c",
                      f"rm -rf /var/lib/odoo/filestore/{DATABASE} && "
                      "tar -C /var/lib/odoo/filestore -xf -", input_bytes=filestore,
                      timeout=600)
        stack.compose("up", "-d", "odoo", timeout=600)
        _wait_odoo(stack)
        observed = guard.capture()
        projection = actor_projection()
    except (StackError, OSError):
        _write_private(record_path, (json.dumps(_seal({**record, "status": "INDETERMINATE"}),
                                                indent=2, sort_keys=True) + "\n").encode())
        return {"status": "INDETERMINATE", "basis": ["reset_command_reported_no_outcome"]}
    changed = differences(record["baseline_guard"], observed)
    projection_matches = digest_json(projection) == record["actor_projection_digest"]
    passed = not changed and projection_matches
    _write_private(record_path, (json.dumps(_seal({**record, "status": "READY" if passed
                                                   else "FAILED"}),
                                            indent=2, sort_keys=True) + "\n").encode())
    basis = [f"changed:{name}" for name in changed]
    if not projection_matches:
        basis.append("actor_projection_mismatch")
    return {"status": "PASS" if passed else "FAIL", "basis": basis,
            "guard_digest": observed["digest"]}
