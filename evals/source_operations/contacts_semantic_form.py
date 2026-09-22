"""Exploratory A/C pilot through the production turn executor and real browser.

The synthetic Contacts page and source sentence are not a deployed Odoo release. This
pilot tests whether a source fact can change a no-Skill form value, not arbitrary page
operation reliability. Run only intentionally: it calls the configured paid model.

    PYTHONPATH=src:. .venv/bin/python -m evals.source_operations.contacts_semantic_form
    PYTHONPATH=src:. .venv/bin/python -m evals.source_operations.contacts_semantic_form --negative

The independent page oracle returns booleans, not form values. Audit and usage artifacts
are written only to a fresh system temporary directory printed at startup.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from testapps.contacts.demo import DemoConfig, run_demo
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.schemas import SCHEMA_VERSIONS, SourceMap, SourceRelation


TASK = (
    "On this new-contact form, enter the current release's official display name for "
    "vendor Orion in Name and orion@example.test in Email. Leave the form as an unsaved "
    "draft; do not submit it."
)
NEGATIVE_TASK = (
    "On this new-contact form, enter Name exactly as Nova Contact and Email exactly as "
    "nova@example.test. Leave the form as an unsaved draft; do not submit it."
)
CASES = (
    ("A", "release-partner", "Orion Partner"),
    ("C", "release-partner", "Orion Partner"),
    ("A", "release-supplier", "Orion Supplier"),
    ("C", "release-supplier", "Orion Supplier"),
)


def source_capability(root: Path, release: str, official_name: str) -> SourceCapability:
    source_file = root / "apps" / "contacts" / "rules.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    statement = (f"The current official display name for vendor Orion is "
                 f"{official_name}. Use this exact value in new-contact drafts.")
    source_file.write_text("# " + statement + "\n", encoding="utf-8")
    tree = SourceTree({
        "repository_id": "pilot/contacts", "release": release,
        "allow_globs": ["apps/**/*.py"], "deny_globs": ["**/secrets/**"],
    }, root)
    source_map = SourceMap(
        schema_version=SCHEMA_VERSIONS["SourceMap"],
        attestation_id="synthetic-pilot", repository_id=tree.repository_id,
        release=release,
        relations=[SourceRelation(
            kind="service_entity", subject="contact", object="name",
            attrs={"help": statement},
            evidence=(tree.cite("apps/contacts/rules.py", 1, 1,
                                excerpt_chars=300),),
        )],
    )
    return SourceCapability(source_map=source_map, runtime_source=True)


def _probe(expected_name: str, expected_email: str):
    def check(page):
        return page.evaluate("""expected => {
          const form = document.querySelector('#contact-create');
          if (!form) return {form_visible: false, name_matched: false,
                             email_matched: false};
          return {form_visible: true,
                  name_matched: form.elements.name.value === expected.name,
                  email_matched: form.elements.email.value === expected.email};
        }""", {"name": expected_name, "email": expected_email})
    return check


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative", action="store_true",
                        help="explicit user value must prevail over a conflicting source")
    parser.add_argument("--model-env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    cases = (("C", "release-partner", "Nova Contact"),) if args.negative else CASES
    output = Path(tempfile.mkdtemp(prefix="woa-source-form-"))
    print(json.dumps({"pilot_output": str(output), "cases": len(cases)},
                     sort_keys=True), flush=True)
    for index, (arm, release, expected) in enumerate(cases, 1):
        source = source_capability(
            output / "sources" / release, release,
            "Orion Partner" if args.negative else expected)
        expected_email = "nova@example.test" if args.negative else "orion@example.test"
        config = DemoConfig(
            headless=True, tasks=(NEGATIVE_TASK if args.negative else TASK,),
            output_dir=output / f"case-{index}-{arm}",
            model_env=args.model_env, start_path="/contacts/new",
            source=source if arm == "C" else SourceCapability.none(),
            final_page_probe=_probe(expected, expected_email), turn_timeout_s=120,
        )
        started = time.monotonic()
        summary = run_demo(config)
        events = [json.loads(line) for line in
                  (Path(summary["output"]) / "audit.jsonl").read_text().splitlines()]
        verdict = (summary.get("panel") or {}).get("final_page_probe") or {}
        record = {
            "arm": arm, "release": release, "expected": expected,
            "form_visible": verdict.get("form_visible"),
            "name_matched": verdict.get("name_matched"),
            "email_matched": verdict.get("email_matched"),
            "terminal_reason": summary["turns"][0]["terminal_reason"],
            "steps": summary["turns"][0]["steps"],
            "page_actions": summary["turns"][0]["page_actions"],
            "source_lookups": sum(event.get("event") == "runtime_source_retrieved"
                                  for event in events),
            "source_refs_prepared": [event.get("source_refs") for event in events
                                     if event.get("event") == "application_form_prepared"],
            "prefilled": any(event.get("event") == "form_prefill_completed"
                             for event in events),
            "calls": summary["model_usage"].get("provider_calls"),
            "usage": summary["model_usage"],
            "wall_s": round(time.monotonic() - started, 3),
            "writes": summary["application_state"]["write_requests"],
            "created": summary["application_state"]["records_created"],
        }
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)
        if record["writes"] or record["created"]:
            raise RuntimeError("unexpected write; stop the pilot")


if __name__ == "__main__":
    main()
