"""Create and verify a model-free qualification for the sealed v4 goal holdout."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from evals.odoo.goals.binding import make_binding, recheck, recheck_checkout
from evals.odoo.goals.run_goals import ROOT, TASKSETS, load_tasks

_KINDS = {
    "answer": ("evals.odoo.goals.verify_v4_answers", 58),
    "page": ("evals.odoo.goals.verify_v4_pages", 58),
    "limitation": ("evals.odoo.goals.verify_v4_limitations", 15),
}


def verify_qualification(path: Path, *, binding: dict, tasks: list[dict]) -> None:
    if not path.is_file():
        raise RuntimeError("the v4 holdout needs its zero-model qualification")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1 or report.get("binding") != binding:
        raise RuntimeError("the v4 oracle qualification is for a different candidate or fixture")
    wanted = {
        "answer": {task["id"] for task in tasks if task["category"] == "answer"},
        "page": {task["id"] for task in tasks if task["category"] in ("open", "handoff")},
        "limitation": {task["id"] for task in tasks if task["category"] == "impossible"},
    }
    for kind, (_, count) in _KINDS.items():
        record = (report.get("reports") or {}).get(kind) or {}
        artifact = Path(record.get("path") or "")
        if not artifact.is_file() or len(wanted[kind]) != count:
            raise RuntimeError(f"the {kind} oracle report is missing or incomplete")
        raw = artifact.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            raise RuntimeError(f"the {kind} oracle report changed")
        rows = json.loads(raw)
        if len(rows) != count or {row.get("id") for row in rows} != wanted[kind] or not all(
                row.get("ok") is True for row in rows):
            raise RuntimeError(f"the {kind} oracle qualification did not pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".private" / "odoo-reader")
    parser.add_argument("--db-container", default="woa-odoo-reference-db-1")
    parser.add_argument("--app-container", default="woa-odoo-reference-odoo-1")
    parser.add_argument("--database", default="woa_reference")
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in args.model_env.read_text().splitlines()
               if "=" in line and not line.lstrip().startswith("#"))
    doc = load_tasks("v4")
    binding = make_binding(root=ROOT, task_digest=TASKSETS["v4"][1],
                           reference_date=doc["reference_date"],
                           db_container=args.db_container, database=args.database,
                           app_container=args.app_container,
                           source_release=env["WOA_ODOO_SOURCE_RELEASE"].strip())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    reports = {}
    for kind, (module, _) in _KINDS.items():
        artifact = args.output.parent / f"v4-{kind}-oracles.json"
        command = [sys.executable, "-m", module, "--output", str(artifact)]
        if kind == "answer":
            command += ["--db-container", args.db_container, "--database", args.database]
        elif kind == "page":
            command += ["--base-url", args.base_url, "--storage-state",
                        str(args.credentials / "storage-state.json")]
        else:
            command += ["--db-container", args.db_container, "--database", args.database,
                        "--base-url", args.base_url, "--storage-state",
                        str(args.credentials / "storage-state.json")]
        subprocess.run(command, check=True)
        reports[kind] = {"path": str(artifact.resolve()),
                         "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        recheck(binding, app_container=args.app_container,
                db_container=args.db_container, database=args.database)
        recheck_checkout(binding, ROOT)
    report = {"schema_version": 1, "binding": binding, "reports": reports}
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
                           + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    verify_qualification(args.output, binding=binding, tasks=doc["tasks"])
    print(json.dumps({"qualified": True, "tasks": len(doc["tasks"]),
                      "candidate_commit": binding["candidate_commit"],
                      "fixture_signature": binding["fixture_signature"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
