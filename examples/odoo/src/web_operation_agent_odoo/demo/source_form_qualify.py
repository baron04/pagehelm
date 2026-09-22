"""Model-free Odoo gate for a source-derived value in the real current-form path.

The scripted decision-maker may use only the cited source result and fresh opaque form
bindings. It never calls a provider or clicks Save. A hidden DOM/database oracle verifies
the draft; this is a wiring qualification, not evidence that source improves model success.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import tempfile
from pathlib import Path

from web_operation_agent.modeling.client import load_env
from web_operation_agent.modeling.egress import ProviderEgressPolicy

from ..adapter import OdooApplicationAdapter
from .demo import OdooDemoConfig, run_demo
from .form_qualify import (FormCase, FormQualificationChat, _event_counts,
                           _page_probe, _record_state)


QUERY = "product.template invoice_policy Delivered quantities delivery"
SELECTION = "Delivered quantities"
SUPPORT = "('delivery', 'Delivered quantities')"


def _selected_value(source_result: str) -> str:
    """Parse the cited selection, never infer a native option from a task string."""
    if "source#1" not in source_result or SUPPORT not in source_result:
        raise ValueError("the expected cited selection was not retrieved")
    matches = re.findall(r"\('([^']+)',\s*'Delivered quantities'\)", source_result)
    if len(set(matches)) != 1:
        raise ValueError("the cited selection is missing or ambiguous")
    return matches[0]


class SourceFormQualificationChat(FormQualificationChat):
    """Consume real deployed source before the existing generic form script."""

    def __init__(self, case: FormCase, *, egress_policy: ProviderEgressPolicy):
        super().__init__(case, egress_policy=egress_policy)
        self._source_phase = "lookup"
        self.source_value = ""

    def complete(self, messages, tools, run_id, **kwargs):
        if self._source_phase == "lookup":
            self.calls += 1
            self.tool_surfaces.append(sorted(
                tool["function"]["name"] for tool in (tools or [])))
            self._source_phase = "consume"
            return self._response("lookup_source", {"query": QUERY})
        if self._source_phase == "consume":
            result = str(messages[-1].get("content") or "")
            self.source_value = _selected_value(result)
            self.case = dataclasses.replace(
                self.case, values=(("Invoicing Policy", self.source_value),))
            self._source_phase = "used"
        response = super().complete(messages, tools, run_id, **kwargs)
        function = response["tool_calls"][0]["function"]
        if function["name"] == "prepare_form":
            arguments = json.loads(function["arguments"])
            for value in arguments["values"]:
                value["source_basis"] = {"ref": "source#1", "support": SUPPORT}
            function["arguments"] = json.dumps(arguments)
        return response


def run_gate(config: OdooDemoConfig) -> dict:
    case = FormCase(
        name="source_derived_invoice_policy", entry_path="/odoo/products/new",
        task=("On this unsaved new Product form, choose the invoicing policy that "
              "invoices after delivery based on quantities delivered. Do not save."),
        values=(("Invoicing Policy", "delivery"),),
        selectors=(("Invoicing Policy", '[name="invoice_policy"] select'),),
        model="product.template",
    )
    adapter = OdooApplicationAdapter(
        base_url=config.base_url, api_key_path=config.api_key_path,
        page_controller_module=config.page_controller_module,
        allow_generic_forms=True)
    before = _record_state(adapter, case)
    holder = {}

    def factory(policy):
        chat = SourceFormQualificationChat(case, egress_policy=policy)
        holder["chat"] = chat
        return chat

    summary = run_demo(dataclasses.replace(
        config, scripted=True, scripted_task=case.task, scripted_chat_factory=factory,
        tasks=(), headless=True, entry_path=case.entry_path,
        allow_generic_forms=True, keep_open_seconds=0,
        final_page_probe=_page_probe(case)))
    after = _record_state(adapter, case)
    events = _event_counts(summary)
    chat = holder["chat"]
    audit = [json.loads(line) for line in
             (Path(summary["output"]) / "audit.jsonl").read_text().splitlines()]
    prepared = [row for row in audit if row.get("event") == "application_form_prepared"]
    retrieved = [row for row in audit if row.get("event") == "runtime_source_retrieved"]
    checks = {
        "deployed_source_returned": len(retrieved) == 1 and
            any(SUPPORT in line for line in retrieved[0].get("evidence", [])),
        "source_determined_value": chat.source_value == "delivery",
        "source_ref_bound_to_draft": len(prepared) == 1 and
            prepared[0].get("source_refs") == ["source#1"],
        "draft_reached_live_dom": (summary.get("final_page_probe") or {}).get(
            "all_values_matched") is True,
        "normal_stop": summary["turns"][0]["terminal_reason"] == "agent_finished",
        "no_persistence": before == after and
            events.get("business_mutation_result", 0) == 0,
        "no_skill_loaded": events.get("business_skill_loaded", 0) == 0,
        "no_paid_model": summary["model_usage"].get("provider_calls") == 0,
    }
    return {"status": "READY" if all(checks.values()) else "FAIL",
            "checks": checks, "source_query": QUERY,
            "source_citations": retrieved[0].get("citations", 0) if retrieved else 0,
            "source_lines": len(retrieved[0].get("evidence", [])) if retrieved else 0,
            "turn": {key: summary["turns"][0].get(key)
                     for key in ("terminal_reason", "steps", "page_actions")},
            "record_count_before": before["count"],
            "record_count_after": after["count"],
            "model_usage": summary["model_usage"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    env = load_env(args.model_env)
    output = Path(tempfile.mkdtemp(prefix="woa-odoo-source-form-"))
    config = OdooDemoConfig(
        base_url=env["WOA_ODOO_BASE_URL"],
        api_key_path=Path(env["WOA_ODOO_API_KEY_FILE"]),
        storage_state_path=Path(env["WOA_ODOO_STORAGE_STATE"]),
        page_controller_module=env["WOA_ODOO_PAGE_CONTROLLER_MODULE"],
        source_root=Path(env["WOA_ODOO_SOURCE_ROOT"]),
        source_release=env["WOA_ODOO_SOURCE_RELEASE"],
        output_dir=output)
    result = run_gate(config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    print(json.dumps({"private_output": str(output)}, sort_keys=True), flush=True)
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
