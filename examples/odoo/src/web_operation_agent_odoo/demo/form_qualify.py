"""Model-free qualification for the experimental generic Odoo form surface.

This is deliberately separate from the ordinary live qualification.  It enables an
experimental adapter capability, visits real Odoo create forms in Chromium and exercises
the product's ``describe_form -> prepare_form -> prefill_form`` state machine.  It never
calls a provider and never clicks Save or any other application control.

The dynamic script sees only application labels and opaque IDs returned by the server.  DOM
selectors live in the hidden evaluator below; they are not an execution input or Agent
context.  The persisted artifact contains booleans, counts, timings and digests, never form
values, credentials or opaque IDs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_operation_agent.composition.binding import bind_application
from web_operation_agent.modeling.egress import ProviderEgressPolicy

from ..adapter import OdooApplicationAdapter
from .demo import OdooDemoConfig, ScriptedOdooChat, run_demo


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FormCase:
    name: str
    entry_path: str
    task: str
    values: tuple[tuple[str, str], ...]
    selectors: tuple[tuple[str, str], ...]
    model: str
    marker: tuple[str, str] | None = None


def form_cases(nonce: str) -> tuple[FormCase, ...]:
    """Frozen coverage for scalar text, number, date and native selection widgets."""
    customer = f"P86 Form Probe {nonce}"
    product = f"P86 Unsaved Product {nonce}"
    return (
        FormCase(
            name="customer_scalar_combobox",
            entry_path="/odoo/customers/new",
            task=(f"Fill Name with {customer} and Email with "
                  f"p86-{nonce}@example.test. Do not save or submit."),
            values=(("Name", customer), ("Email", f"p86-{nonce}@example.test")),
            selectors=(("Name", '[name="name"] input, [name="name"] textarea'),
                       ("Email", '[name="email"] input, [name="email"] textarea')),
            model="res.partner",
            marker=("name", customer),
        ),
        FormCase(
            name="product_text_number_choice",
            entry_path="/odoo/products/new",
            task=(f"Fill Name with {product}, Sales Price with 123.45, and Invoicing "
                  "Policy with the delivered-quantity option. Do not save or submit."),
            values=(("Name", product), ("Sales Price", "123.45"),
                    ("Invoicing Policy", "delivery")),
            selectors=(("Name", '[name="name"] input, [name="name"] textarea'),
                       ("Sales Price", '[name="list_price"] input'),
                       ("Invoicing Policy", '[name="invoice_policy"] select')),
            model="product.template",
            marker=("name", product),
        ),
        FormCase(
            name="quotation_date",
            entry_path="/odoo/sales/new",
            task="Fill Expiration with 2026-12-31. Do not save or submit.",
            values=(("Expiration", "2026-12-31"),),
            selectors=(("Expiration", '[name="validity_date"] input'),),
            model="sale.order",
        ),
    )


def _tool_payload(messages: list[dict], prefix: str) -> dict:
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, str) and content.startswith(prefix):
            # A tool may append an instruction after its compact JSON payload. Decode the
            # first value exactly instead of requiring the whole remainder to be JSON.
            value, _end = json.JSONDecoder().raw_decode(
                content.split("\n", 1)[1].lstrip())
            if isinstance(value, dict):
                return value
    raise RuntimeError(f"the product returned no {prefix.rstrip(':')} payload")


class FormQualificationChat(ScriptedOdooChat):
    """A no-provider script that can consume freshly minted opaque form bindings."""

    def __init__(self, case: FormCase, *, egress_policy: ProviderEgressPolicy):
        super().__init__((), egress_policy=egress_policy)
        self.case = case
        self.decision_trace: list[tuple[str, float]] = []
        self.described_fields: list[dict[str, Any]] = []
        self._phase = "start"
        self._waited_for_application = False

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        self.calls += 1
        self.tool_surfaces.append(sorted(
            tool["function"]["name"] for tool in (tools or [])))
        if self._phase == "start":
            name = "describe_form"
            arguments = {
                "field_query": " ".join(label for label, _value in self.case.values),
                "limit": 20,
            }
            self._phase = "descriptor"
        elif self._phase == "after_wait":
            name = "describe_form"
            arguments = {
                "field_query": " ".join(label for label, _value in self.case.values),
                "limit": 20,
            }
            self._phase = "descriptor"
        elif self._phase == "descriptor":
            described = _tool_payload(messages, "CURRENT FORM DESCRIPTOR")
            if described.get("form_available") is not True:
                if not self._waited_for_application:
                    self._waited_for_application = True
                    name, arguments = "wait_for", {"ms": 1_000}
                    self._phase = "after_wait"
                    self.decision_trace.append((name, time.perf_counter()))
                    return self._response(name, arguments)
                raise RuntimeError("the application did not expose the current form")
            fields = described.get("fields")
            if not isinstance(fields, list):
                raise RuntimeError("the current form descriptor has no fields")
            by_label = {field.get("label"): field for field in fields
                        if isinstance(field, dict)}
            missing = [label for label, _value in self.case.values
                       if label not in by_label]
            if missing:
                # Odoo commits the SPA route before its form component has rendered. The
                # first observation can therefore be a real, actor-bound empty fact. Use
                # the product's bounded wait action and require a fresh observation; never
                # retain or guess a field from the first one.
                if not self._waited_for_application:
                    self._waited_for_application = True
                    name, arguments = "wait_for", {"ms": 1_000}
                    self._phase = "after_wait"
                    self.decision_trace.append((name, time.perf_counter()))
                    return self._response(name, arguments)
                raise RuntimeError("the form omitted requested labels: " + ", ".join(missing))
            self.described_fields = [{
                "label": label,
                "type": by_label[label].get("type"),
                "control": by_label[label].get("control"),
            } for label, _value in self.case.values]
            name = "prepare_form"
            arguments = {
                "form_id": described["form_id"],
                "values": [{"field_id": by_label[label]["field_id"], "value": value}
                           for label, value in self.case.values],
            }
            self._phase = "prepared"
        elif self._phase == "prepared":
            _tool_payload(messages, "APPLICATION FORM DRAFT READY")
            name, arguments = "prefill_form", {}
            self._phase = "done"
        else:
            name, arguments = "finish", {
                "done": False, "why": "the form qualification script was exhausted"}
        self.decision_trace.append((name, time.perf_counter()))
        return self._response(name, arguments)

    def _response(self, name: str, arguments: dict) -> dict[str, Any]:
        return {"tool_calls": [{
            "id": f"form-qualification-{self.calls}",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}


def _page_probe(case: FormCase):
    expected = dict(case.values)

    def probe(page) -> dict:
        fields = {}
        for label, selector in case.selectors:
            locator = page.locator(selector)
            visible = [locator.nth(index) for index in range(locator.count())
                       if locator.nth(index).is_visible()]
            value = visible[0].input_value() if len(visible) == 1 else None
            comparable = value
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                    if isinstance(decoded, str):
                        comparable = decoded
                except json.JSONDecodeError:
                    pass
            fields[label] = {
                "one_visible_control": len(visible) == 1,
                "value_matched": comparable == expected[label],
                "value_digest": _digest(value),
            }
        return {
            "all_values_matched": all(row["one_visible_control"]
                                      and row["value_matched"]
                                      for row in fields.values()),
            "fields": fields,
        }

    return probe


def _count(executor, model: str, domain: list) -> int:
    result = executor.call(model, "search_count", [domain])
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise RuntimeError("Odoo returned an invalid record count")
    return result


def _record_state(adapter: OdooApplicationAdapter, case: FormCase) -> dict:
    state = {"count": _count(adapter._executor, case.model, [])}
    if case.marker is not None:
        field, value = case.marker
        state["marker_count"] = _count(
            adapter._executor, case.model, [[field, "=", value]])
    return state


def _event_counts(summary: dict) -> dict[str, int]:
    path = Path(str(summary["output"])) / "audit.jsonl"
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line).get("event")
        except json.JSONDecodeError:
            continue
        if isinstance(event, str):
            counts[event] = counts.get(event, 0) + 1
    return counts


def run_case(case: FormCase, args, output_dir: Path,
             adapter: OdooApplicationAdapter) -> dict:
    before = _record_state(adapter, case)
    if before.get("marker_count", 0):
        raise RuntimeError("the synthetic form marker already exists")
    holder: dict[str, FormQualificationChat] = {}

    def factory(policy: ProviderEgressPolicy) -> ScriptedOdooChat:
        chat = FormQualificationChat(case, egress_policy=policy)
        holder["chat"] = chat
        return chat

    started = time.perf_counter()
    summary = run_demo(OdooDemoConfig(
        base_url=args.base_url,
        api_key_path=Path(args.api_key),
        storage_state_path=Path(args.storage_state),
        page_controller_module=args.page_controller_module,
        scripted=True,
        scripted_task=case.task,
        scripted_chat_factory=factory,
        headless=True,
        entry_path=case.entry_path,
        output_dir=output_dir,
        turn_timeout_s=args.turn_timeout,
        keep_open_seconds=0.0,
        browser_ignore_https_errors=args.ignore_browser_https_errors,
        final_page_probe=_page_probe(case),
    ))
    ended = time.perf_counter()
    after = _record_state(adapter, case)
    chat = holder["chat"]
    turns = summary.get("turns") or []
    turn = turns[0] if turns else {}
    events = _event_counts(summary)
    trace = chat.decision_trace
    round_trips = [{
        "tool": trace[index][0],
        "elapsed_ms": round((trace[index + 1][1] - trace[index][1]) * 1_000, 3),
    } for index in range(max(0, len(trace) - 1))]
    latencies = {
        "tool_round_trips": round_trips,
        "prefill_ms": round((ended - trace[-1][1]) * 1_000, 3) if trace else None,
        "total_ms": round((ended - started) * 1_000, 3),
    }
    probe = summary.get("final_page_probe") or {}
    checks = {
        "turn_finished_after_user_takeover": turn.get("terminal_reason") == "agent_finished",
        "requested_fields_were_described": (
            [row.get("label") for row in chat.described_fields]
            == [label for label, _value in case.values]),
        "descriptor_prepare_and_prefill_completed": (
            events.get("application_form_described", 0) in {1, 2}
            and events.get("application_form_prepared", 0) == 1
            and events.get("form_prefill_completed", 0) == 1),
        "no_business_skill_loaded": events.get("business_skill_loaded", 0) == 0,
        "generic_form_tools_were_offered": {
            "describe_form", "prepare_form", "prefill_form"
        }.issubset(summary.get("tools_offered") or []),
        "scripted_provider_used_zero_calls_and_cost": (
            summary.get("model_usage", {}).get("provider_calls") == 0
            and summary.get("model_usage", {}).get("cost_cny") == 0.0),
        "every_requested_value_reached_the_live_dom": (
            probe.get("all_values_matched") is True),
        "no_record_was_created": before == after,
    }
    return {
        "case": case.name,
        "entry_path": case.entry_path,
        "field_contract": chat.described_fields,
        "field_count": len(case.values),
        "record_count_before": before["count"],
        "record_count_after": after["count"],
        "marker_absent_before_and_after": (
            before.get("marker_count", 0) == after.get("marker_count", 0) == 0),
        "page_probe": probe,
        "latency": latencies,
        "page_actions": turn.get("page_actions"),
        "document_reloads_after_entry": summary.get("document_reloads_after_entry"),
        "task_digest": _digest(case.task),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _emit(artifact: dict, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if artifact["status"] == "READY" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-form-qualify",
        description="Model-free live qualification for generic Odoo form prefill.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--storage-state", required=True)
    parser.add_argument("--page-controller-module", default=(
        "http://127.0.0.1:8069/woa_runtime/static/src/page-controller.js"))
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    parser.add_argument("--ignore-browser-https-errors", action="store_true",
                        help="loopback qualification only; rejected for non-loopback URLs")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    nonce = secrets.token_hex(4)
    cases = form_cases(nonce)
    adapter = OdooApplicationAdapter(
        base_url=args.base_url,
        api_key_path=Path(args.api_key),
        page_controller_module=args.page_controller_module,
    )
    health = adapter.health()
    bound = bind_application(adapter)
    results = []
    with tempfile.TemporaryDirectory(prefix="woa-odoo-form-qualification-") as tmp:
        for case in cases:
            try:
                results.append(run_case(case, args, Path(tmp), adapter))
            except Exception as exc:
                results.append({
                    "case": case.name,
                    "entry_path": case.entry_path,
                    "failure_type": type(exc).__name__,
                    "passed": False,
                })
    checks = {
        "application_reachable": bool(health.get("passed")),
        "all_frozen_form_cases_ran": len(results) == len(cases),
        "all_frozen_form_cases_passed": all(row.get("passed") is True for row in results),
    }
    artifact = {
        "schema_version": 1,
        "kind": "woa_odoo_generic_form_qualification",
        "application_release": f"odoo@{health.get('server_version', '')}",
        "catalog_digest": "sha256:" + bound.descriptor.catalog_sha256,
        "case_count": len(results),
        "cases": results,
        "checks": checks,
        "model_usage": {"provider_calls": 0, "cost_cny": 0.0},
        "status": "READY" if all(checks.values()) else "FAIL",
    }
    return _emit(artifact, Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
