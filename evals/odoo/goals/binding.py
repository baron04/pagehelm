"""Bind a goal holdout run to one candidate, fixture and application date.

This is an integrity check, not proof that the task oracles are correct. It runs
before model calls and again before each attempt so a resumed batch cannot silently
mix candidates, database contents or relative-date interpretations.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

from evals.odoo.p87_gate import fingerprint

_TRUTH_TABLES = ("res_partner", "sale_order", "sale_order_line", "product_template",
                 "product_product", "account_move", "account_move_line",
                 "account_payment_term", "res_country", "ir_module_module", "res_users")
_TRUTH_TABLES_SQL = "SELECT string_agg(label||':'||rows||':'||digest, E'\\n' ORDER BY label) FROM (\n" + (
    " UNION ALL\n".join(
        f"SELECT '{table}' AS label, count(*)::text AS rows, "
        f"md5(coalesce(string_agg(md5(to_jsonb(record)::text), '' "
        f"ORDER BY record.id), '')) AS digest FROM {table} AS record"
        for table in _TRUTH_TABLES)
) + "\n) AS snapshots;"


def _output(command: list[str], *, input_text: str | None = None) -> str:
    return subprocess.run(command, input=input_text, capture_output=True, text=True,
                          timeout=60, check=True).stdout.strip()


def fixture_signature(db_container: str, database: str) -> str:
    """Hash the rows behind the truth, including accounting lines the old guard omitted."""
    rows = _output(["docker", "exec", "-i", db_container, "psql", "-U", "odoo",
                    "-d", database, "-At"], input_text=_TRUTH_TABLES_SQL)
    return hashlib.sha256((fingerprint(db_container, database) + "\n" + rows).encode()).hexdigest()


def current_reference_date(app_container: str) -> str:
    return _output(["docker", "exec", app_container, "env", "TZ=Asia/Shanghai",
                    "date", "+%Y-%m-%d"])


def candidate_commit(root: Path) -> str:
    root = root.resolve()
    if _output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"]):
        raise RuntimeError("the candidate checkout is not clean")
    return _output(["git", "-C", str(root), "rev-parse", "HEAD"])


def assert_runtime_checkout(root: Path) -> None:
    for package in ("web_operation_agent", "web_operation_agent_odoo"):
        spec = importlib.util.find_spec(package)
        origin = Path(spec.origin).resolve() if spec is not None and spec.origin else None
        if origin is None or not origin.is_relative_to(root.resolve()):
            raise RuntimeError(f"{package} is not imported from the candidate checkout")


def addon_digest(app_container: str) -> str:
    mounts = json.loads(_output(["docker", "inspect", "--format", "{{json .Mounts}}",
                                 app_container]))
    roots = [host_mount_path(mount["Source"]) for mount in mounts
             if mount.get("Destination") == "/mnt/extra-addons"]
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError("the application addon mount cannot be fingerprinted")
    digest = hashlib.sha256()
    for path in sorted(roots[0].rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(str(path.relative_to(roots[0])).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def host_mount_path(source: str) -> Path:
    path = Path(source)
    if path.is_dir():
        return path
    # Docker Desktop may report a macOS bind mount through its Linux /host_mnt
    # prefix. Resolve only that explicit prefix; never guess another directory.
    if path.parts[:2] == ("/", "host_mnt"):
        return Path("/", *path.parts[2:])
    return path


def make_binding(*, root: Path, task_digest: str, reference_date: str,
                 db_container: str, database: str, app_container: str,
                 source_release: str) -> dict:
    day = current_reference_date(app_container)
    if day != reference_date:
        raise RuntimeError(f"holdout requires application date {reference_date}; got {day}")
    assert_runtime_checkout(root)
    return {
        "schema_version": 1,
        "candidate_commit": candidate_commit(root),
        "task_set_sha256": "sha256:" + task_digest,
        "reference_date": reference_date,
        "fixture_signature": fixture_signature(db_container, database),
        "application_container": _output(["docker", "inspect", "--format", "{{.Id}}",
                                          app_container]),
        "addon_digest": addon_digest(app_container),
        "source_release": source_release,
    }


def bind_output(destination: Path, binding: dict) -> None:
    path = destination / "run-binding.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != binding:
            raise RuntimeError("the resumed holdout no longer matches its original binding")
        return
    if any((destination / "attempts").glob("*.json")):
        raise RuntimeError("unbound prior attempts cannot be resumed")
    path.write_text(json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    path.chmod(0o600)


def recheck(binding: dict, *, app_container: str, db_container: str, database: str) -> None:
    if current_reference_date(app_container) != binding["reference_date"]:
        raise RuntimeError("the application date changed during the holdout")
    if fixture_signature(db_container, database) != binding["fixture_signature"]:
        raise RuntimeError("the database changed between holdout attempts")
    if _output(["docker", "inspect", "--format", "{{.Id}}", app_container]) != binding[
            "application_container"]:
        raise RuntimeError("the application container changed during the holdout")
    if addon_digest(app_container) != binding["addon_digest"]:
        raise RuntimeError("the deployed addon changed during the holdout")


def recheck_checkout(binding: dict, root: Path) -> None:
    assert_runtime_checkout(root)
    if candidate_commit(root) != binding["candidate_commit"]:
        raise RuntimeError("the candidate commit changed during the holdout")


__all__ = ["bind_output", "make_binding", "recheck", "recheck_checkout"]
