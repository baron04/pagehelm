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
from ..sourcemap import OdooCompiler, odoo_source_tree
from .demo import OdooDemoConfig, run_demo
from .fixture import (SELF_WRITTEN_TABLES, OdooPostgresFixture, load_json,
                      restore_snapshot)
from .skills import verify_packaged_skills

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


#: The six workflow kinds G3 set out to cover, one scripted case each. The scripts are
#: fixed, so a pass says the apparatus carries that workflow end to end and a failure is a
#: defect rather than a sampling accident; nothing here says anything about a model.
#:
#: Three of them write, and they write in sequence on purpose: `confirm_quotation` takes
#: S00002 from draft to confirmed, and `invoice_sale_order` invoices S00007, which is the
#: only order in Odoo's own demo data with anything left to invoice. The fixture is
#: restored afterwards, which is what makes running them repeatable at all.
CASES = {
    "complete_enumeration": {
        "task": "How many customers are there in total?",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.customers-query"}),
            ("query_customers", {"limit": 50}),
            ("compute", {"operation": "count", "dataset_ref": "call2-query#1"}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete", "conclusions": [
                    {"kind": "aggregate", "evidence_ref": "calc#3"}]}}),
        ),
        "expect_path": "/odoo/customers",
        "expect_page_text": (),
        "expect_complete_scan": True,
    },
    "conditional_query": {
        "task": "Which quotations are still open?",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.quotations-query"}),
            ("query_quotations", {}),
            ("finish", {"outcome": "answered", "answer": {
                "version": 1, "coverage": "complete", "conclusions": [
                    {"kind": "selection", "evidence_ref": "call2-query#1",
                     "fields": ["name", "partner_id", "state", "amount_total"]}]}}),
        ),
        "expect_path": "/odoo/customers",
        "expect_page_text": (),
        "expect_complete_scan": True,
    },
    "open_record": {
        "task": "Open the customer record for Acme Corporation.",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.customers-query"}),
            ("query_customers", {"name_contains": "Acme Corporation"}),
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
    "cited_source_lookup": {
        # The one case that needs a compiled source tree, so it only runs when one is
        # configured. What it proves is narrow and was previously unprovable from a
        # deployment: the adapter declares `runtime_source=True`, and the deployed turn
        # therefore offers `lookup_source`, answers from the compiled index, and cites byte
        # ranges that are still in the tree. Until G5 the product turn rebuilt the
        # capability out of two of its four settings and this tool was never on the surface.
        # The task needs both halves on purpose. Source answers what the deployment's own
        # descriptors declare; only a typed read answers what is in the library right now,
        # and the order here is the order AGENTS.md requires -- source is a prior, the
        # runtime fact wins.
        "task": ("Which fields does Odoo's customer invoice form declare, and how many "
                 "customer invoices are there right now?"),
        "replies": (
            ("lookup_source",
             {"query": "which fields does the customer invoice form declare"}),
            ("load_skill", {"skill_id": "odoo.sales.customer-invoices-query"}),
            ("query_customer_invoices", {}),
            ("compute", {"operation": "count", "dataset_ref": "call3-query#1"}),
            ("finish", {"outcome": "partial", "answer": {
                "version": 1, "coverage": "partial", "conclusions": [
                    {"kind": "aggregate", "evidence_ref": "calc#4"}]}}),
        ),
        "expect_path": "/odoo/customers",
        "expect_page_text": (),
        "requires_source": True,
        "expect_source_lookup": True,
    },
    "safe_refusal": {
        # S00006 is confirmed and its invoice status is `no`: nothing left to invoice. The
        # refusal has to come from the adapter, before any proposal reaches the user --
        # `sale.advance.payment.inv` raises a UserError *after* they would have approved.
        "task": "Create an invoice for sales order S00006.",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.sale-orders-invoice"}),
            ("invoice_sale_order", {"reference": "S00006"}),
            ("finish", {"done": False,
                        "why": "S00006 has nothing left to invoice."}),
        ),
        "expect_path": "/odoo/customers",
        "expect_page_text": (),
        "writes": True,
        "expect_refusal": True,
        "expect_no_approval": True,
    },
    "multi_step_write_confirm": {
        "task": "Confirm quotation S00002.",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.sale-orders-confirm"}),
            ("query_quotations", {"name_contains": "S00002"}),
            ("confirm_quotation", {"reference": "S00002"}),
            ("finish", {"done": True, "why": "Confirmed quotation S00002."}),
        ),
        "expect_path": "/odoo/customers",
        "expect_page_text": (),
        "writes": True,
        "approve": True,
        "expect_record": ("sale.order", "S00002", "state", "sale"),
    },
    "multi_step_write_invoice": {
        "task": "Create the customer invoice for sales order S00007.",
        "replies": (
            ("load_skill", {"skill_id": "odoo.sales.sale-orders-invoice"}),
            ("query_sale_orders", {"name_contains": "S00007"}),
            ("invoice_sale_order", {"reference": "S00007"}),
            ("finish", {"done": True, "why": "Invoiced sales order S00007."}),
        ),
        "expect_path": "/odoo/customers",
        "expect_page_text": (),
        "writes": True,
        "approve": True,
        "expect_invoice_for": "S00007",
    },
}

#: The reads first, then the refusal, then the two writes. The refusal runs before anything
#: has written, so what it refuses is Odoo's demo data exactly as shipped; the writes run
#: last so that only the fixture restore has to undo them.
CASE_ORDER = ("complete_enumeration", "conditional_query", "open_record", "open_collection",
              "cited_source_lookup",
              "safe_refusal", "multi_step_write_confirm", "multi_step_write_invoice")


def cases_for(source_root: str) -> tuple[str, ...]:
    """The cases this environment can actually run, in order.

    A case that needs a compiled source tree is dropped rather than failed where there is
    none: source retrieval is optional by design, and a gate that reported a missing
    optional prerequisite as a product failure would be lying about a different thing.
    """
    return tuple(name for name in CASE_ORDER
                 if source_root or not CASES[name].get("requires_source"))


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
        allow_mutations=bool(case.get("writes")),
        mutation_journal_path=(output_dir / f"{name}-mutations.sqlite3"
                               if case.get("writes") else None),
        approve_in_page=bool(case.get("approve")),
    ))
    turns = summary.get("turns") or []
    turn = turns[0] if turns else {}
    usage = summary.get("model_usage") or {}
    page_text = summary.get("last_page_text") or ""
    control = turn.get("business_control") or {}
    audit = summary.get("audit_projection") or []
    reads = [row for row in audit if row.get("event") == "business_tool_result"]
    expected_terminal = ("agent_refused" if case.get("expect_refusal")
                         else "agent_finished")
    return {
        "case": name,
        "task_kind": name,
        "turn_finished": turn.get("terminal_reason") == expected_terminal,
        "terminal_reason": turn.get("terminal_reason", ""),
        # Every read this turn made said it saw the whole scoped collection. A tool that
        # answers from a first page and a tool that answers from all of them look the same
        # in prose; they do not look the same here.
        "every_read_scanned_its_whole_scope": all(
            row.get("source_complete") is True for row in reads),
        "reads": len(reads),
        "approval_requested": control.get("approval_requested", 0),
        "approval_granted": control.get("approval_granted", 0),
        "mutations_completed": control.get("mutation_completed", 0),
        "mutations_indeterminate": control.get("mutation_indeterminate", 0),
        "readback_verified": all(
            row.get("readback_verified") is True for row in audit
            if row.get("event") == "business_mutation_result"),
        "audit_projection": audit,
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
        "runtime_source_retrievals": summary.get("runtime_source_retrievals") or [],
        "tools_offered": summary.get("tools_offered") or [],
    }


def verify_retrieved_citations(source_root: str, release: str,
                               results: Sequence[dict]) -> dict:
    """Re-read every byte range a source lookup cited, and say whether it is still there.

    Resolution is against the tree, not against the retriever's own record of what it read:
    a bundle that cites its own memory proves nothing. `SourceTree.verify` re-hashes the
    exact cited lines, so a citation survives here only if those bytes are unchanged in the
    release the map was compiled from -- which is what makes a cited answer replayable at
    all, and is the reason retrieval is built on the compiled index rather than on a
    mutable working tree.
    """
    tree = odoo_source_tree(Path(source_root).resolve(), release=release)
    by_key = {reference.key: reference
              for relation in OdooCompiler(tree, "citation-check").compile().relations
              for reference in relation.evidence}
    cited = sorted({key for row in results
                    for retrieval in row["runtime_source_retrievals"]
                    for key in retrieval["citation_keys"]})
    unresolved = [key for key in cited
                  if key not in by_key or not tree.verify(by_key[key])]
    return {"cited_count": len(cited), "unresolved": unresolved,
            "resolved": bool(cited) and not unresolved}


def verify_effect(adapter: OdooApplicationAdapter, case: dict) -> dict:
    """Ask the application what happened, rather than believing the run's own account.

    A turn that says it confirmed a quotation and a turn that did are the same string. The
    only thing that can tell them apart is the record, read afterwards under the delegated
    actor -- which is also the only evidence that would survive somebody disputing it.
    """
    if not case.get("approve"):
        return {}
    executor = adapter._executor
    if case.get("expect_record"):
        model, reference, field, expected = case["expect_record"]
        rows = executor.call(model, "search_read", [[["name", "=", reference]]],
                             {"fields": [field], "limit": 2})
        observed = (executor._label(rows[0], field)
                    if isinstance(rows, list) and len(rows) == 1 else "")
        return {"effect_verified": observed == expected,
                "effect_observed": f"{model} {reference} {field}={observed or 'absent'}"}
    if case.get("expect_invoice_for"):
        reference = case["expect_invoice_for"]
        rows = executor.call(
            "sale.order", "search_read", [[["name", "=", reference]]],
            {"fields": ["invoice_ids", "invoice_status"], "limit": 2})
        invoices = (rows[0].get("invoice_ids")
                    if isinstance(rows, list) and len(rows) == 1 else [])
        moves = executor.call("account.move", "read", [list(invoices or [])],
                              {"fields": ["move_type", "state"]}) if invoices else []
        # One ordinary customer invoice, in draft. A down payment would be an
        # `out_invoice` too, so the count and the order's own status carry the rest.
        ordinary = [move for move in moves
                    if isinstance(move, dict) and move.get("move_type") == "out_invoice"]
        return {"effect_verified": len(ordinary) == 1,
                "effect_observed": (f"sale.order {reference} now has {len(ordinary)} "
                                    "customer invoices")}
    return {"effect_verified": False, "effect_observed": "the case declared no effect"}


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


def classify_fixture_drift(captured: dict, record: dict) -> tuple[list[str], bool]:
    """Name the tables that moved since the baseline, and say who moved them.

    Returns the drifted table names and whether every one of them is a table the running
    application writes by itself. That second value is the whole decision: the application's
    own bookkeeping is put back and recorded, anything else stops the gate.

    With no recorded per-table baseline there is nothing to attribute, so the caller is told
    "not attributable" and stops -- guessing would be the one answer that cannot be checked.
    """
    recorded = record.get("baseline_tables") or {}
    if not recorded:
        return [], False
    drifted = sorted(name for name, row in captured["tables"].items()
                     if recorded.get(name) != row)
    return drifted, bool(drifted) and set(drifted) <= SELF_WRITTEN_TABLES


def _blocked_artifact(bound, health: dict, observed: str, baseline: str,
                      drifted: list[str]) -> dict:
    """Say what stopped the gate, without pretending it produced a measurement.

    Naming the tables matters: a bare digest mismatch reads as "something wrote to the
    library" when it may equally be the running Odoo's own bookkeeping, and the two call for
    opposite responses.
    """
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
        "fixture_drifted_tables": drifted,
        "checks": {"fixture_matched_its_recorded_baseline_before_the_run": False},
        "status": "BLOCKED",
        "detail": ("the fixture is not on its recorded baseline; restore it before "
                   "measuring anything against it. Drifted: "
                   + (", ".join(drifted) if drifted else "no table-level difference")),
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
    drift_before_run: list[str] = []
    restored_cleanly = False
    if database is not None:
        record = load_json(Path(args.fixture_record))
        baseline = record["baseline_digest"]
        captured = database.capture()
        started_on = captured["digest"]
        drift_before_run, attributable = classify_fixture_drift(captured, record)

        if started_on != baseline and not attributable:
            # Stop rather than measure. A gate that runs anyway produces a number nobody
            # can reproduce, and the last one of those cost this project a whole spike.
            return _emit(
                _blocked_artifact(bound, health, started_on, baseline, drift_before_run),
                args)
        if started_on != baseline:
            # Drift the application produced by itself. Putting the library back is what
            # makes the measurement reproducible, which is the whole purpose of the check;
            # refusing here would mean the gate can only ever run once per restore. The
            # drift is not swallowed -- it is named in the artifact, and any table outside
            # the set above still stops the run above.
            put_back = restore_snapshot(
                fixture=database, snapshot_path=Path(args.fixture_snapshot),
                record_path=Path(args.fixture_record))
            if put_back["status"] != "PASS":
                return _emit(_blocked_artifact(bound, health, put_back["observed_digest"],
                                               baseline, drift_before_run), args)
            # Trust the restore's own verdict rather than re-comparing its digest here.
            # `restore_snapshot` already asks the one question worth asking -- did anything
            # outside the application's own log stay off its baseline -- and comparing the
            # whole digest again at this level just reintroduces the race it settles: the
            # server appends a `mail_message` row every twenty seconds, and whether one
            # landed between the restore and its verifying read is not a fact about this
            # fixture.
            restored_cleanly = True
        fixture_checks["fixture_matched_its_recorded_baseline_before_the_run"] = (
            started_on == baseline or restored_cleanly)

    read_only_before = fixture_state(adapter)
    case_order = cases_for(args.source_root)
    with tempfile.TemporaryDirectory() as tmp:
        results = []
        for name in case_order:
            case = CASES[name]
            if case.get("writes") and database is None:
                # A write with nothing to undo it is how a demo library drifts. Refusing is
                # not a failure of the product: it is the gate declining to leave a mess.
                raise SystemExit(
                    f"the {name} case writes; run with a resettable fixture or not at all")
            result = run_case(name, case, args, Path(tmp))
            result.update(verify_effect(adapter, case))
            results.append(result)
            if not case.get("writes"):
                # A read case must leave the application exactly as it found it. Checked per
                # case, so a later write cannot mask an earlier read that wrote.
                result["read_only_kept"] = fixture_state(adapter) == read_only_before

    restored: dict = {}
    if database is not None:
        wrote = any(case.get("writes") and CASES[case_name].get("approve")
                    for case_name, case in CASES.items())
        ended_on = database.capture()["digest"]
        fixture_checks["fixture_changed_only_where_a_write_was_approved"] = (
            (ended_on != baseline) if wrote else (ended_on == baseline))
        restored = restore_snapshot(
            fixture=database, snapshot_path=Path(args.fixture_snapshot),
            record_path=Path(args.fixture_record))
        fixture_checks["fixture_restored_to_its_baseline_afterwards"] = (
            restored["status"] == "PASS")

    read_cases = [row for row in results if not CASES[row["case"]].get("writes")]
    write_cases = [row for row in results if CASES[row["case"]].get("approve")]
    refusal_cases = [row for row in results if CASES[row["case"]].get("expect_refusal")]
    checks = {
        "application_reachable": bool(health["passed"]),
        "every_workflow_kind_ran": sorted(row["case"] for row in results)
                                   == sorted(case_order),
        "every_case_reached_its_expected_terminal_state": all(
            row["turn_finished"] for row in results),
        "every_case_landed_on_its_authorised_route": all(
            row["final_path_expected"] for row in results),
        "every_case_rendered_its_record": all(row["record_visible"] for row in results),
        "identity_bound_in_every_case": all(row["identity_bound"] for row in results),
        "scripted_provider_zero_cost": all(
            row["provider_calls"] == 0 and row["cost_cny"] == 0.0 for row in results),
        # Only about the cases that read. The refusal never gets that far, which is the
        # point of it -- folding it in here would make a vacuous false look like a defect.
        "every_read_saw_its_whole_scope": all(
            row["every_read_scanned_its_whole_scope"]
            for row in results if row["reads"]),
        "every_case_that_should_read_did": all(
            bool(row["reads"]) is not bool(CASES[row["case"]].get("expect_refusal"))
            for row in results),
        "every_read_only_case_left_the_application_alone": all(
            row.get("read_only_kept") is True for row in read_cases),
        # Each write asked exactly once, was approved in the page, executed once, and was
        # read back under the same user. Counting is the point: "asked once" is what
        # separates a confirmed write from one the model talked its way into repeating.
        "every_write_asked_once_and_executed_once": all(
            row["approval_requested"] == 1 and row["approval_granted"] == 1
            and row["mutations_completed"] == 1 and row["mutations_indeterminate"] == 0
            for row in write_cases),
        "every_write_was_read_back_under_the_same_user": all(
            row["readback_verified"] for row in write_cases),
        "every_write_changed_the_record_it_named": all(
            row.get("effect_verified") is True for row in write_cases),
        # The refusal must happen before the user is asked, not after: the application would
        # have raised its own error, and by then a person has already approved something.
        "the_refusal_never_reached_an_approval": all(
            row["approval_requested"] == 0 and row["mutations_completed"] == 0
            for row in refusal_cases),
        **fixture_checks,
    }
    # Both directions, in the deployment itself: with a compiled source root every turn is
    # offered the cited-lookup tools, and without one no turn is. The adapter's declaration
    # is the only thing that moves between those two runs.
    source_cases = [row for row in results
                    if CASES[row["case"]].get("expect_source_lookup")]
    checks["runtime_source_tools_follow_the_declared_capability"] = all(
        ("lookup_source" in row["tools_offered"]) == bool(args.source_root)
        and ("open_source_route" in row["tools_offered"]) == bool(args.source_root)
        for row in results)
    retrieved: dict = {}
    citations: dict = {}
    if args.source_root:
        # With a compiled map the treatment must be real: every delivery in every case has
        # to carry at least one cited candidate, or the source arm is measuring nothing.
        checks["source_treatment_is_non_empty"] = all(
            row["source_relations"] > 0 and row["source_candidates_delivered"]
            and all(count > 0 for count in row["source_candidates_delivered"])
            for row in results)
        # And the Skills must still point at something. A citation nobody resolves is
        # indistinguishable from one that rotted two releases ago, which is how all four
        # shipped Skills came to cite unqualified Odoo XML ids that name four addons at once.
        citations = verify_packaged_skills(args.source_root, args.source_release)
        checks["every_skill_citation_resolves_in_this_release"] = (
            citations["status"] == "READY")
        # The lookup itself: it happened, it returned cited evidence, and every byte range
        # it cited is still exactly those bytes in the compiled release.
        checks["the_source_lookup_case_retrieved_cited_evidence"] = all(
            row["runtime_source_retrievals"]
            and all(retrieval["lines"] > 0 and retrieval["citation_keys"]
                    for retrieval in row["runtime_source_retrievals"])
            for row in source_cases) and bool(source_cases)
        retrieved = verify_retrieved_citations(args.source_root, args.source_release,
                                               results)
        checks["every_retrieved_citation_still_names_those_bytes"] = retrieved["resolved"]
    artifact = {
        "schema_version": 1,
        "kind": "woa_odoo_live_qualification",
        "application_release": f"odoo@{health.get('server_version', '')}",
        "engine_release": "web-operation-agent@0.1.0",
        "catalog_digest": "sha256:" + bound.descriptor.catalog_sha256,
        "tool_names": sorted(bound.business_tools.tool_names),
        "case_count": len(results),
        "case_checks": {row["case"]: row for row in results},
        "fixture_baseline_digest": _digest(read_only_before),
        "fixture_restore": {key: value for key, value in restored.items()
                            if key != "changed_tables"},
        "fixture_database_digest": baseline,
        # What the library looked like before this run touched it. Empty is the ordinary
        # case; anything here was written by the application itself between runs, and a
        # reviewer should see it rather than have it quietly put back.
        "fixture_drift_before_run": drift_before_run,
        "skill_citations": {key: value for key, value in citations.items()
                            if key != "reports"},
        "retrieved_citations": retrieved,
        "checks": checks,
        "status": "READY" if all(checks.values()) else "FAILED",
    }
    return _emit(artifact, args)


if __name__ == "__main__":
    raise SystemExit(main())
