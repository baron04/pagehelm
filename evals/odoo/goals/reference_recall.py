"""Zero-model check of hard-reference recall on recorded goal runs.

For every recorded attempt whose declaration named a record (a `record` on some
deliverable) and that succeeded, cut the request's hard references by shape and ask the
Odoo port which records they name. Recall is the share of those declared records whose
reference was cut out and located to the same record. Nothing here calls a model.

    NO_PROXY=127.0.0.1,localhost PYTHONPATH=src:examples/odoo/src:. \\
        python -m evals.odoo.goals.reference_recall <run-dir>... --credentials .private/odoo-reader
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

from web_operation_agent.goal.narrow import locate, references
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent_odoo.goal import OdooGoalData
from web_operation_agent_odoo.server.executor import OdooBusinessToolExecutor
from web_operation_agent_odoo.server.identity import OdooApiKeyStore

ROOT = Path(__file__).resolve().parents[3]


def _norm(text: str) -> str:
    return "".join(text.casefold().split())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".private" / "odoo-reader")
    args = parser.parse_args(argv)
    client = OdooBusinessToolExecutor(
        base_url=args.base_url, keys=OdooApiKeyStore(args.credentials / "api-key.json"),
        timeout_s=20, allow_private_http=True)
    port = DataGoalPort(OdooGoalData(client))
    port.index()
    wanted = 0
    cut = 0
    located = 0
    misses = []
    seen = set()
    for run in args.runs:
        for path in sorted(glob.glob(str(run / "attempts" / "*.json"))):
            row = json.loads(Path(path).read_text())
            if not row.get("success") or not row.get("run_dir"):
                continue
            spec = None
            for line in Path(row["run_dir"], "audit.jsonl").read_text().splitlines():
                event = json.loads(line)
                if event.get("event") == "goal_declared":
                    spec = event.get("spec")
            if not spec:
                continue
            for d in spec.get("deliverables", []):
                record = str(d.get("record") or "").strip()
                if not record or (row["task"], record) in seen:
                    continue
                seen.add((row["task"], record))
                wanted += 1
                refs = references(row["prompt"])
                hit = next((r for r in refs if _norm(r) == _norm(record)
                            or _norm(record).startswith(_norm(r))), None)
                if hit is None:
                    misses.append((row["task"], record, "not cut", refs))
                    continue
                cut += 1
                known = locate(port, hit)
                if any(_norm(k.name).startswith(_norm(record)) or _norm(record) in _norm(k.name)
                       for k in known):
                    located += 1
                else:
                    misses.append((row["task"], record, "not located", [k.name for k in known]))
    print(f"declared records: {wanted}  cut by shape: {cut}  located: {located}  "
          f"recall {located / wanted:.1%}" if wanted else "no declared records")
    for miss in misses[:30]:
        print("  miss", miss)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
