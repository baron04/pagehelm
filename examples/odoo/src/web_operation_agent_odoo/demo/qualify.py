"""Model-free qualification: real Odoo, real Chromium, the real product turn.

Two scripted cases -- a typed read and an exact-route open -- run through the same
composition a deployment uses. Nothing here asserts anything about a model: the chat is a
fixed script, so a pass means the *apparatus* holds end to end, and a failure is a real
defect rather than a sampling accident.

The read-only guarantee is checked, not assumed: the record counts and a content digest are
taken before and after and must be identical. The artifact contains counts, digests and
booleans only.

    python -m web_operation_agent_odoo.demo.qualify \\
      --base-url http://127.0.0.1:8069 \\
      --api-key .private/odoo-api-key.json \\
      --storage-state .private/odoo-storage-state.json \\
      --output .artifacts/odoo/live-qualification.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from web_operation_agent.application import bind_application

from ..adapter import COLLECTIONS, OdooApplicationAdapter
from .demo import OdooDemoConfig, run_demo
from .fixture import OdooPostgresFixture, load_json, restore_snapshot

#: A read-only run must leave every collection exactly as it found it.
_FIXTURE_FIELDS = ("id", "name")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def fixture_state(adapter: OdooApplicationAdapter) -> dict:
    """Counts and a content digest per collection, read under the delegated actor."""
    executor = adapter._executor
    state = {}
    for collection in COLLECTIONS:
        rows, total, _scan = executor._scan(collection)
        state[collection.tool] = {
            "model": collection.model,
            "count": total,
            "digest": _digest([[row.get(field) for field in _FIXTURE_FIELDS]
                               for row in sorted(rows, key=lambda item: item["id"])]),
        }
    return state


CASES = {
    "open_record": {
        "task": "Open the customer record for Acme Corporation.",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.customers-query"}),
            ("query_customers", {"name_contains": "Acme"}),
            ("goto", {"url": "/odoo/customers/10"}),
            ("wait_for", {"text": "Acme"}),
            ("finish", {"done": True, "why": "Opened Acme Corporation's record."}),
        ),
        "expect_path": "/odoo/customers/10",
        "expect_page_text": ("Acme Corporation",),
    },
    "open_collection": {
        "task": "Show me the confirmed sales orders.",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.sale-orders-query"}),
            ("query_sale_orders", {}),
            ("goto", {"url": "/odoo/orders"}),
            ("wait_for", {"text": "S00"}),
            ("finish", {"done": True, "why": "Opened the confirmed sales orders."}),
        ),
        "expect_path": "/odoo/orders",
        "expect_page_text": ("S00",),
    },
}


def run_case(name: str, case: dict, args, output_dir: Path) -> dict:
    summary = run_demo(OdooDemoConfig(
        base_url=args.base_url,
        api_key_path=Path(args.api_key),
        storage_state_path=Path(args.storage_state),
        page_controller_module=args.page_controller_module,
        entry_path=args.entry_path,
        source_root=Path(args.source_root) if args.source_root else None,
        source_release=args.source_release,
        scripted=True, headless=True,
        scripted_task=case["task"],
        scripted_replies=case["replies"],
        output_dir=output_dir,
        turn_timeout_s=args.turn_timeout,
        keep_open_seconds=0.0,
    ))
    turns = summary.get("turns") or []
    turn = turns[0] if turns else {}
    usage = summary.get("model_usage") or {}
    page_text = summary.get("last_page_text") or ""
    return {
        "case": name,
        "task_kind": name,
        "turn_finished": turn.get("terminal_reason") == "agent_finished",
        "final_path": str(summary.get("last_url", "")).split(args.base_url, 1)[-1],
        "final_path_expected": str(summary.get("last_url", "")).endswith(
            case["expect_path"]),
        "record_visible": all(needle in page_text for needle in case["expect_page_text"]),
        "identity_bound": summary.get("identity_binding") == "principal_bound",
        "provider_calls": usage.get("provider_calls"),
        "cost_cny": usage.get("cost_cny"),
        "page_actions": turn.get("page_actions"),
        "task_digest": _digest(turn.get("task", "")),
        "answer_digest": _digest(turn.get("answer", "")),
        "source_relations": summary.get("source_relations", 0),
        "source_candidates_delivered": summary.get("source_candidates_delivered") or [],
    }


def _database_fixture(args) -> OdooPostgresFixture | None:
    """The resettable fixture, or None when this environment has no Postgres to reach."""
    supplied = (args.fixture_container, args.fixture_database, args.fixture_snapshot,
                args.fixture_record)
    if not any(supplied):
        return None
    if not all(supplied):
        raise SystemExit("the fixture needs container, database, snapshot and record")
    return OdooPostgresFixture(container=args.fixture_container,
                              database=args.fixture_database,
                              db_user=args.fixture_db_user)


def _blocked_artifact(bound, health: dict, observed: str, baseline: str) -> dict:
    """Say what stopped the gate, without pretending it produced a measurement."""
    return {
        "schema_version": 1,
        "kind": "woa_odoo_live_qualification",
        "application_release": f"odoo@{health.get('server_version', '')}",
        "engine_release": "web-operation-agent@0.1.0",
        "catalog_digest": "sha256:" + bound.descriptor.catalog_sha256,
        "tool_names": sorted(bound.business_tools.tool_names),
        "case_count": 0,
        "case_checks": {},
        "fixture_database_digest": observed,
        "fixture_expected_digest": baseline,
        "checks": {"fixture_matched_its_recorded_baseline_before_the_run": False},
        "status": "BLOCKED",
        "detail": ("the fixture is not on its recorded baseline; restore it before "
                   "measuring anything against it"),
    }


def _emit(artifact: dict, args) -> int:
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if artifact["status"] == "READY" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-qualify",
        description="Model-free live qualification for the Odoo reference integration.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--storage-state", required=True)
    parser.add_argument("--page-controller-module", default=(
        "http://127.0.0.1:8069/woa_runtime/static/src/page-controller.js"))
    parser.add_argument("--entry-path", default="/odoo/customers")
    parser.add_argument("--source-root", default="",
                        help="an Odoo source tree matching the deployed release")
    parser.add_argument("--source-release", default="")
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    # The resettable fixture. Optional only so the gate still runs where Postgres is not
    # reachable; when it is supplied the gate refuses to start on a drifted library, which
    # is the whole point -- a live result computed against an unknown baseline measures
    # nothing, and G2's spike left exactly such a baseline behind.
    parser.add_argument("--fixture-container", default="")
    parser.add_argument("--fixture-database", default="")
    parser.add_argument("--fixture-db-user", default="odoo")
    parser.add_argument("--fixture-snapshot", default="")
    parser.add_argument("--fixture-record", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    adapter = OdooApplicationAdapter(
        base_url=args.base_url, api_key_path=Path(args.api_key),
        page_controller_module=args.page_controller_module)
    bound = bind_application(adapter)
    health = adapter.health()

    database = _database_fixture(args)
    baseline = ""
    fixture_checks: dict = {}
    if database is not None:
        baseline = load_json(Path(args.fixture_record))["baseline_digest"]
        started_on = database.capture()["digest"]
        fixture_checks["fixture_matched_its_recorded_baseline_before_the_run"] = (
            started_on == baseline)
        if started_on != baseline:
            # Stop rather than measure. A gate that runs anyway produces a number nobody
            # can reproduce, and the last one of those cost this project a whole spike.
            return _emit(_blocked_artifact(bound, health, started_on, baseline), args)

    before = fixture_state(adapter)
    with tempfile.TemporaryDirectory() as tmp:
        results = [run_case(name, case, args, Path(tmp))
                   for name, case in CASES.items()]
    after = fixture_state(adapter)

    if database is not None:
        ended_on = database.capture()["digest"]
        fixture_checks["fixture_returned_to_its_baseline"] = ended_on == baseline
        if ended_on != baseline:
            # Every case here is read-only, so this is a defect report, not a cleanup step.
            # Restore anyway: leaving a drifted library behind is how the next run inherits
            # this one's mistake.
            recovery = restore_snapshot(
                fixture=database, snapshot_path=Path(args.fixture_snapshot),
                record_path=Path(args.fixture_record))
            fixture_checks["fixture_was_restored_after_an_unexpected_write"] = (
                recovery["status"] == "PASS")

    checks = {
        "application_reachable": bool(health["passed"]),
        "every_case_finished": all(row["turn_finished"] for row in results),
        "every_case_landed_on_its_authorised_route": all(
            row["final_path_expected"] for row in results),
        "every_case_rendered_its_record": all(row["record_visible"] for row in results),
        "identity_bound_in_every_case": all(row["identity_bound"] for row in results),
        "scripted_provider_zero_cost": all(
            row["provider_calls"] == 0 and row["cost_cny"] == 0.0 for row in results),
        "fixture_unchanged": before == after,
        **fixture_checks,
    }
    if args.source_root:
        # With a compiled map the treatment must be real: every delivery in every case has
        # to carry at least one cited candidate, or the source arm is measuring nothing.
        checks["source_treatment_is_non_empty"] = all(
            row["source_relations"] > 0 and row["source_candidates_delivered"]
            and all(count > 0 for count in row["source_candidates_delivered"])
            for row in results)
    artifact = {
        "schema_version": 1,
        "kind": "woa_odoo_live_qualification",
        "application_release": f"odoo@{health.get('server_version', '')}",
        "engine_release": "web-operation-agent@0.1.0",
        "catalog_digest": "sha256:" + bound.descriptor.catalog_sha256,
        "tool_names": sorted(bound.business_tools.tool_names),
        "case_count": len(results),
        "case_checks": {row["case"]: row for row in results},
        "fixture_baseline_digest": _digest(before),
        "fixture_database_digest": baseline,
        "checks": checks,
        "status": "READY" if all(checks.values()) else "FAILED",
    }
    return _emit(artifact, args)


if __name__ == "__main__":
    raise SystemExit(main())
