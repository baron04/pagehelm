from __future__ import annotations

import json
import unittest

from evals.twenty.evidence import (
    EvidenceContractError, case_projection, disclosed_routes, failed_evidence,
    open_evidence, query_evidence, validate_execution_case,
)


COMPANIES = [
    {"id": "11111111-1111-4111-8111-111111111111",
     "name": "Synthetic One", "domain": "one.synthetic.example"},
    {"id": "22222222-2222-4222-8222-222222222222",
     "name": "Synthetic Two", "domain": "two.synthetic.example"},
]


def _results(companies, *, complete=True):
    return [{
        "companies": [{"name": item["name"], "domain": item["domain"],
                       "ui_path": f"/object/company/{item['id']}"} for item in companies],
        "completeness": {"source_complete": complete, "result_truncated": not complete},
    }]


def _answer(companies, *, complete=True):
    return json.dumps({
        "artifact_version": 1, "execution_status": "completed", "complete": complete,
        "companies": [{"name": item["name"], "domain": item["domain"]}
                      for item in companies],
    })


def _case(kind="query"):
    task = {"artifact_version": 1, "kind": kind}
    if kind == "open":
        task["company_id"] = COMPANIES[0]["id"]
    if kind == "create":
        task.update({"name": "X", "domain": "x.test", "require_open": False})
    return {
        "artifact_version": 1, "kind": "twenty_execution_case",
        "set_id": "twenty-development", "split": "development", "task_id": "dev-query-one",
        "template_family_digest": "sha256:" + "a" * 64,
        "task_digest": "sha256:" + "b" * 64,
        "prompt": "List every company and domain.", "evaluator_task": task,
    }


class SharedEvidenceContractTests(unittest.TestCase):
    def test_query_answer_is_used_as_written_and_must_restate_the_tool_results(self):
        evidence = query_evidence(_answer(COMPANIES), _results(COMPANIES))
        self.assertTrue(evidence["complete"])
        self.assertEqual(len(evidence["companies"]), 2)

        for answer in ("```json\n" + _answer(COMPANIES) + "\n```",
                       "Here they are: " + _answer(COMPANIES)):
            with self.subTest(answer=answer[:20]), self.assertRaisesRegex(
                    EvidenceContractError, "strict JSON"):
                query_evidence(answer, _results(COMPANIES))
        with self.assertRaisesRegex(EvidenceContractError, "restate"):
            query_evidence(_answer(COMPANIES[:1]), _results(COMPANIES))
        with self.assertRaisesRegex(EvidenceContractError, "completeness"):
            query_evidence(_answer(COMPANIES, complete=False), _results(COMPANIES))
        with self.assertRaisesRegex(EvidenceContractError, "no business result"):
            query_evidence(_answer(COMPANIES), [])

    def test_open_evidence_needs_disclosure_a_permit_and_visible_page_facts(self):
        target = COMPANIES[0]
        path = f"/object/company/{target['id']}"
        visible = f"{target['name']}\nDomain\n{target['domain']}"
        evidence = open_evidence(
            path=path, visible_projection=visible, business_results=_results(COMPANIES),
            permitted_routes=[path])
        self.assertEqual(evidence["page"]["path"], path)

        with self.assertRaisesRegex(EvidenceContractError, "route Host"):
            open_evidence(path=path, visible_projection=visible,
                          business_results=_results(COMPANIES), permitted_routes=[])
        with self.assertRaisesRegex(EvidenceContractError, "not visible"):
            open_evidence(path=path, visible_projection="Loading…",
                          business_results=_results(COMPANIES), permitted_routes=[path])
        with self.assertRaisesRegex(EvidenceContractError, "disclosed"):
            open_evidence(path="/object/company/absent", visible_projection=visible,
                          business_results=_results(COMPANIES),
                          permitted_routes=["/object/company/absent"])

    def test_execution_case_validation_and_projection_are_engine_neutral(self):
        selected = validate_execution_case(_case("query"))
        self.assertEqual(selected["task_kind"], "query")
        projection = case_projection(selected)
        self.assertEqual(set(projection), {
            "set_id", "split", "task_id", "task_digest", "template_family_digest",
            "evaluator_task"})
        self.assertEqual(projection["evaluator_task"], {"kind": "query"})
        self.assertNotIn("prompt", projection)

        with self.assertRaisesRegex(EvidenceContractError, "query and open only"):
            validate_execution_case(_case("create"))
        self.assertEqual(
            validate_execution_case(
                _case("create"), allowed_kinds=("query", "open", "create"))["task_kind"],
            "create")

    def test_disclosed_routes_and_failed_evidence_are_stable(self):
        self.assertEqual(
            disclosed_routes(_results(COMPANIES)),
            [f"/object/company/{item['id']}" for item in COMPANIES])
        self.assertEqual(failed_evidence(), {
            "artifact_version": 1, "execution_status": "failed"})


if __name__ == "__main__":
    unittest.main()
