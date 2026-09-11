"""Draft and check Odoo Business Skills against the deployed release's own descriptors.

Two commands, which are the two halves of the project's oldest claim -- that a Skill should
be derived from the application's source rather than written from memory:

    python -m web_operation_agent_odoo.demo.skills verify --source-root ... --release ...
    python -m web_operation_agent_odoo.demo.skills draft  --action sale.action_orders \\
        --tools query_sale_orders --source-root ... --release ...

`verify` is the one that runs in a gate. It resolves every ``src:`` citation in every
packaged Skill against the compiled ``SourceMap``: the file must still be one the compiler
reads, and the anchor must still be something declared inside it. That is what makes
citation rot visible -- the first run of it found that all four shipped Skills cited
*unqualified* Odoo XML ids (``action_orders``), which is ambiguous across modules: four
different addons declare a ``product_template_action``.

`draft` compiles one action into a reviewable Skill. It writes the facts a descriptor can
state and refuses to write the one it cannot: the instructions are a playbook for a model,
and a compiler that generated them would be inventing the only part that needs judgement.
What it does supply is the thing prose gets wrong -- a list page that reads as "all orders"
is a filtered scope, and the filter exists only in the descriptor's ``domain``.

`--action` takes Odoo's globally qualified XML id; the subject the core compiler works with
is looked up here, because "an action id" is Odoo's word for it and not a core concept.
"""
from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from web_operation_agent.business import BusinessSkill
from web_operation_agent.source_skill import BusinessSkillCompiler

from ..route import ODOO_ROUTE_PORT, ODOO_SOURCE_SCOPE
from ..sourcemap import OdooCompiler, odoo_source_tree


APPLICATION = "odoo"


def packaged_skills() -> list[BusinessSkill]:
    """Every Skill this distribution ships, loaded from the package itself."""
    directory = resources.files("web_operation_agent_odoo").joinpath("skills")
    return [BusinessSkill.from_bytes(
                entry.read_bytes(),
                artifact_uri=f"package://web_operation_agent_odoo/skills/{entry.name}")
            for entry in sorted(directory.iterdir()) if entry.name.endswith(".json")]


def compiler(source_root: str, release: str) -> BusinessSkillCompiler:
    tree = odoo_source_tree(Path(source_root).resolve(), release=release)
    return BusinessSkillCompiler(OdooCompiler(tree, "skill-compiler").compile(),
                                 application=APPLICATION, route_port=ODOO_ROUTE_PORT)


def verify_packaged_skills(source_root: str, release: str) -> dict:
    """The gate shape: one verdict, and every unresolved citation named."""
    checker = compiler(source_root, release)
    reports = [checker.verify(skill).to_json() for skill in packaged_skills()]
    return {
        "schema_version": 1,
        "kind": "woa_odoo_skill_citations",
        "release": release,
        "skill_count": len(reports),
        "source_citation_count": sum(
            len([check for check in report["checks"] if check["kind"] == "src"])
            for report in reports),
        "unresolved": [
            {"skill_id": report["skill_id"], "citation": check["citation"],
             "reason": check["reason"]}
            for report in reports for check in report["checks"]
            if check["kind"] == "src" and not check["resolved"]],
        "reports": reports,
        "status": "READY" if all(report["passed"] for report in reports) else "FAILED",
    }


def _subject_of(checker: BusinessSkillCompiler, action_id: str) -> str:
    matches = {row.subject for row in checker.source_map.by_kind("request_handler")
               if row.attrs.get("action_id") == action_id}
    if len(matches) != 1:
        raise SystemExit(f"{action_id} matches {len(matches)} declared requests")
    return matches.pop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-skills",
        description="Draft and check Odoo Skills against the deployed release's source.")
    parser.add_argument("command", choices=("verify", "draft"))
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--action", default="", help="a globally qualified Odoo XML id")
    parser.add_argument("--skill-id", default="")
    parser.add_argument("--tools", default="", help="comma-separated typed tool names")
    parser.add_argument("--methods", default="",
                        help="comma-separated model methods whose declarations to quote")
    parser.add_argument("--write", action="store_true", help="a mutation Skill draft")
    parser.add_argument("--output", default="")
    arguments = parser.parse_args(argv)

    if arguments.command == "verify":
        report = verify_packaged_skills(arguments.source_root, arguments.release)
        if arguments.output:
            Path(arguments.output).parent.mkdir(parents=True, exist_ok=True)
            Path(arguments.output).write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items()
                          if key != "reports"},
                         ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["status"] == "READY" else 1

    if not arguments.action or not arguments.skill_id or not arguments.tools:
        parser.error("draft needs --action, --skill-id and --tools")
    checker = compiler(arguments.source_root, arguments.release)
    draft = checker.draft(
        skill_id=arguments.skill_id,
        subject=_subject_of(checker, arguments.action),
        tool_names=tuple(name.strip() for name in arguments.tools.split(",") if name.strip()),
        read_only=not arguments.write,
        methods=tuple(name.strip() for name in arguments.methods.split(",") if name.strip()),
    )
    rendered = json.dumps(draft.to_json(), ensure_ascii=False, sort_keys=True, indent=2)
    if arguments.output:
        Path(arguments.output).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    # A draft is never a shipped Skill. Exit 0 with the review list visible: a person reads
    # it, writes the instructions, and only then does the artifact belong in `skills/`.
    print(f"\n{len(draft.review)} items need a person before this is a Skill:",
          file=sys.stderr)
    for item in draft.review:
        print(f"  - {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
