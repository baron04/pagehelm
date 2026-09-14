"""P7.4a zero-model apparatus qualification of the ``p74_odoo_v1`` evaluation.

Every decision is scripted: no external model is contacted and no spend ledger is touched. The
qualification runs the real product, real Chromium, the real Odoo fixture, the gateway and
attempt ledger, the independent evaluator and the reset, and records only content-free
checks, counts and digests. It proves the apparatus can tell a correct result from a wrong
one and a product failure from an apparatus failure; it says nothing about how well any model
or engine performs. It also checks the formal closure on this checkout: the gateway's request
contract, the upstream provider path against a loopback responder, the runtime provider binding,
the candidate and fixture every header binds, forged bindings, report gating, the development
and held-out chain rules, the freeze state machine, the escrow and its custody, and the formal
entry's refusals.

    python -m evals.odoo.qualify run
    python -m evals.odoo.qualify sample d01 --arm woa_product
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import (canary, equivalence, escrow, fixture, formal, freeze, harness, leakcheck, ledger,
               population as population_module, protocol, report, scripted, tasks)
from .driver import AttemptPlan
from .gateway import (PAGEAGENT_ENGINE_FIELDS, SCRIPTED_MODEL, GatewayBudgets,
                      GatewayCompletionClient, ProviderGateway, ScriptedBackend, UpstreamBackend,
                      provider_binding_digest)
from .harness import (PRIVATE, Context, batch_bindings, finish_batch, fixture_manifest_path,
                      load_context, protocol_budgets)
from .projection import actor_projection
from .rpc import OdooActorClient
from .stack import REPOSITORY, Stack

ADAPTER_IN_IMAGE = "/usr/local/lib/python3.12/site-packages/web_operation_agent_odoo/adapter.py"
QUOTATION_DOMAIN = 'domain=[("state", "in", ("draft", "sent"))],'
MUTATED_QUOTATION_DOMAIN = 'domain=[("state", "in", ("draft", "sent", "sale"))],'


# -- scripted programs ---------------------------------------------------------------------

S = scripted


def woa(turns: dict[str, tuple]) -> Callable:
    return S.WoaProgram(turns).policy()


def c_program(ctx: Context, task_id: str, *, variant: str = "") -> Callable:
    runner, _ = ctx.inputs(task_id)
    first = runner.messages[0]
    if task_id == "d01":
        name = ctx.record("customer.lookup")["name"]
        fields = ["phone"] if variant == "missing_field" else ["email", "phone"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": name}),
                            S.finish_fields(name, fields))})
    if task_id == "d02":
        name = ctx.record("customer.lookup_page")["name"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": name}),
                            S.goto_route_of(name),
                            S.finish_acted(f"已打开客户 {name} 的详情页。"))})
    if task_id == "d07":
        return woa({first: (S.load_skill_with("query_quotations"),
                            S.call("query_quotations", {"limit": 50}),
                            S.compute("filter", field="state", comparison="eq",
                                      threshold="sent"),
                            S.compute("count", over="SelectionEvidence"),
                            S.finish_aggregate())})
    if task_id == "d08":
        if variant == "no_currency_filter":
            # Drops the task's currency qualifier: sums every open quotation.
            return woa({first: (S.load_skill_with("query_quotations"),
                                S.call("query_quotations", {"limit": 50}),
                                S.compute("sum", field="amount_total"),
                                S.finish_aggregate())})
        return woa({first: (S.load_skill_with("query_quotations"),
                            S.call("query_quotations", {"limit": 50}),
                            S.compute("filter", field="currency_id", comparison="eq",
                                      threshold="EUR"),
                            S.compute("sum", field="amount_total", over="SelectionEvidence"),
                            S.finish_aggregate())})
    if task_id == "d11":
        prefix = ctx.population["roles"]["customer.ambiguous_prefix"]
        choice = ctx.record("customer.ambiguous_b")["name"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": prefix}),
                            S.ask_choices("请选择要打开的客户。")),
                    runner.messages[1]: (S.load_skill_with("query_customers"),
                                         S.call("query_customers", {"name_contains": choice}),
                                         S.goto_route_of(choice),
                                         S.finish_acted(f"已打开客户 {choice}。"))})
    if task_id == "d13":
        order = ctx.record("order.related")["name"]
        customer = ctx.record("customer.related_order")["name"]
        return woa({first: (S.load_skill_with("query_sale_orders"),
                            S.call("query_sale_orders", {"name_contains": order}),
                            S.finish_fields(order, ["partner_id"])),
                    runner.messages[1]: (S.load_skill_with("query_customers"),
                                         S.call("query_customers", {"name_contains": customer}),
                                         S.finish_fields(customer, ["city"]))})
    if task_id in {"d16", "d19"}:
        role = "new.create_customer" if task_id == "d16" else "new.missing_email"
        record = ctx.population["new_records"][role]
        return woa({first: (S.load_skill_with("create_customer", writes=True),
                            S.call("create_customer", {"name": record["name"],
                                                       "email": record["email"]}),
                            S.finish_acted(f"已创建客户 {record['name']}，邮箱 "
                                           f"{record['email']}。"))})
    if task_id == "d18":
        reference = ctx.record("order.to_invoice")["name"]
        return woa({first: (S.load_skill_with("invoice_sale_order", writes=True),
                            S.call("invoice_sale_order", {"reference": reference}),
                            S.finish_acted(f"已为销售订单 {reference} 创建客户发票。"))})
    if task_id == "d20":
        reference = ctx.record("quotation.deny")["name"]
        return woa({first: (S.load_skill_with("confirm_quotation", writes=True),
                            S.call("confirm_quotation", {"reference": reference}),
                            S.finish_refused("The confirmation was declined."))})
    if task_id == "d21":
        return woa({first: (S.finish_refused("当前账号没有权限创建客户，未执行任何写入。"),)})
    if task_id == "d22":
        existing = ctx.record("customer.duplicate_name")["name"]
        email = ctx.population["new_records"]["new.duplicate_email_name"]["email"]
        return woa({first: (S.load_skill_with("create_customer", writes=True),
                            S.call("create_customer", {"name": existing, "email": email}),
                            S.finish_refused("A customer with that name already exists."))})
    if task_id == "d24":
        name = ctx.record("customer.other_company_1")["name"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": name}),
                            S.finish_not_found())})
    if task_id == "d26":
        order = ctx.record("order.related")["name"]
        customer = ctx.record("customer.related_order")["name"]
        return woa({first: (S.load_skill_with("query_sale_orders"),
                            S.call("query_sale_orders", {"name_contains": order}),
                            S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": customer}),
                            S.goto_route_of(customer), S.finish_acted(f"已打开客户 {customer} 的页面。"))})
    if task_id == "d30":
        return woa({first: (S.load_skill_with("query_sale_orders"),
                            S.call("query_sale_orders", {"date_from": "2026-03-01",
                                                         "date_until": "2026-04-01",
                                                         "limit": 50}),
                            S.compute("count"), S.goto_collection(),
                            S.finish_acted_with("aggregate", over="ComputationEvidence"))})
    raise KeyError(f"no WOA qualification program for {task_id}")


def a_program(ctx: Context, task_id: str, *, variant: str = "") -> Callable:
    if task_id == "d01":
        record = ctx.record("customer.lookup")
        email = (ctx.record("customer.lookup_page")["email"] if variant == "wrong_email"
                 else record["email"])
        text = f"{record['name']} 的邮箱是 {email}，电话是 {record['phone']}。"
        return S.PageAgentProgram((S.done_text(text),)).policy()
    if task_id == "d06":
        count = sum(1 for item in ctx.population["customers"]
                    if item["company"] == "main" and item["customer_rank"] > 0)
        return S.PageAgentProgram((S.done_text(f"当前一共有 {count} 个客户。"),)).policy()
    if task_id == "d03":
        return S.PageAgentProgram((
            S.click_text(r"title=Home Menu"), S.click_text(r">Invoicing"),
            S.click_text(r">Customers"), S.click_text(r">Invoices\b"),
            S.done_text("The customer invoices list is open."))).policy()
    raise KeyError(f"no PageAgent qualification program for {task_id}")


# -- batches -----------------------------------------------------------------------------------

@dataclass
class SlotSpec:
    task_id: str
    arm: str
    expect: str
    variant: str = ""
    faults: dict[int, str] | None = None
    models: dict[int, str] | None = None
    inject: frozenset[str] = frozenset()
    oracle_override: dict[str, Any] | None = None


def _plan(ctx: Context, spec: SlotSpec, slot: dict[str, Any]) -> AttemptPlan:
    runner, evaluator = ctx.inputs(spec.task_id)
    if spec.oracle_override is not None:
        evaluator = dataclasses.replace(evaluator, approval_oracle=spec.oracle_override)
    policy = (c_program(ctx, spec.task_id, variant=spec.variant) if spec.arm == "woa_product"
              else a_program(ctx, spec.task_id, variant=spec.variant))
    return AttemptPlan(slot=slot, runner_input=runner, evaluator_input=evaluator,
                       policy=policy, provider_faults=spec.faults, provider_models=spec.models,
                       inject=spec.inject)


def _slot_fields(slot: dict[str, Any]) -> dict[str, Any]:
    return {key: slot[key] for key in ("slot_id", "arm", "task_id", "replicate")}


def create_batch(ctx: Context, name: str, specs: list[SlotSpec]) -> Path:
    """A new qualification ledger whose schedule is exactly ``specs``; nothing runs yet."""
    ledgers = ctx.root / "ledgers"
    ledgers.mkdir(mode=0o700, exist_ok=True)
    path = ledgers / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    slots = [{"slot_id": f"q{index + 1:03d}", "arm": spec.arm, "task_id": spec.task_id,
              "replicate": "primary"} for index, spec in enumerate(specs)]
    ledger.create(path, {
        "batch_id": name, "protocol_id": protocol.PROTOCOL_ID,
        "protocol_digest": protocol.document()["protocol_digest"],
        "schedule_digest": ledger.schedule_digest(slots), "purpose": "apparatus_qualification",
        "provider_config_digest": protocol.provider_config_digest(), "slots": slots,
        "previous_batch": None, "schedule_seed": None,
        "provider_binding_digest": provider_binding_digest(ScriptedBackend().identity(),
                                                           protocol_budgets()),
        **batch_bindings(ctx)})
    return path


def open_driver(ctx: Context, path: Path, playwright: Any, **options: Any):
    """A driver bound to an existing qualification ledger; its gateway answers from scripts."""
    return harness.open_driver(ctx, path, playwright, backend=ScriptedBackend(), **options)


def batch_record(path: Path, summary: dict[str, Any], specs: list[SlotSpec]) -> dict[str, Any]:
    return {"ledger": str(path.relative_to(REPOSITORY)), "summary": summary,
            "expectations": [dataclasses.asdict(spec) | {"inject": sorted(spec.inject)}
                             for spec in specs]}


def run_batch(ctx: Context, name: str, specs: list[SlotSpec], playwright: Any, *,
              budgets: GatewayBudgets | None = None, stack: Stack | None = None,
              recovery_dir: Path | None = None,
              between: Callable[[ProviderGateway, int], None] | None = None) -> dict[str, Any]:
    path = create_batch(ctx, name, specs)
    with open_driver(ctx, path, playwright, budgets=budgets, stack=stack,
                     recovery_dir=recovery_dir) as driver:
        slots = ledger.read(path)[1]["slots"]
        for index, (spec, slot) in enumerate(zip(specs, slots)):
            if between is not None and index:
                between(driver.gateway, index)
            # A replacement gets its own fresh script and never inherits the injected fault.
            clean = dataclasses.replace(spec, faults=None, models=None, inject=frozenset())
            summary = driver.run_slot(_plan(ctx, spec, _slot_fields(slot)),
                                      replacement=_plan(ctx, clean, _slot_fields(slot)))
            if summary["status"] != "OPEN":
                break
    return batch_record(path, finish_batch(path), specs)


def selected_outcomes(batch: dict[str, Any]) -> list[dict[str, Any]]:
    summary = batch["summary"]
    by_id = {item["attempt_id"]: item for item in summary["attempts"]}
    rows = []
    for slot, spec in zip(summary["slots"], batch["expectations"]):
        attempts = [by_id[item] for item in slot["attempts"]]
        rows.append({"slot_id": slot["slot_id"], "task_id": slot["task_id"], "arm": slot["arm"],
                     "expect": spec["expect"], "attempts": [
                         {key: item[key] for key in ("attempt_id", "product_outcome",
                                                     "apparatus_integrity", "apparatus_code",
                                                     "unclassified", "reset_outcome",
                                                     "replacement_of", "replacement_eligible",
                                                     "safety_events", "extraction_ambiguous")}
                         for item in attempts]})
    return rows


INTERRUPTED_SPECS = [SlotSpec("d01", "woa_product", "INTERRUPTED_THEN_REPLACED"),
                     SlotSpec("d02", "woa_product", "PASS")]


def _child_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": os.pathsep.join(
        str(REPOSITORY / item) for item in ("src", "examples/odoo/src", "examples/twenty/src",
                                            "."))}


def child_attempt(ctx: Context, path: Path, index: int) -> int:
    """Start one scheduled attempt in this process and die once its product has started."""
    from playwright.sync_api import sync_playwright

    slots = ledger.read(path)[1]["slots"]
    spec = dataclasses.replace(INTERRUPTED_SPECS[index],
                               inject=frozenset({"kill_after_product_start"}))
    with sync_playwright() as playwright, open_driver(ctx, path, playwright) as driver:
        driver.run_attempt(_plan(ctx, spec, _slot_fields(slots[index])))
    return 0


def interrupted_probe(ctx: Context, playwright: Any) -> dict[str, Any]:
    """A driver process killed mid-attempt is recovered, replaced once, and the batch goes on."""
    path = create_batch(ctx, "p74a-interrupted-attempt", INTERRUPTED_SPECS)
    child = subprocess.run([sys.executable, "-m", "evals.odoo.qualify", "child-attempt",
                            str(path), "0"], cwd=REPOSITORY, env=_child_env(),
                           capture_output=True, timeout=900)
    # The dead driver's Agent may still be mid-turn. Cutting its provider path keeps a late
    # request from reaching the next gateway, while the Agent stays up for its audit.
    ctx.stack.compose("stop", "provider-relay", timeout=120)
    with open_driver(ctx, path, playwright) as driver:
        recovered = driver.recover_open_attempt()
        slots = ledger.read(path)[1]["slots"]
        interrupted = recovered["attempts"][-1] if recovered else None
        if interrupted is not None and interrupted["replacement_eligible"]:
            plan = _plan(ctx, INTERRUPTED_SPECS[0], _slot_fields(slots[0]))
            plan.replacement_of = interrupted["attempt_id"]
            driver.run_attempt(plan)
        if not ledger.read(path)[1]["pending_seal"]:
            driver.run_slot(_plan(ctx, INTERRUPTED_SPECS[1], _slot_fields(slots[1])))
    record = batch_record(path, finish_batch(path), INTERRUPTED_SPECS)
    record["child_exit_status"] = child.returncode
    return record


# -- reset rejection probes ----------------------------------------------------------------------

def _restore(ctx: Context, directory: Path, *, stack: Stack | None = None,
             rearm: bool = False) -> dict[str, Any]:
    stack = stack or ctx.stack
    return fixture.restore(stack, fixture.FixtureGuard(stack), directory,
                           actor_projection=lambda: actor_projection(stack.rpc_url, ctx.keys),
                           rearm_after_failure=rearm)


def reset_probes(ctx: Context) -> dict[str, Any]:
    real = PRIVATE / "recovery"
    probes = ctx.root / "reset-probes"
    shutil.rmtree(probes, ignore_errors=True)
    results: dict[str, Any] = {}

    def copy(name: str) -> Path:
        target = probes / name
        shutil.copytree(real, target)
        return target

    wrong = copy("wrong-archive")
    data = bytearray((wrong / "baseline.pgdump").read_bytes())
    data[-1] ^= 0xFF
    (wrong / "baseline.pgdump").write_bytes(bytes(data))
    results["wrong_archive"] = _restore(ctx, wrong)
    results["wrong_project"] = _restore(
        ctx, copy("wrong-project"), stack=dataclasses.replace(ctx.stack,
                                                              project="woa-p74-not-this-project"))

    # Missing object: an archive that restores cleanly but lacks one customer.
    missing = copy("missing-object")
    victim = next(item for item in ctx.population["customers"]
                  if item["key"] not in ctx.population["roles"].values()
                  and item["company"] == "main" and item["customer_rank"] > 0)
    partner_id = ctx.seed_ids["partners"][victim["key"]]
    ctx.stack.psql(f"delete from res_partner where id = {int(partner_id)}")
    archive = ctx.stack.run(["docker", "exec", ctx.stack.container("db"), "pg_dump", "-U",
                             "odoo", "-d", "woa_trial", "--format=custom", "--no-owner",
                             "--no-privileges"], timeout=900).stdout
    (missing / "baseline.pgdump").write_bytes(archive)
    record = json.loads((missing / "recovery.json").read_text())
    record["database_archive"] = {"sha256": "sha256:" + hashlib.sha256(archive).hexdigest(),
                                  "bytes": len(archive)}
    record = fixture._seal(record)
    (missing / "recovery.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    results["missing_object"] = _restore(ctx, missing)

    # Half restore: the database comes back, the attachment objects do not.
    half = copy("half-restore")
    empty = ctx.stack.compose("run", "--rm", "--no-deps", "--entrypoint", "sh", "odoo", "-c",
                              "mkdir -p /tmp/empty/woa_trial && tar -C /tmp/empty -cf - "
                              "woa_trial").stdout
    (half / "baseline-filestore.tar").write_bytes(empty)
    record = json.loads((half / "recovery.json").read_text())
    record["filestore_archive"] = {"sha256": "sha256:" + hashlib.sha256(empty).hexdigest(),
                                   "bytes": len(empty)}
    record = fixture._seal(record)
    (half / "recovery.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    results["half_restore"] = _restore(ctx, half)

    # Positive control: a real write, put back by the real baseline.
    writer = OdooActorClient(base_url=ctx.stack.rpc_url, api_key_file=ctx.keys["writer"])
    writer.execute("res.partner", "create", [{"name": "P74 Reset Probe Customer",
                                              "customer_rank": 1}])
    results["positive_reset"] = _restore(ctx, real, rearm=True)
    results["repeat_reset"] = _restore(ctx, real)
    expected = {"wrong_archive": "FAIL", "wrong_project": "FAIL", "missing_object": "FAIL",
                "half_restore": "FAIL", "positive_reset": "PASS", "repeat_reset": "PASS"}
    return {"results": {key: {"status": value["status"], "basis": value.get("basis", [])}
                        for key, value in results.items()},
            "as_expected": all(results[key]["status"] == value for key, value in expected.items())}


# -- mutation probe ------------------------------------------------------------------------------

def mutation_stack(ctx: Context) -> Stack:
    source = ctx.stack.compose("run", "--rm", "--no-deps", "--entrypoint", "cat", "agent",
                               ADAPTER_IN_IMAGE).stdout.decode("utf-8")
    if source.count(QUOTATION_DOMAIN) != 1:
        raise RuntimeError("the adapter no longer contains the mutated quotation domain")
    directory = ctx.root / "mutation"
    directory.mkdir(mode=0o700, exist_ok=True)
    mutated = directory / "adapter.py"
    mutated.write_text(source.replace(QUOTATION_DOMAIN, MUTATED_QUOTATION_DOMAIN))
    os.chmod(mutated, 0o644)
    overlay = directory / "compose.mutation.yml"
    overlay.write_text("services:\n  agent:\n    volumes:\n"
                       f"      - {mutated}:{ADAPTER_IN_IMAGE}:ro\n")
    return dataclasses.replace(ctx.stack, extra_compose=(overlay,))


# -- formal closure probes -------------------------------------------------------------------------

CLOSURE_CHECKS = ("gateway_contract_fails_closed", "upstream_path_accounts_every_call",
                  "provider_binding_follows_endpoint_and_key",
                  "headers_bind_the_verified_candidate", "forged_bindings_refused",
                  "reports_gate_only_on_the_frozen_plan", "development_chain_rule_holds",
                  "freezes_refuse_unreviewed_or_arbitrary_records", "escrow_verifies_and_restores",
                  "custody_refuses_a_single_failure_domain",
                  "formal_entry_refuses_without_prerequisites", "spend_ledgers_untouched")


class LoopbackProvider:
    """An OpenAI-compatible responder on loopback, so the upstream path runs without a model."""

    def __init__(self, script: list[tuple[int, dict[str, Any], float]]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length", "0"))
                provider.requests.append({
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": json.loads(self.rfile.read(length))})
                status, payload, delay = provider.script.pop(0)
                time.sleep(delay)
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def spend_ledger_fingerprints() -> dict[str, str | None]:
    """The project's two spend ledgers, so a run can show it wrote to neither."""
    from web_operation_agent.model import DEFAULT_LEDGER

    ledgers = {"default": Path(os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER),
               "acceptance": REPOSITORY / ".artifacts" / "acceptance" / "spend.jsonl"}
    return {name: ("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                   if path.exists() else None) for name, path in ledgers.items()}


def _refused(action: Callable[[], Any], *errors: type[BaseException]) -> bool:
    try:
        action()
    except errors:
        return True
    return False


def _contract_probe() -> dict[str, Any]:
    budgets = protocol_budgets()
    contract = canary.canary_request(budgets)
    option = sorted(budgets.model_options)[0]
    variants = {
        "zero_output_cap": {**contract, "max_tokens": 0},
        "negative_output_cap": {**contract, "max_tokens": -1},
        "output_cap_above_budget": {**contract, "max_tokens": budgets.max_output_tokens + 1},
        "unfrozen_top_p": {**contract, "top_p": 0.9},
        "unfrozen_seed": {**contract, "seed": 7},
        "changed_temperature": {**contract, "temperature": 0.7},
        "changed_model_option": {**contract, option: "changed"},
        "missing_model_option": {key: value for key, value in contract.items() if key != option},
    }
    refusals = {name: budgets.request_refusal(body) for name, body in variants.items()}
    mapped = GatewayCompletionClient(
        base_url="http://127.0.0.1:9/v1", token=secrets.token_urlsafe(32),
        model=budgets.requested_model, model_options=budgets.model_options).forwarded_body({
            "model": budgets.requested_model, "messages": contract["messages"],
            "temperature": budgets.temperature, "top_p": 1.0,
            "max_completion_tokens": budgets.max_output_tokens, "enable_thinking": False})
    mapped_accepted = budgets.request_refusal(mapped) is None
    return {"refusals": refusals, "pageagent_mapping_accepted": mapped_accepted,
            "fails_closed": (budgets.request_refusal(contract) is None
                             and all(refusals.values()) and mapped_accepted
                             and not set(mapped) & set(PAGEAGENT_ENGINE_FIELDS))}


def _upstream_probe(probe: Path) -> dict[str, Any]:
    budgets = protocol_budgets()
    contract = canary.canary_request(budgets)
    key = probe / "evaluation.key"
    key.write_text(secrets.token_urlsafe(32) + "\n")
    os.chmod(key, 0o600)
    answer = {"id": "loopback", "object": "chat.completion", "model": budgets.requested_model,
              "choices": [{"index": 0, "finish_reason": "stop", "message": {
                  "role": "assistant", "content": str(canary.CANARY_ANSWER)}}],
              "usage": {"prompt_tokens": 20, "completion_tokens": 2}}
    provider = LoopbackProvider([(200, answer, 0.0), (503, {"error": {"code": "busy"}}, 0.0),
                                 (200, answer, 3.0)])
    try:
        backend = UpstreamBackend(base_url=provider.base_url, key_file=key,
                                  requested_model=budgets.requested_model,
                                  spend_ledger=probe / "spend.jsonl",
                                  batch_id="p74a-closure-probe", timeout_s=1.0)
        calls = [backend.complete(dict(contract), attempt_id="p74a-closure-probe",
                                  call_index=index) for index in (1, 2, 3)]
    finally:
        provider.close()
    rows = [json.loads(line)["accounting_status"]
            for line in (probe / "spend.jsonl").read_text().splitlines()]
    results = [[call.status, call.error_code] for call in calls]
    request_id_sent = (provider.requests[0]["headers"].get("x-woa-request-id")
                       == calls[0].provider_request_id)
    return {"results": results, "request_id_sent": request_id_sent, "spend_rows": rows,
            "accounts_every_call": (
                results == [["ok", None], ["http_error", "provider_unavailable"],
                            ["transport_error", "provider_deadline"]]
                and request_id_sent
                and rows == ["reported", "usage_unavailable", "usage_unavailable"])}


def _provider_identity_probe(probe: Path) -> dict[str, Any]:
    """The runtime binding follows the endpoint and key; no provider is contacted."""
    budgets = protocol_budgets()
    keys = {}
    for name in ("a", "b"):
        keys[name] = probe / f"identity-key-{name}"
        keys[name].write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(keys[name], 0o600)

    def binding(url: str, key: Path) -> str:
        backend = UpstreamBackend(base_url=url, key_file=key,
                                  requested_model=budgets.requested_model,
                                  spend_ledger=probe / "identity-spend.jsonl",
                                  batch_id="p74a-identity-probe")
        return provider_binding_digest(backend.identity(), budgets)

    base = binding("http://127.0.0.1:18001/v1", keys["a"])
    record = {
        "normalized_endpoint_kept": base == binding("HTTP://127.0.0.1:18001/v1/", keys["a"]),
        "endpoint_change_detected": base != binding("http://127.0.0.1:18002/v1", keys["a"]),
        "key_change_detected": base != binding("http://127.0.0.1:18001/v1", keys["b"]),
        "nothing_sent": not (probe / "identity-spend.jsonl").exists(),
    }
    return {**record, "follows": all(record.values())}


def _chain_rule_probe(probe: Path, sample: dict[str, Any]) -> dict[str, Any]:
    """The development and held-out chain rules, on headers derived from a real one."""
    development = {**sample, "purpose": "development", "schedule_seed": "0" * 32}
    completed = {"batch_id": "p74a-chain-probe-000", "purpose": "development",
                 "protocol_digest": sample["protocol_digest"],
                 "candidate_commit": sample["candidate"]["commit"],
                 "chain_head": "sha256:" + "0" * 64, "termination_reason": "batch_completed"}
    other = {**sample["candidate"], "commit": "0" * 40, "agent_image_revision": "0" * 40}

    def allowed(name: str, header: dict[str, Any]) -> bool:
        return not _refused(lambda: ledger.create(probe / f"chain-{name}.jsonl", {
            **header, "batch_id": f"p74a-chain-{name}"}), ledger.LedgerError)

    record = {
        "new_candidate_follows_a_completed_batch": allowed(
            "new-candidate", {**development, "previous_batch": completed, "candidate": other}),
        "same_candidate_refused_after_completion": not allowed(
            "same-candidate", {**development, "previous_batch": completed}),
        "same_candidate_follows_an_exogenous_stop": allowed("exogenous", {
            **development, "previous_batch": {**completed,
                                              "termination_reason": "model_identity_changed"}}),
        "heldout_refused_after_completion_under_the_same_protocol": not allowed("heldout", {
            **development, "purpose": "heldout", "candidate": other,
            "previous_batch": {**completed, "purpose": "heldout"}}),
    }
    return {**record, "holds": all(record.values())}


def _custody_probe() -> dict[str, Any]:
    """Two directories on this machine are one failure domain, and the entry says so."""
    paths = formal.FormalPaths()
    try:
        pointer = json.loads(paths.escrow_record.read_text())
        shared = (escrow.failure_domain(pointer["archive"])["identifier"]
                  == escrow.failure_domain(pointer["secret"])["identifier"]
                  == escrow.failure_domain(paths.private_dir)["identifier"])
    except (escrow.EscrowError, OSError, ValueError, KeyError) as exc:
        return {"error": str(exc), "refuses": False}
    refused = _refused(lambda: escrow.custody_record(
        private_dir=paths.private_dir, archive=pointer["archive"], secret=pointer["secret"]),
        escrow.EscrowError)
    entry = formal.status(paths)["checks"]["escrow_custody"]
    return {"local_copies_share_the_original_domain": shared, "local_custody_refused": refused,
            "entry_custody_check": entry,
            "refuses": shared and refused and entry.startswith("refused")}


def _binding_probe(probe: Path, sample: dict[str, Any],
                   candidate: dict[str, Any]) -> dict[str, Any]:
    path = probe / "binding-probe.jsonl"
    ledger.create(path, {**sample, "batch_id": "p74a-binding-probe"})
    slot = sample["slots"][0]
    genuine = {"attempt_id": "attempt-001", "slot_id": slot["slot_id"], "arm": slot["arm"],
               "task_id": slot["task_id"], "replicate": slot["replicate"],
               "run_id": "run-binding-probe", "fixture_digest": sample["fixture"]["manifest_digest"],
               "capability_digest": sample["capability_digests"][slot["arm"]],
               "provider_config_digest": sample["provider_config_digest"],
               "provider_binding_digest": sample["provider_binding_digest"], "replacement_of": None}
    forged = "sha256:" + hashlib.sha256(b"a well-formed digest of nothing").hexdigest()
    refused = {field: _refused(lambda field=field: ledger.append(
        path, "attempt_started", {**genuine, field: forged}), ledger.LedgerError)
        for field in ("fixture_digest", "capability_digest", "provider_config_digest",
                      "provider_binding_digest")}
    refused["mislabelled_image"] = _refused(lambda: ledger.create(
        probe / "mislabelled-image.jsonl",
        {**sample, "batch_id": "p74a-mislabelled-image",
         "candidate": {**candidate, "agent_image_revision": "0" * 40}}), ledger.LedgerError)
    accepted = ledger.append(path, "attempt_started", genuine)["open_attempt"] == "attempt-001"
    return {**refused, "genuine_accepted": accepted,
            "refused": all(refused.values()) and accepted}


def _report_probe(batches: dict[str, Any], sample: dict[str, Any],
                  metadata: dict[str, Any]) -> dict[str, Any]:
    summary = batches["samples"]["summary"]
    lookalike = {"purpose": "development", "protocol_digest": sample["protocol_digest"],
                 "task_set_seal_digest": sample["task_set_seal_digest"],
                 "fixture_manifest_digest": sample["fixture"]["manifest_digest"],
                 "capability_digests": sample["capability_digests"],
                 "candidate_commit": sample["candidate"]["commit"],
                 "provider_config_digest": sample["provider_config_digest"],
                 "provider_binding_digest": sample["provider_binding_digest"],
                 "schedule_digest": sample["schedule_digest"]}
    unplanned = report.build_report(batches=[summary], tasks=metadata)["batches"][0]
    planned = report.build_report(batches=[summary], tasks=metadata, plan=lookalike)["batches"][0]
    unlinked = _refused(lambda: report.build_report(
        batches=[batches["negatives"]["summary"], summary], tasks=metadata), ValueError)
    return {"without_plan": unplanned["verdict_basis"],
            "with_a_plan_copied_from_the_batch": planned["verdict_basis"],
            "unlinked_chain_refused": unlinked,
            "gates_only_on_the_frozen_plan": (
                unplanned["verdict_basis"] == ["no_frozen_plan"]
                and planned["verdict_status"] == "descriptive_only"
                and "not_the_fixed_thirty_task_schedule" in planned["verdict_basis"]
                and unlinked)}


def _freeze_probe(started: str) -> dict[str, Any]:
    development = json.loads(harness.DEVELOPMENT_SEAL.read_text())
    heldout = json.loads(harness.HELDOUT_SEAL.read_text())
    split = json.loads(harness.SPLIT_VERIFICATION.read_text())
    revise = freeze.family_review_record(
        reviewer="P7.4a qualification probe", reviewed_at=started, verdict="REVISE",
        development_seal=development, heldout_seal=heldout,
        findings=["probe: a REVISE verdict must not open the first freeze"])
    record = {
        "stage": protocol.document()["stage"],
        "arbitrary_first_freeze_refused": _refused(
            lambda: protocol.document(first_freeze={"kind": "p74_first_freeze"}),
            freeze.FreezeError),
        "revised_review_refused": _refused(
            lambda: freeze.first_freeze_record(development_seal=development, heldout_seal=heldout,
                                               split_verification=split, family_review=revise),
            freeze.FreezeError),
        "second_without_first_refused": _refused(
            lambda: protocol.document(second_freeze={"kind": "p74_second_freeze"}),
            freeze.FreezeError),
    }
    record["refuses"] = record["stage"] == "draft" and all(
        value for key, value in record.items() if key != "stage")
    return record


def _escrow_probe(probe: Path) -> dict[str, Any]:
    paths = formal.FormalPaths()
    try:
        pointer = json.loads(paths.escrow_record.read_text())
        verification = escrow.verify(pointer["archive"], pointer["secret"],
                                     private_dir=paths.private_dir)
        restored = escrow.restore(pointer["archive"], pointer["secret"], probe / "escrow-restore")
    except (escrow.EscrowError, OSError, ValueError, KeyError) as exc:
        return {"status": "FAILED", "error": str(exc), "verifies_and_restores": False}
    finally:
        shutil.rmtree(probe / "escrow-restore", ignore_errors=True)
    return {"status": verification["status"], "files": verification["files"],
            "matches_private_files": verification["matches_private_files"],
            "restored_copy_matches": restored["matches_private_files"],
            "verifies_and_restores": (verification["matches_private_files"] is True
                                      and restored["matches_private_files"] is True)}


def _formal_entry_probe() -> dict[str, Any]:
    paths = formal.FormalPaths()
    state = formal.status(paths)
    unconfirmed = _refused(lambda: formal.paid_call_prerequisites(paths, confirm_paid_calls=False),
                           formal.FormalError)
    return {"checks": state["checks"], "unconfirmed_refused": unconfirmed,
            "refuses": (unconfirmed and state["development_can_start"] is False
                        and state["checks"]["first_freeze"].startswith("refused"))}


def closure_checks(ctx: Context, batches: dict[str, Any], *, candidate: dict[str, Any],
                   metadata: dict[str, Any], started: str) -> dict[str, Any]:
    """The formal-closure repairs, checked on this run's real headers and without any model."""
    probe = ctx.root / "closure-probes" / started.replace("-", "").replace(":", "")
    probe.mkdir(mode=0o700, parents=True)
    headers = {name: ledger.read(REPOSITORY / batch["ledger"])[0][0]["body"]
               for name, batch in batches.items()}
    manifest_record = json.loads(fixture_manifest_path(ctx.private_dir).read_text())
    contract = _contract_probe()
    upstream = _upstream_probe(probe)
    identity = _provider_identity_probe(probe)
    bindings = _binding_probe(probe, headers["samples"], candidate)
    gating = _report_probe(batches, headers["samples"], metadata)
    chain = _chain_rule_probe(probe, headers["samples"])
    freezes = _freeze_probe(started)
    held = _escrow_probe(probe)
    custody = _custody_probe()
    entry = _formal_entry_probe()
    return {
        "gateway_contract": contract, "gateway_contract_fails_closed": contract["fails_closed"],
        "upstream_path": upstream,
        "upstream_path_accounts_every_call": upstream["accounts_every_call"],
        "provider_binding": identity,
        "provider_binding_follows_endpoint_and_key": identity["follows"],
        "development_chain": chain, "development_chain_rule_holds": chain["holds"],
        "custody": custody, "custody_refuses_a_single_failure_domain": custody["refuses"],
        "headers_bind_the_verified_candidate": (
            candidate["agent_image_revision"] == candidate["commit"]
            and manifest_record["candidate"] == candidate
            and all(item["candidate"] == candidate
                    and item["fixture"]["manifest_digest"] == manifest_record["manifest_digest"]
                    for item in headers.values())),
        "forged_bindings": bindings, "forged_bindings_refused": bindings["refused"],
        "report_gating": gating,
        "reports_gate_only_on_the_frozen_plan": gating["gates_only_on_the_frozen_plan"],
        "freezes": freezes, "freezes_refuse_unreviewed_or_arbitrary_records": freezes["refuses"],
        "escrow": held, "escrow_verifies_and_restores": held["verifies_and_restores"],
        "formal_entry": entry, "formal_entry_refuses_without_prerequisites": entry["refuses"],
    }


# -- the whole qualification -----------------------------------------------------------------------

def offline_checks(ctx: Context) -> dict[str, Any]:
    heldout = json.loads((PRIVATE / "heldout" / "p74-odoo-heldout-v1.json").read_text())
    heldout_population = population_module.generate(
        (PRIVATE / "seeds" / "heldout.seed").read_text().strip())
    tasksets = REPOSITORY / "evals/odoo/tasksets"
    development_seal = json.loads((tasksets / "p74-odoo-development-v1.seal.json").read_text())
    heldout_seal = json.loads((tasksets / "p74-odoo-heldout-v1.seal.json").read_text())
    tasks.verify_seal(ctx.task_set, development_seal, population=ctx.population)
    tasks.verify_seal(heldout, heldout_seal, population=heldout_population)
    split = tasks.verify_split_separation(development_seal, heldout_seal)
    committed_split = json.loads((tasksets / "p74-odoo-split-v1.json").read_text())
    ruling = equivalence.rule()
    leaks = leakcheck.scan(population_module.record_names(ctx.population)
                           | population_module.record_names(heldout_population))
    backend = ScriptedBackend()
    backend.install("canary", lambda _body, _index: {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": str(canary.CANARY_ANSWER)}, "finish_reason": "stop"}]})
    backend.install("canary-drift", lambda _body, _index: {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": str(canary.CANARY_ANSWER)}, "finish_reason": "stop"}]},
        model_overrides={3: "p74-scripted/v2"})
    counters = {"canary": 0, "canary-drift": 0}

    def sender(attempt: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def send(request: dict[str, Any]) -> dict[str, Any]:
            counters[attempt] += 1
            return backend.complete(request, attempt_id=attempt,
                                    call_index=counters[attempt]).payload
        return send

    stable = canary.run_preflight(sender("canary"), budgets=protocol_budgets())
    drift = canary.run_preflight(sender("canary-drift"), budgets=protocol_budgets())
    stability = json.loads((ctx.root / "stability-proof.json").read_text())
    return {
        "bc_ruling": {"verdict": ruling["verdict"], "reasons": ruling["reasons"]},
        "task_seals_verified": True,
        "split_matches_committed": split == committed_split,
        "leak_check": {key: leaks[key] for key in ("status", "scanned_files",
                                                   "optional_roots_present")},
        "canary_scripted": {"stable": stable["status"], "drift": drift["status"]},
        "stability_proof": {key: stability[key] for key in ("status", "interval_s",
                                                            "required_interval_s", "changed")},
        "protocol_digest": protocol.document()["protocol_digest"],
    }


def run(ctx: Context) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    spend_before = spend_ledger_fingerprints()
    candidate = ctx.stack.verify_candidate()
    result: dict[str, Any] = {
        "artifact_version": 1, "kind": "p74a_apparatus_qualification", "started_at": started,
        "external_model_calls": 0, "candidate": candidate,
        "fixture_manifest_digest": json.loads(fixture_manifest_path(ctx.private_dir).read_text())[
            "manifest_digest"]}
    result["offline"] = offline_checks(ctx)
    result["reset_probes"] = reset_probes(ctx)
    W, A = "woa_product", "pageagent_browser_only"
    batches: dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        result["browser_version"] = browser.version
        browser.close()
        batches["samples"] = run_batch(ctx, "p74a-samples", [
            SlotSpec("d01", W, "PASS"), SlotSpec("d01", A, "PASS"), SlotSpec("d02", W, "PASS"),
            SlotSpec("d03", A, "PASS"), SlotSpec("d06", A, "PASS"), SlotSpec("d07", W, "PASS"),
            SlotSpec("d08", W, "PASS"), SlotSpec("d11", W, "PASS"), SlotSpec("d13", W, "PASS"),
            SlotSpec("d16", W, "PASS"), SlotSpec("d18", W, "PASS"), SlotSpec("d20", W, "PASS"),
            SlotSpec("d21", W, "PASS"), SlotSpec("d22", W, "PASS"), SlotSpec("d24", W, "PASS"),
            SlotSpec("d26", W, "PASS"), SlotSpec("d30", W, "PASS")], playwright)
        batches["negatives"] = run_batch(ctx, "p74a-negatives", [
            SlotSpec("d01", W, "FAIL", variant="missing_field"),
            SlotSpec("d01", A, "FAIL", variant="wrong_email"),
            SlotSpec("d08", W, "FAIL", variant="no_currency_filter")], playwright)
        batches["seal_side_effect"] = run_batch(ctx, "p74a-seal-extra-side-effect", [
            SlotSpec("d01", W, "SEAL:critical_safety_event",
                     inject=frozenset({"extra_side_effect"})),
            SlotSpec("d02", W, "NOT_RUN")], playwright)
        batches["interrupted"] = interrupted_probe(ctx, playwright)
        batches["faults_provider"] = run_batch(ctx, "p74a-faults-provider", [
            SlotSpec("d01", W, "REPLACED_PASS", faults={1: "http_503"}),
            SlotSpec("d02", W, "CAP_SEAL", faults={2: "connection_lost"})], playwright)
        batches["faults_harness"] = run_batch(ctx, "p74a-faults-harness", [
            SlotSpec("d01", W, "REPLACED_PASS", inject=frozenset({"evaluator_exit"})),
            SlotSpec("d07", W, "CAP_SEAL", inject=frozenset({"browser_crash"}))], playwright)
        batches["faults_prestart"] = run_batch(ctx, "p74a-faults-prestart", [
            SlotSpec("d01", W, "REPLACED_PASS", inject=frozenset({"baseline_drift"})),
            # Replacement caps are per arm, so the cap probe stays on the same arm.
            SlotSpec("d02", W, "CAP_SEAL", inject=frozenset({"identity_before_start"}))],
            playwright)
        batches["seal_model"] = run_batch(ctx, "p74a-seal-model-string", [
            SlotSpec("d01", W, "SEAL:model_identity_changed", models={2: "p74-scripted/v2"}),
            SlotSpec("d02", W, "NOT_RUN")], playwright)

        def stray(gateway: ProviderGateway, _index: int) -> None:
            request = urllib.request.Request(
                gateway.base_url + "/chat/completions", method="POST",
                data=json.dumps({"model": "qwen3.8-flash", "messages": []}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + secrets.token_urlsafe(32)})
            try:
                urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError:
                pass

        batches["seal_unowned"] = run_batch(ctx, "p74a-seal-unowned-request", [
            SlotSpec("d01", W, "PASS"), SlotSpec("d02", W, "SEAL:unowned_model_request")],
            playwright, between=stray)
        _, d16 = ctx.inputs("d16")
        wrong_oracle = {**d16.approval_oracle,
                        "targets": [d16.approval_oracle["targets"][0], "邮箱：wrong@example.test"]}
        batches["seal_safety"] = run_batch(ctx, "p74a-seal-safety", [
            SlotSpec("d16", W, "SEAL:critical_safety_event", oracle_override=wrong_oracle),
            SlotSpec("d01", W, "NOT_RUN")], playwright)
        batches["budget"] = run_batch(ctx, "p74a-budget-refusal", [
            SlotSpec("d01", W, "FAIL")], playwright,
            budgets=protocol_budgets(max_model_calls=2))
        broken = ctx.root / "reset-probes" / "reset-fail-recovery"
        shutil.rmtree(broken, ignore_errors=True)
        shutil.copytree(PRIVATE / "recovery", broken)
        (broken / "baseline-filestore.tar").write_bytes(b"not an archive")
        batches["seal_reset"] = run_batch(ctx, "p74a-seal-reset-failure", [
            SlotSpec("d16", W, "SEAL:reset_failed"), SlotSpec("d01", W, "NOT_RUN")], playwright,
            recovery_dir=broken)
        result["post_reset_failure_restore"] = _restore(ctx, PRIVATE / "recovery", rearm=True)[
            "status"]
        batches["domain_mutation"] = run_batch(ctx, "p74a-domain-mutation", [
            SlotSpec("d08", W, "FAIL")], playwright, stack=mutation_stack(ctx))
    result["batches"] = {name: {"ledger": batch["ledger"], "status": batch["summary"]["status"],
                                "seal_reason": batch["summary"]["seal_reason"],
                                "slots": selected_outcomes(batch)}
                         for name, batch in batches.items()}
    metadata = {task["task_id"]: {"class": task["class"], "stratum": task["stratum"],
                                  "read_only": task["max_mutations"] == 0}
                for task in ctx.task_set["tasks"]}
    # Every qualification batch starts its own chain, so each gets its own report.
    for batch in batches.values():
        report.verify_report(report.build_report(batches=[batch["summary"]], tasks=metadata))
    result["report_verified"] = True
    result["closure"] = closure_checks(ctx, batches, candidate=candidate, metadata=metadata,
                                       started=started)
    result["closure"]["spend_ledgers_untouched"] = spend_ledger_fingerprints() == spend_before
    result["checks"] = evaluate_checks(result, batches)
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["status"] = "READY" if all(result["checks"].values()) else "FAILED"
    # Every run keeps its own artifact; an earlier run's record is never overwritten.
    target = ctx.root / f"p74a-qualification-{started.replace('-', '').replace(':', '')}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True,
                                default=str) + "\n")
    return {**result, "artifact": str(target.relative_to(REPOSITORY))}


def _selected(row: dict[str, Any]) -> dict[str, Any] | None:
    return row["attempts"][-1] if row["attempts"] else None


def evaluate_checks(result: dict[str, Any], batches: dict[str, Any]) -> dict[str, bool]:
    view = result["batches"]

    def rows(name: str) -> list[dict[str, Any]]:
        return view[name]["slots"]

    def outcome_is(row: dict[str, Any], expected: str) -> bool:
        attempt = _selected(row)
        return bool(attempt) and attempt["product_outcome"] == expected and attempt[
            "apparatus_integrity"] == "VALID"

    samples = rows("samples")
    strata = {task["task_id"]: task["stratum"] for task in
              json.loads((REPOSITORY / "evals/odoo/tasksets/p74-odoo-development-v1.json")
                         .read_text())["tasks"]}
    checks = {
        "bc_ruling_excludes_b": result["offline"]["bc_ruling"]["verdict"] == "B_EXCLUDED",
        "split_and_seals": result["offline"]["split_matches_committed"],
        "leak_check": result["offline"]["leak_check"]["status"] == "READY",
        "canary_contract": result["offline"]["canary_scripted"] == {"stable": "READY",
                                                                    "drift": "FAILED"},
        "stability_proof": result["offline"]["stability_proof"]["status"] == "READY",
        "reset_rejections": result["reset_probes"]["as_expected"],
        "samples_batch_complete": view["samples"]["status"] == "COMPLETE",
        "every_sample_passes": all(outcome_is(row, "PASS") for row in samples),
        "all_six_strata_on_woa": {strata[row["task_id"]] for row in samples
                                  if row["arm"] == "woa_product"} == set(tasks.STRATA),
        "pageagent_real_chromium_samples": sum(row["arm"] == "pageagent_browser_only"
                                               for row in samples) >= 3,
        "negative_controls_fail": all(outcome_is(row, "FAIL") for row in rows("negatives")),
        "provider_fault_replaced_then_cap_sealed": _fault_batch(view["faults_provider"],
                                                                "provider_http_5xx"),
        "harness_fault_replaced_then_cap_sealed": _fault_batch(view["faults_harness"],
                                                               "evaluator_exception"),
        "prestart_fault_replaced_then_cap_sealed": _fault_batch(view["faults_prestart"],
                                                                "fixture_baseline_unstable"),
        "model_string_change_seals": view["seal_model"]["seal_reason"] == "model_identity_changed",
        "unowned_request_seals": view["seal_unowned"]["seal_reason"] == "unowned_model_request",
        "safety_event_seals": view["seal_safety"]["seal_reason"] == "critical_safety_event",
        "extra_side_effect_detected_and_sealed": (
            view["seal_side_effect"]["seal_reason"] == "critical_safety_event"
            and "extra_side_effect" in _selected(rows("seal_side_effect")[0])["safety_events"]),
        "interrupted_attempt_recovered_and_replaced": _interrupted_ok(view["interrupted"]),
        "reset_failure_seals_without_erasing_product_result": (
            view["seal_reset"]["seal_reason"] == "reset_failed"
            and _selected(rows("seal_reset")[0])["product_outcome"] in {"PASS", "FAIL"}),
        "baseline_restored_after_reset_failure": result["post_reset_failure_restore"] == "PASS",
        "budget_refusal_is_product_failure": outcome_is(rows("budget")[0], "FAIL"),
        "domain_mutation_fails_evaluator": outcome_is(rows("domain_mutation")[0], "FAIL"),
        "no_unclassified_results": not any(
            attempt["unclassified"] for batch in view.values() for row in batch["slots"]
            for attempt in row["attempts"]),
        "report_recomputes_and_verifies": result.get("report_verified", False),
        "scripted_model_only": all(
            batch["summary"]["provider_model"] in {None, SCRIPTED_MODEL, "p74-scripted/v2"}
            for batch in batches.values()),
        **{f"closure_{name}": result["closure"][name] is True for name in CLOSURE_CHECKS},
    }
    feedback = [attempt.get("metrics", {}).get("first_feedback_ms")
                for batch in batches.values() for attempt in batch["summary"]["attempts"]
                if attempt["arm"] == "woa_product" and attempt.get("metrics")]
    measured = [value for value in feedback if value is not None]
    checks["submit_feedback_within_300ms"] = bool(measured) and max(measured) <= 300
    return checks


def _interrupted_ok(batch: dict[str, Any]) -> bool:
    first = batch["slots"][0]["attempts"]
    return (batch["status"] == "COMPLETE" and len(first) == 2
            and first[0]["apparatus_code"] == "browser_or_control_channel_crash"
            and first[0]["replacement_eligible"] is True
            and first[1]["replacement_of"] == first[0]["attempt_id"]
            and first[1]["product_outcome"] == "PASS")


def _fault_batch(batch: dict[str, Any], code: str) -> bool:
    first, second = batch["slots"]
    original = first["attempts"][0] if first["attempts"] else {}
    replacement = first["attempts"][1] if len(first["attempts"]) > 1 else {}
    return (original.get("apparatus_code") == code
            and original.get("product_outcome") == "NOT_EVALUABLE"
            and original.get("replacement_eligible") is True
            and replacement.get("product_outcome") == "PASS"
            and replacement.get("apparatus_integrity") == "VALID"
            and batch["seal_reason"] == "replacement_cap_exceeded"
            and len(second["attempts"]) == 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.qualify")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    child = commands.add_parser("child-attempt")
    child.add_argument("ledger")
    child.add_argument("index", type=int)
    sample = commands.add_parser("sample")
    sample.add_argument("task_id")
    sample.add_argument("--arm", default="woa_product",
                        choices=["woa_product", "pageagent_browser_only"])
    sample.add_argument("--variant", default="")
    args = parser.parse_args(argv)
    ctx = load_context()
    if args.command == "child-attempt":
        return child_attempt(ctx, Path(args.ledger), args.index)
    if args.command == "run":
        result = run(ctx)
        print(json.dumps({"status": result["status"], "artifact": result["artifact"],
                          "checks": result["checks"]}, indent=1))
        return 0 if result["status"] == "READY" else 1
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        batch = run_batch(ctx, f"debug-{args.task_id}",
                          [SlotSpec(args.task_id, args.arm, "debug", variant=args.variant)],
                          playwright)
    print(json.dumps({"status": batch["summary"]["status"],
                      "slots": selected_outcomes(batch)}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
