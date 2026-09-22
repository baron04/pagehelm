"""Frozen four-arm source-operation gate using the production Agent and browser.

Run ``--pilot`` first; only a clean apparatus pilot qualifies ``--formal``. Both modes
call the configured paid model. Raw audits stay in a fresh system temporary directory;
the projected result contains only boolean or aggregate verdicts, not form values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from testapps.contacts.demo import DemoConfig, run_demo
from web_operation_agent.business.runtime import BusinessSkill
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.planning.repository_source import (CombinedSourceLookup,
                                                            RepositorySourceLookup)
from web_operation_agent.planning.source_facts import CompiledSourceLookup
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.schemas import SourceMap

from .contacts_semantic_form import _probe, source_capability


TASKSET = Path(__file__).with_name("taskset-v1.json")
TASKSET_SHA256 = "6eeec952f81470774d9dbfc9c854a1ea00d8fd3a3825ec53974bd51a2bd7d383"
ARMS = ("A", "B", "C", "D")


def _tasks() -> dict:
    raw = TASKSET.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TASKSET_SHA256:
        raise RuntimeError("the frozen source-operation taskset changed")
    tasks = json.loads(raw)
    if tasks.get("schema_version") != 1 or len(tasks.get("semantic", [])) != 2 \
            or len(tasks.get("negative", [])) != 2:
        raise RuntimeError("the source-operation taskset has an invalid shape")
    return tasks


def _skill(release: str, official_name: str) -> BusinessSkill:
    artifact = {
        "schema_version": 1,
        "skill_id": "minimal.contacts.orion-official-name",
        "title": "Orion contact naming rule",
        "intent": "Fill Orion's current official name in a new contact draft.",
        "triggers": ["vendor Orion", "Orion"],
        "instructions": [
            f"For this release, vendor Orion's official contact display name is "
            f"{official_name}. Use this exact Name when the user asks for its official name.",
            "The current page is a new-contact form. Call describe_form, copy the exact "
            "form_id and field_id values from that response into prepare_form, then use "
            "the returned draft_id in prefill_form. Stop when the unsaved draft is filled; "
            "never save or submit it.",
            "Do not call list_contacts: existing records cannot establish this release's "
            "official naming rule, which this Skill already states.",
        ],
        "tool_names": ["list_contacts"],
        "provenance": [f"src:minimal_contacts@{release}:apps/contacts/rules.py"],
        "read_only": True,
    }
    return BusinessSkill.from_bytes(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode(),
        artifact_uri=f"eval://source-operation/{release}/orion-skill")


def _source(root: Path, release: str, official_name: str, arm: str
            ) -> tuple[SourceCapability, bool]:
    compiled = source_capability(root, release, official_name)
    if arm not in {"B", "C"}:
        return SourceCapability.none(), False
    tree = SourceTree({
        "repository_id": "pilot/contacts", "release": release,
        "allow_globs": ["apps/**/*.py"], "deny_globs": ["**/secrets/**"],
    }, root)
    raw = RepositorySourceLookup(tree)
    if arm == "B":
        source_map = SourceMap(
            schema_version=compiled.source_map.schema_version,
            attestation_id=compiled.source_map.attestation_id,
            repository_id=compiled.source_map.repository_id,
            release=release, relations=[])
        lookup = raw
    else:
        source_map = compiled.source_map
        lookup = CombinedSourceLookup(CompiledSourceLookup(source_map), raw)
    offline = lookup.search("official display name for vendor Orion", limit=5)
    in_top5 = any(official_name in hit.line for hit in offline)
    return SourceCapability(source_map=source_map, runtime_source=True,
                            lookup=lookup), in_top5


def _case_row(output: Path, case: dict, arm: str, repeat: int,
              model_env: Path) -> dict:
    semantic = "official_name" in case
    release = case.get("release", "release-partner")
    official_name = case.get("official_name", "Orion Partner")
    source_root = output / "sources" / release
    source, offline_top5 = _source(source_root, release, official_name, arm)
    skill = (_skill(release, official_name),) if arm == "D" else ()
    if semantic:
        probe = _probe(official_name, case["expected_email"])
    elif case["kind"] == "visible_click":
        probe = lambda page: {"target_path": (
            urlsplit(page.url).path == case["expected_path"])}
    else:
        probe = lambda page: {"stayed_on_list": (
            urlsplit(page.url).path == case["entry_path"])}
    config = DemoConfig(
        headless=True, tasks=(case["prompt"],),
        output_dir=output / f"r{repeat}-{case['id']}-{arm}",
        model_env=model_env, start_path=case["entry_path"], source=source,
        extra_skills=skill,
        allow_page_click=not semantic and case["kind"] == "visible_click",
        final_page_probe=probe, turn_timeout_s=120,
    )
    started = time.monotonic()
    summary = run_demo(config)
    events = [json.loads(line) for line in
              (Path(summary["output"]) / "audit.jsonl").read_text().splitlines()]
    retrieved = [row for row in events if row.get("event") == "runtime_source_retrieved"]
    loaded_skills = [row.get("skill_id") for row in events
                     if row.get("event") in {"business_skill_auto_loaded",
                                               "business_skill_loaded"}]
    rank = next((index for row in retrieved for index, line in
                 enumerate(row.get("evidence", [])[:5], 1)
                 if official_name in line), None) if semantic else None
    verdict = (summary.get("panel") or {}).get("final_page_probe") or {}
    turn = summary["turns"][0]
    writes = summary["application_state"]["write_requests"]
    created = summary["application_state"]["records_created"]
    if semantic:
        achieved = (verdict.get("name_matched") is True
                    and verdict.get("email_matched") is True
                    and any(row.get("event") == "form_prefill_completed" for row in events))
    elif case["kind"] == "visible_click":
        achieved = (verdict.get("target_path") is True
                    and any(row.get("event") == "action_receipt"
                            and row.get("tool") == "click" and row.get("ok")
                            for row in events))
    else:
        used_api = any(row.get("event") == "business_tool_result"
                       and row.get("tool") in {"list_contacts", "query_entity_records"}
                       for row in events)
        exact = bool(re.search(r"(?<!\d)3(?!\d)|\bthree\b",
                               turn.get("answer", ""), flags=re.IGNORECASE))
        achieved = verdict.get("stayed_on_list") is True and used_api and exact
    usage = summary["model_usage"]
    row = {
        "case": case["id"], "arm": arm, "repeat": repeat,
        "kind": "semantic" if semantic else case["kind"],
        "passed": bool(achieved and turn["terminal_reason"] == "agent_finished"
                       and writes == 0 and created == 0),
        "achieved": bool(achieved), "terminal_reason": turn["terminal_reason"],
        "steps": turn["steps"], "page_actions": turn["page_actions"],
        "source_lookups": len(retrieved), "offline_top5": offline_top5,
        "runtime_target_rank": rank, "source_query": [row.get("query") for row in retrieved],
        "source_refs_prepared": [row.get("source_refs") for row in events
                                 if row.get("event") == "application_form_prepared"],
        "loaded_skills": loaded_skills,
        "name_matched": verdict.get("name_matched") if semantic else None,
        "email_matched": verdict.get("email_matched") if semantic else None,
        "writes": writes, "created": created,
        "wall_s": round(time.monotonic() - started, 3),
        "provider_calls": usage.get("provider_calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "cost_cny": usage.get("cost_cny", 0.0),
    }
    if writes or created:
        raise RuntimeError("unexpected application write; stop all evaluation")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pilot", action="store_true")
    group.add_argument("--formal", action="store_true")
    parser.add_argument("--model-env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    tasks = _tasks()
    output = Path(tempfile.mkdtemp(prefix="woa-source-abcd-"))
    print(json.dumps({"private_output": str(output),
                      "taskset_sha256": TASKSET_SHA256,
                      "mode": "pilot" if args.pilot else "formal"}), flush=True)
    cases = []
    for repeat in range(1, 2 if args.pilot else 4):
        semantic = tasks["semantic"][:1] if args.pilot else tasks["semantic"]
        for case in semantic:
            cases.extend((case, arm, repeat) for arm in ARMS)
        for case in tasks["negative"]:
            cases.extend((case, arm, repeat) for arm in ("A", "C"))
    results = []
    for case, arm, repeat in cases:
        row = _case_row(output, case, arm, repeat, args.model_env)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    (output / "projection.json").write_text(
        json.dumps(results, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    apparatus = all(row["writes"] == row["created"] == 0 for row in results)
    apparatus &= all(row["offline_top5"] and row["runtime_target_rank"] is not None
                     for row in results if row["kind"] == "semantic"
                     and row["arm"] in {"B", "C"})
    apparatus &= all(row["loaded_skills"] for row in results
                     if row["kind"] == "semantic" and row["arm"] == "D")
    print(json.dumps({"attempts": len(results), "apparatus_ready": apparatus,
                      "passed": sum(row["passed"] for row in results),
                      "provider_calls": sum(row["provider_calls"] for row in results),
                      "cost_cny": round(sum(row["cost_cny"] for row in results), 4)},
                     sort_keys=True), flush=True)
    return 0 if apparatus else 1


if __name__ == "__main__":
    raise SystemExit(main())
