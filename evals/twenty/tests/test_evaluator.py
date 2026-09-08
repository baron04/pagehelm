from __future__ import annotations

import copy
import unittest

from evals.twenty.evaluator import (digest_json, evaluate, make_state,
                                    make_unavailable_state)


FIXTURE = {
    "container": "twenty-test-db-1",
    "database": "default",
    "db_user": "postgres",
    "workspace_schema": "workspace_test",
}
COMPANY_ID = "7e172546-a20e-4c7a-861c-a6478a944d37"
NEW_ID = "94630e86-022c-4b88-bc4e-e65d42fd51db"


def company(company_id: str = COMPANY_ID, name: str = "Anthropic",
            domain: str = "anthropic.com") -> dict:
    return {
        "id": company_id,
        "name": name,
        "domain": domain,
        "created_at": "2026-09-08T01:00:00+00:00",
        "updated_at": "2026-09-08T01:00:00+00:00",
        "deleted_at": None,
    }


def guards(*, people: list | None = None, opportunities: list | None = None) -> dict:
    people = [] if people is None else people
    opportunities = [] if opportunities is None else opportunities
    return {
        "people": {"row_count": len(people), "digest": digest_json(people)},
        "opportunities": {
            "row_count": len(opportunities), "digest": digest_json(opportunities),
        },
    }


def state(role: str, companies: list[dict] | None = None, *,
          state_guards: dict | None = None, fixture: dict | None = None) -> dict:
    return make_state(
        fixture=FIXTURE if fixture is None else fixture,
        companies=[company()] if companies is None else companies,
        guards=guards() if state_guards is None else state_guards,
        captured_at="2026-09-08T02:00:00+00:00",
        role=role,
    )


class TwentyEvaluationTests(unittest.TestCase):
    def test_query_passes_only_for_complete_truth_without_state_change(self):
        before = state("baseline")
        after = state("observation")
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"},
            before=before,
            after=after,
            evidence={
                "artifact_version": 1,
                "execution_status": "completed",
                "complete": True,
                "companies": [{"name": "Anthropic", "domain": "https://ANTHROPIC.com/"}],
            },
        )
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(result["checks"].values()))

        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=before, after=after,
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": True, "companies": []},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["reported_all_current_companies"])

    def test_query_detects_a_forbidden_business_change(self):
        before = state("baseline")
        after = state("observation", [company(name="Changed")])
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=before, after=after,
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": True,
                      "companies": [{"name": "Changed", "domain": "anthropic.com"}]},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["business_state_unchanged"])

    def test_candidate_failure_is_fail_and_incomplete_answer_is_not_invalid(self):
        before = state("baseline")
        after = state("observation")
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=before, after=after,
            evidence={"artifact_version": 1, "execution_status": "failed"},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["execution_completed"])

        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=before, after=after,
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": False, "companies": []},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["response_marked_complete"])

    def test_open_checks_database_target_route_page_and_no_mutation(self):
        result = evaluate(
            task={"artifact_version": 1, "kind": "open", "company_id": COMPANY_ID},
            before=state("baseline"), after=state("observation"),
            evidence={
                "artifact_version": 1,
                "execution_status": "completed",
                "page": {
                    "path": f"/object/company/{COMPANY_ID}",
                    "name": "Anthropic",
                    "domain": "anthropic.com",
                },
            },
        )
        self.assertEqual(result["status"], "PASS")

        result = evaluate(
            task={"artifact_version": 1, "kind": "open", "company_id": COMPANY_ID},
            before=state("baseline"), after=state("observation"),
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "page": {"path": "/object/company/other", "name": "Anthropic",
                               "domain": "anthropic.com"}},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["final_route_matches_target"])

    def test_create_checks_exact_delta_receipt_page_and_forbidden_entities(self):
        created = company(NEW_ID, "NovaForge Demo", "novaforge-demo.example")
        result = evaluate(
            task={"artifact_version": 1, "kind": "create", "name": "NovaForge Demo",
                  "domain": "novaforge-demo.example", "require_open": True},
            before=state("baseline"), after=state("observation", [company(), created]),
            evidence={
                "artifact_version": 1,
                "execution_status": "completed",
                "write_receipt": {"dispatch_count": 1, "acknowledged": True,
                                  "same_actor_readback": True},
                "page": {"path": f"/object/company/{NEW_ID}", "name": "NovaForge Demo",
                         "domain": "novaforge-demo.example"},
            },
        )
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(result["checks"].values()))

        changed_guards = guards(people=[{"id": "unexpected"}])
        result = evaluate(
            task={"artifact_version": 1, "kind": "create", "name": "NovaForge Demo",
                  "domain": "novaforge-demo.example", "require_open": False},
            before=state("baseline"),
            after=state("observation", [company(), created], state_guards=changed_guards),
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "write_receipt": {"dispatch_count": 1, "acknowledged": True,
                                        "same_actor_readback": True}},
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["people_unchanged"])

    def test_uncertain_execution_stays_indeterminate_even_when_delta_looks_correct(self):
        created = company(NEW_ID, "NovaForge Demo", "novaforge-demo.example")
        result = evaluate(
            task={"artifact_version": 1, "kind": "create", "name": "NovaForge Demo",
                  "domain": "novaforge-demo.example", "require_open": True},
            before=state("baseline"), after=state("observation", [company(), created]),
            evidence={"artifact_version": 1, "execution_status": "indeterminate"},
        )
        self.assertEqual(result["status"], "INDETERMINATE")
        self.assertFalse(result["checks"]["execution_certain"])
        self.assertTrue(result["checks"]["created_company_matches_request"])

    def test_unavailable_after_state_is_indeterminate(self):
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=state("baseline"),
            after=make_unavailable_state(
                fixture=FIXTURE, captured_at="2026-09-08T02:00:00+00:00",
                role="observation"),
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": True, "companies": []},
        )
        self.assertEqual(result["status"], "INDETERMINATE")

    def test_invalid_artifacts_and_fixture_mismatch_are_invalid(self):
        tampered = state("baseline")
        tampered["projection"]["companies"][0]["name"] = "Tampered"
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=tampered,
            after=state("observation"),
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": True, "companies": []},
        )
        self.assertEqual(result["status"], "INVALID")

        other = {**FIXTURE, "workspace_schema": "workspace_other"}
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"}, before=state("baseline"),
            after=state("observation", fixture=other),
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": True, "companies": []},
        )
        self.assertEqual(result["status"], "INVALID")

    def test_company_projection_accepts_a_missing_optional_domain(self):
        no_domain = company(domain="")
        result = evaluate(
            task={"artifact_version": 1, "kind": "query"},
            before=state("baseline", [no_domain]), after=state("observation", [no_domain]),
            evidence={"artifact_version": 1, "execution_status": "completed",
                      "complete": True, "companies": [{"name": "Anthropic", "domain": ""}]},
        )
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
