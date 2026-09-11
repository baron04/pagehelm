#!/usr/bin/env python3
"""Assert each distribution's wheel ships exactly what it should and nothing else.

Run after building all three wheels into their distribution's own ``dist/`` directory. Exits
1 and prints every violation on failure; the wheel-build gate that calls this must not report
a pass while this script has anything to say.
"""
from __future__ import annotations

import glob
import sys
import zipfile

DISTRIBUTIONS = {
    "web-operation-agent (core)": {
        "glob": "dist/*.whl",
        "required": [
            "web_operation_agent/durable.py",
            "web_operation_agent/mutation_ops.py",
            "web_operation_agent/application.py",
            "web_operation_agent/sourcetree.py",
            "web_operation_agent/xmlscan.py",
            "web_operation_agent/policies/model_options.json",
            "web_operation_agent/policies/prices.json",
            "web_operation_agent/web_runtime/embedded.js",
            "web_operation_agent/web_runtime/trace_replay.html",
            "web_operation_agent_demo/assets/page_controller.js",
        ],
        "forbidden": [
            "web_operation_agent/browser_worker.py",
            "web_operation_agent/cli.py",
            "web_operation_agent/policies/enforcement.json",
        ],
        # The core package must not carry any application's own assets. A name containing
        # either application's slug, wherever it appears in the path, is a leak.
        "forbidden_name_substrings": ["ofbiz", "twenty"],
    },
    "web-operation-agent-ofbiz": {
        "glob": "packages/web-operation-agent-ofbiz/dist/*.whl",
        "required": [
            "web_operation_agent_ofbiz/__init__.py",
            "web_operation_agent_ofbiz/route.py",
            "web_operation_agent_ofbiz/knowledge.py",
            "web_operation_agent_ofbiz/sourcemap.py",
            "web_operation_agent_ofbiz/policies/ofbiz.json",
        ],
        "forbidden_name_substrings": ["twenty"],
    },
    "web-operation-agent-twenty": {
        "glob": "packages/web-operation-agent-twenty/dist/*.whl",
        "required": [
            "web_operation_agent_twenty/__init__.py",
            "web_operation_agent_twenty/adapter.py",
            "web_operation_agent_twenty/skills/companies-query.json",
            "web_operation_agent_twenty/skills/companies-create.json",
            "web_operation_agent_twenty/skills/people-query.json",
            "web_operation_agent_twenty/skills/opportunities-query.json",
        ],
        "forbidden_name_substrings": ["ofbiz"],
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
