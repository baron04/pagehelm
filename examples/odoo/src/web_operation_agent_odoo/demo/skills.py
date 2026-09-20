"""Draft, check and reissue Odoo Business Skills against the deployed release's descriptors.

Three commands, which are the halves of the project's oldest claim -- that a Skill should be
derived from the application's source rather than written from memory -- plus the repair:

    python -m web_operation_agent_odoo.demo.skills verify --source-root ... --release ...
    python -m web_operation_agent_odoo.demo.skills draft  --action sale.action_orders \\
        --tools query_sale_orders --source-root ... --release ...
    python -m web_operation_agent_odoo.demo.skills reissue --source-root ... --release ...

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

`reissue` is what `verify` is for. It takes the Skills `verify` judged red, recompiles their
compiled facts against this release's map, and keeps what a person wrote -- the instructions
above all, byte for byte. Before this existed, one rotted citation meant hand-resealing the
whole artifact, which is where the friction actually was.

It writes nothing without `--apply`, and what it prints first is a field-by-field diff. That
is not politeness: `--accept` defaults to every field a compiler can derive, and for these
Skills two of the three carry human edits the source cannot regenerate -- the titles are
hand-written and the trigger lists are bilingual, with Chinese aliases that appear in no
descriptor. Repairing citation rot alone is `--accept provenance`; the diff is how a reviewer
sees which of those they are doing.

`--action` takes Odoo's globally qualified XML id; the subject the core compiler works with
is looked up here, because "an action id" is Odoo's word for it and not a core concept.
"""
from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from web_operation_agent.business.runtime import BusinessSkill
from web_operation_agent.planning.source_skill import BusinessSkillCompiler

from ..route import ODOO_ROUTE_PORT, ODOO_SOURCE_SCOPE
from ..skill_sources import SKILL_SOURCES
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


def skill_artifact_path(skill_id: str) -> Path:
    """Where this distribution keeps one Skill, resolved from the package itself."""
    for entry in resources.files("web_operation_agent_odoo").joinpath("skills").iterdir():
        if not entry.name.endswith(".json"):
            continue
        if json.loads(entry.read_bytes()).get("skill_id") == skill_id:
            return Path(str(entry))
    raise SystemExit(f"no packaged artifact declares {skill_id}")


def reissue_packaged_skills(source_root: str, release: str, *,
                            accept: tuple[str, ...],
                            only: tuple[str, ...] = ()) -> dict:
    """Recompile the named Skills (default: the ones `verify` judged red) as diffs.

    `verify` stays the entry point. Reissuing a Skill whose citations all resolve would
    rewrite an artifact nobody asked about, and would make every release's diff noise --
    the whole value of a citation gate is that it names the few things that moved.
    """
    checker = compiler(source_root, release)
    skills = {skill.skill_id: skill for skill in packaged_skills()}
    if not only:
        only = tuple(sorted(
            report["skill_id"] for report in
            (checker.verify(skill).to_json() for skill in skills.values())
            if not report["passed"]))
    reissues = []
    for skill_id in only:
        if skill_id not in skills:
            raise SystemExit(f"no packaged Skill is called {skill_id}")
        if skill_id not in SKILL_SOURCES:
            raise SystemExit(
                f"{skill_id} declares no drafting source; add it to skill_sources.py")
        subject, methods = SKILL_SOURCES[skill_id]
        reissues.append(checker.reissue(skills[skill_id], subject=subject,
                                        methods=methods, recompute=accept))
    return {
        "schema_version": 1,
        "kind": "woa_odoo_skill_reissue",
        "release": release,
        "accepted_fields": list(accept),
        "skill_ids": list(only),
        "reissued": [item.to_json() for item in reissues],
        "reissues": reissues,
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
    parser.add_argument("command", choices=("verify", "draft", "reissue"))
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--action", default="", help="a globally qualified Odoo XML id")
    parser.add_argument("--skill-id", default="")
    parser.add_argument("--tools", default="", help="comma-separated typed tool names")
    parser.add_argument("--methods", default="",
                        help="comma-separated model methods whose declarations to quote")
    parser.add_argument("--write", action="store_true", help="a mutation Skill draft")
    parser.add_argument("--skill-ids", default="",
                        help="reissue: comma-separated Skills to reissue instead of the "
                             "ones verify judged red")
    parser.add_argument("--accept", default=",".join(
                            BusinessSkillCompiler.RECOMPUTABLE_FIELDS),
                        help="reissue: which compiled fields to take from the new map; "
                             "'provenance' alone repairs citation rot and touches nothing "
                             "a person edited")
    parser.add_argument("--apply", action="store_true",
                        help="reissue: write the artifacts back; without it nothing on "
                             "disk changes and only the diff is printed")
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

    if arguments.command == "reissue":
        accept = tuple(name.strip() for name in arguments.accept.split(",")
                       if name.strip())
        unknown = sorted(set(accept) - set(BusinessSkillCompiler.RECOMPUTABLE_FIELDS))
        if unknown:
            parser.error(f"a compiler cannot recompute {unknown}")
        report = reissue_packaged_skills(
            arguments.source_root, arguments.release, accept=accept,
            only=tuple(name.strip() for name in arguments.skill_ids.split(",")
                       if name.strip()))
        reissues = report.pop("reissues")
        if arguments.output:
            Path(arguments.output).parent.mkdir(parents=True, exist_ok=True)
            Path(arguments.output).write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
        if not reissues:
            print("every packaged Skill's citations resolve in this release; "
                  "nothing to reissue")
            return 0
        for item in reissues:
            print(item.render())
            print()
        if not arguments.apply:
            print(f"{len(reissues)} artifact(s) would change. Nothing was written; "
                  "re-run with --apply.", file=sys.stderr)
            return 0
        for item in reissues:
            path = skill_artifact_path(item.skill_id)
            path.write_text(json.dumps(item.skill, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            print(f"wrote {path}", file=sys.stderr)
        return 0

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
