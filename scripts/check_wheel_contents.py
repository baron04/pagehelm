#!/usr/bin/env python3
"""Assert each distribution's wheel ships exactly what it should and nothing else.

Run after building every wheel into their distribution's own ``dist/`` directory. Exits
1 and prints every violation on failure; the wheel-build gate that calls this must not report
a pass while this script has anything to say.
"""
from __future__ import annotations

import glob
import sys
import zipfile

DISTRIBUTIONS = {
    "web-operation-agent (core)": {
        "glob": "dist/web_operation_agent-[0-9]*.whl",
        "required": [
            "web_operation_agent/state/store.py",
            "web_operation_agent/transport/registry.py",
            "web_operation_agent/business/operations.py",
            "web_operation_agent/business/entities.py",
            "web_operation_agent/business/page_entries.py",
            "web_operation_agent/business/forms.py",
            "web_operation_agent/composition/binding.py",
            "web_operation_agent/composition/ports.py",
            "web_operation_agent/skills/artifacts.py",
            "web_operation_agent/business/tools/registry.py",
            "web_operation_agent/business/write_service.py",
            "web_operation_agent/business/dispatch_evidence.py",
            "web_operation_agent/execution/events.py",
            "web_operation_agent/goal/source_answers.py",
            "web_operation_agent/goal/write_coverage.py",
            "web_operation_agent/goal/read_tools.py",
            "web_operation_agent/goal/read_schemas.py",
            "web_operation_agent/goal/resolve/read_evidence.py",
            "web_operation_agent/execution/data_tools.py",
            "web_operation_agent/execution/action_admission.py",
            "web_operation_agent/execution/model_loop.py",
            "web_operation_agent/execution/model_loop_contracts.py",
            "web_operation_agent/execution/model_sources.py",
            "web_operation_agent/business/draft_steps.py",
            "web_operation_agent/execution/admitted_actions.py",
            "web_operation_agent/planning/repository_source.py",
            "web_operation_agent/sourcetree.py",
            "web_operation_agent/xmlscan.py",
            "web_operation_agent/policies/model_options.json",
            "web_operation_agent/policies/prices.json",
            # The Runtime and the panel, in the one file a host page includes. A wheel
            # without it cannot serve the assistant at all, so its absence is a packaging
            # failure, not something to discover at runtime.
            "web_operation_agent/web_runtime/assistant.js",
            "web_operation_agent/web_runtime/browser_contract.json",
            "web_operation_agent/web_runtime/panel_contract.json",
            # Built from `frontend/trace/` by scripts/build_panel.py, palette inlined.
            "web_operation_agent/web_runtime/trace_replay.html",
            # The pinned PageController contract. Without it an installed package carries no
            # statement of which build it is qualified against, which is the state this file
            # was added to end.
            "web_operation_agent/policies/page_controller.json",
        ],
        "forbidden": [
            "web_operation_agent/browser_worker.py",
            "web_operation_agent/cli.py",
            "web_operation_agent/policies/enforcement.json",
            # Replaced by the panel built from `frontend/`.
            "web_operation_agent/web_runtime/assistant_panel.js",
            # Replaced by `assistant.js`, built from `frontend/src/runtime/`.
            "web_operation_agent/web_runtime/embedded.js",
            "web_operation_agent/web_runtime/panel_host.js",
            # The panel as a module of its own; it is part of `assistant.js`.
            "web_operation_agent/web_runtime/panel/panel.js",
            "web_operation_agent/web_runtime/panel/manifest.json",
            # The design tokens are the frontend's; the built pages carry them already.
            "web_operation_agent/web_runtime/design_tokens.json",
        ],
        # The core package must not carry any application's own assets. A name containing
        # The core wheel carries no application, example, or internal test application.
        "forbidden_name_substrings": ["odoo", "yudao", "demo", "contacts", "testapps"],
    },
    "web-operation-agent-odoo": {
        "glob": "examples/odoo/dist/web_operation_agent_odoo-[0-9]*.whl",
        "required": [
            "web_operation_agent_odoo/__init__.py",
            "web_operation_agent_odoo/route.py",
            "web_operation_agent_odoo/adapter.py",
            "web_operation_agent_odoo/sourcemap.py",
            "web_operation_agent_odoo/server/generic_forms.py",
            "web_operation_agent_odoo/server/page_entries.py",
            "web_operation_agent_odoo/demo/form_qualify.py",
            "web_operation_agent_odoo/policies/odoo.json",
            "web_operation_agent_odoo/skills/customer-maintenance/SKILL.md",
            "web_operation_agent_odoo/skills/customer-maintenance/agents/woa.toml",
            "web_operation_agent_odoo/skills/customer-maintenance/references/source-provenance.json",
            "web_operation_agent_odoo/skills/sales-processing/SKILL.md",
            "web_operation_agent_odoo/skills/order-billing/SKILL.md",
            "web_operation_agent_odoo/skills/catalog-and-records/SKILL.md",
        ],
        # The fixture provisioner reads a Compose file and an addon from a checkout. Shipping
        # it would put a fixture master password in a wheel and offer a command that cannot
        # work once installed.
        "forbidden_prefixes": ["web_operation_agent_odoo/deploy/"],
        "forbidden_name_substrings": ["contacts", "testapps"],
    },
    "web-operation-agent-yudao": {
        "glob": "examples/yudao/dist/web_operation_agent_yudao-[0-9]*.whl",
        "required": [
            "web_operation_agent_yudao/__init__.py",
            "web_operation_agent_yudao/application.py",
            "web_operation_agent_yudao/client.py",
            "web_operation_agent_yudao/goal.py",
            "web_operation_agent_yudao/native_server.py",
            "web_operation_agent_yudao/deployment_validation.py",
            "web_operation_agent_yudao/model_page_policy.py",
            "web_operation_agent_yudao/read_actions.py",
            "web_operation_agent_yudao/generic_reads.py",
            "web_operation_agent_yudao/repository.py",
            "web_operation_agent_yudao/turn.py",
            "web_operation_agent_yudao/writes.py",
            "web_operation_agent_yudao/skill_packages.py",
            "web_operation_agent_yudao/domain_playbooks/crm-sales.md",
            "web_operation_agent_yudao/domain_playbooks/erp-trade-stock.md",
            "web_operation_agent_yudao/domain_playbooks/erp-finance.md",
            "web_operation_agent_yudao/domain_playbooks/mall-catalog-promo.md",
            "web_operation_agent_yudao/domain_playbooks/mall-trade-aftersale.md",
            "web_operation_agent_yudao/domain_playbooks/hrm-org-salary.md",
            "web_operation_agent_yudao/domain_playbooks/fms-accounting.md",
            "web_operation_agent_yudao/domain_playbooks/oa-bpm-system.md",
            "web_operation_agent_yudao/write_status_ops.py",
            "web_operation_agent_yudao/skill_references/disambiguation.md",
            "web_operation_agent_yudao/skill_references/erp-units.md",
            "web_operation_agent_yudao/skill_references/hierarchy-paths.md",
            "web_operation_agent_yudao/skills/crm-customer-creation/SKILL.md",
            "web_operation_agent_yudao/skills/crm-customer-creation/agents/woa.toml",
            "web_operation_agent_yudao/skills/crm-contact-creation/SKILL.md",
            "web_operation_agent_yudao/skills/crm-contact-creation/agents/woa.toml",
            "web_operation_agent_yudao/skills/crm-customer-update/SKILL.md",
            "web_operation_agent_yudao/skills/crm-contact-update/SKILL.md",
            "web_operation_agent_yudao/skills/crm-customer-pool-release/SKILL.md",
            "web_operation_agent_yudao/skills/crm-customer-receive/SKILL.md",
            "web_operation_agent_yudao/skills/crm-customer-lock/SKILL.md",
            "web_operation_agent_yudao/skills/crm-customer-deal-status/SKILL.md",
            "web_operation_agent_yudao/skills/crm-follow-up/SKILL.md",
            "web_operation_agent_yudao/skills/crm-follow-up/agents/woa.toml",
            "web_operation_agent_yudao/write_definitions.py",
            "web_operation_agent_yudao/write_inputs.py",
            "web_operation_agent_yudao/skills/erp-supplier-maintenance/SKILL.md",
            "web_operation_agent_yudao/skills/erp-supplier-maintenance/agents/woa.toml",
            "web_operation_agent_yudao/skills/erp-warehouse-maintenance/SKILL.md",
            "web_operation_agent_yudao/skills/erp-warehouse-maintenance/agents/woa.toml",
            "web_operation_agent_yudao/skills/erp-customer-maintenance/SKILL.md",
            "web_operation_agent_yudao/skills/erp-customer-maintenance/agents/woa.toml",
            "web_operation_agent_yudao/skills/system-post-creation/SKILL.md",
            "web_operation_agent_yudao/skills/system-dept-creation/SKILL.md",
            "web_operation_agent_yudao/skills/member-tag-creation/SKILL.md",
            "web_operation_agent_yudao/skills/member-group-creation/SKILL.md",
            "web_operation_agent_yudao/skills/product-brand-maintenance/SKILL.md",
            "web_operation_agent_yudao/skills/product-category-creation/SKILL.md",
            "web_operation_agent_yudao/skills/bpm-category-maintenance/SKILL.md",
            "web_operation_agent_yudao/skills/bpm-category-maintenance/agents/woa.toml",
            "web_operation_agent_yudao/policies/repository.json",
            "web_operation_agent_yudao/source.py",
            "web_operation_agent_yudao/ui_source.py",
            "web_operation_agent_yudao/conventions/reference.json",
        ],
        "forbidden_prefixes": ["web_operation_agent_yudao/deploy/"],
        "forbidden_name_substrings": ["odoo", "contacts", "testapps"],
    },
}


def check_one(label: str, spec: dict) -> list[str]:
    problems = []
    paths = glob.glob(spec["glob"])
    if len(paths) != 1:
        return [f"{label}: expected exactly one wheel at {spec['glob']!r}, found {paths}"]
    names = zipfile.ZipFile(paths[0]).namelist()

    missing = [name for name in spec.get("required", []) if name not in names]
    if missing:
        problems.append(f"{label}: wheel is missing {missing}")

    present = [name for name in spec.get("forbidden", []) if name in names]
    if present:
        problems.append(f"{label}: wheel must not contain {present}")

    for prefix in spec.get("forbidden_prefixes", []):
        shipped = [name for name in names if name.startswith(prefix)]
        if shipped:
            problems.append(
                f"{label}: wheel must not contain anything under {prefix!r}: {shipped[:5]}")

    for needle in spec.get("forbidden_name_substrings", []):
        leaked = [name for name in names if needle in name.lower()]
        if leaked:
            problems.append(
                f"{label}: wheel leaks {needle!r}-named paths it must not ship: {leaked[:5]}")

    leaked_tests = [name for name in names if "/tests/" in name]
    if leaked_tests:
        problems.append(f"{label}: tests must not ship in the wheel: {leaked_tests[:5]}")

    if not problems:
        print(f"{label}: wheel contents verified ({paths[0]})")
    return problems


def main() -> int:
    problems = []
    for label, spec in DISTRIBUTIONS.items():
        problems.extend(check_one(label, spec))
    if problems:
        print("FAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
