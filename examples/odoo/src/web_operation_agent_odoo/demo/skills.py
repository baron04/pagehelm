"""Verify packaged source references or draft source facts for manual workflow review."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.planning.source_skill import BusinessSkillCompiler

from ..route import ODOO_ROUTE_PORT
from ..skill_packages import packaged_skills as installed_skills
from ..sourcemap import OdooCompiler, odoo_source_tree


APPLICATION = "odoo"


def packaged_skills():
    """Citation-only projection; installed workflows remain Markdown packages."""
    return [SimpleNamespace(skill_id=skill.skill_id, provenance=tuple(json.loads(
        skill.read_resource("references/source-provenance.json"))["provenance"]))
        for skill in installed_skills().artifacts]


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
