from __future__ import annotations

import copy
import unittest

from evals.twenty.breadth import (evaluate_breadth, make_breadth_state,
                                  validate_breadth_state)


FIXTURE = {"container": "twenty-db", "database": "default", "db_user": "postgres",
           "workspace_schema": "workspace_test"}
COMPANY_ID = "7e172546-a20e-4c7a-861c-a6478a944d37"
PERSON_ID = "7a93d1e5-3f74-4945-8a65-d7f996083f72"
OPPORTUNITY_ID = "2beb07b0-340c-41d7-be33-5aa91757f329"


def projection():
    return {
        "companies": [{"id": COMPANY_ID, "name": "Acme", "domain": "acme.example",
                       "created_at": "2026-09-01", "updated_at": "2026-09-01",
                       "deleted_at": None}],
        "people": [{"id": PERSON_ID, "name": "Ivan Zhao", "email": "zhao@example.com",
                    "company_id": COMPANY_ID, "created_at": "2026-09-01",
                    "updated_at": "2026-09-01", "deleted_at": None}],
        "opportunities": [{"id": OPPORTUNITY_ID, "name": "API Integration Deal",
                           "stage": "SCREENING", "amount_micros": "75000000000",
                           "currency": "USD", "company_id": COMPANY_ID,
                           "created_at": "2026-09-01", "updated_at": "2026-09-01",
                           "deleted_at": None}],
    }


def states():
    before = make_breadth_state(
        fixture=FIXTURE, projection=projection(), captured_at="2026-09-10T00:00:00Z",
        role="baseline")
    after = make_breadth_state(
        fixture=FIXTURE, projection=projection(), captured_at="2026-09-10T00:01:00Z",
        role="observation")
    return before, after


class BreadthEvaluatorTests(unittest.TestCase):
    def test_enumeration_requires_the_exact_complete_current_projection(self):
        before, after = states()
        task = {"artifact_version": 2, "kind": "enumerate", "entity": "people"}
        evidence = {"artifact_version": 2, "execution_status": "completed",
                    "complete": True,
                    "records": [{"name": "Ivan Zhao", "detail": "zhao@example.com"}]}
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "PASS")
        omitted = {**evidence, "records": []}
        result = evaluate_breadth(task=task, before=before, after=after, evidence=omitted)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["reported_exact_records"])

    def test_filter_is_computed_from_the_baseline_instead_of_model_claims(self):
        before, after = states()
        task = {"artifact_version": 2, "kind": "filter", "entity": "opportunities",
                "name_contains": "integration"}
        evidence = {"artifact_version": 2, "execution_status": "completed",
                    "complete": True,
                    "records": [{"name": "API Integration Deal", "detail": "SCREENING"}]}
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "PASS")

    def test_record_open_checks_typed_route_visible_identity_and_no_business_change(self):
        before, after = states()
        task = {"artifact_version": 2, "kind": "open_record", "entity": "opportunities",
                "record_id": OPPORTUNITY_ID}
        evidence = {"artifact_version": 2, "execution_status": "completed",
                    "page": {"path": f"/object/opportunity/{OPPORTUNITY_ID}",
                             "visible_projection": "API Integration Deal\nSCREENING"}}
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "PASS")
        evidence["page"]["path"] = f"/object/company/{COMPANY_ID}"
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "FAIL")

    def test_collection_open_requires_exact_entity_route(self):
        before, after = states()
        task = {"artifact_version": 2, "kind": "open_collection", "entity": "people"}
        evidence = {"artifact_version": 2, "execution_status": "completed",
                    "page": {"path": "/objects/people"}}
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "PASS")

    def test_unsupported_write_requires_explicit_zero_dispatch_refusal(self):
        before, after = states()
        task = {"artifact_version": 2, "kind": "refuse", "entity": "people",
                "operation": "delete"}
        evidence = {"artifact_version": 2, "execution_status": "failed",
                    "refusal_code": "unsupported_operation", "dispatch_count": 0}
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "PASS")
        evidence["dispatch_count"] = 1
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "FAIL")

    def test_changed_state_and_malformed_or_deleted_rows_fail_closed(self):
        before, _after = states()
        changed = projection()
        changed["people"][0]["email"] = "changed@example.com"
        after = make_breadth_state(
            fixture=FIXTURE, projection=changed, captured_at="2026-09-10T00:01:00Z",
            role="observation")
        task = {"artifact_version": 2, "kind": "open_collection", "entity": "people"}
        evidence = {"artifact_version": 2, "execution_status": "completed",
                    "page": {"path": "/objects/people"}}
        self.assertEqual(evaluate_breadth(
            task=task, before=before, after=after, evidence=evidence)["status"], "FAIL")

        invalid = copy.deepcopy(before)
        invalid["projection"]["people"][0]["id"] = "not-a-uuid"
        invalid["digest"] = "sha256:" + "0" * 64
        self.assertEqual(evaluate_breadth(
            task=task, before=invalid, after=after, evidence=evidence)["status"], "INVALID")

        deleted = projection()
        deleted["people"][0].update(name="", email="", deleted_at="2026-09-09")
        state = make_breadth_state(
            fixture=FIXTURE, projection=deleted, captured_at="2026-09-10T00:00:00Z",
            role="baseline")
        self.assertEqual(len(validate_breadth_state(state, role="baseline")[
            "projection"]["people"]), 1)


if __name__ == "__main__":
    unittest.main()
