import time
import unittest
from types import SimpleNamespace

from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent_odoo.demo.qualify import (CASES, approved_write_ran,
                                                    generic_read_probe)


class ApprovedWriteRanTests(unittest.TestCase):
    """The post-run fixture expectation follows the cases that ran, not the case table."""

    def test_read_only_run_expects_an_unchanged_fixture(self):
        executed = [{"case": name} for name, case in CASES.items() if not case.get("writes")]
        self.assertTrue(executed)
        self.assertFalse(approved_write_ran(executed))

    def test_unapproved_write_case_expects_an_unchanged_fixture(self):
        refusals = [{"case": name} for name, case in CASES.items()
                    if case.get("writes") and not case.get("approve")]
        self.assertTrue(refusals)
        self.assertFalse(approved_write_ran(refusals))

    def test_an_executed_approved_write_expects_a_changed_fixture(self):
        writes = [{"case": name} for name, case in CASES.items() if case.get("approve")]
        self.assertTrue(writes)
        self.assertTrue(approved_write_ran(writes[:1]))


class _ProbeRuntime:
    def __init__(self):
        self.calls = []

    @staticmethod
    def load(skill_id, allowed_ids):
        return (object(), "") if skill_id in allowed_ids else (None, "not selected")

    def execute(self, name, args, loaded_ids, *, context):
        self.calls.append((name, args, loaded_ids, context))
        common = {"audit": {"identity_binding": "principal_bound"},
                  "data_classes": (), "evidence": (), "query_answer": None}
        if name == "discover_odoo_models":
            return SimpleNamespace(content={"candidates": [{
                "candidate_id": "m_" + "1" * 32, "label": "Journal Item",
            }]}, **common)
        if name == "describe_odoo_model":
            return SimpleNamespace(content={"fields": [
                {"field_id": "f_" + "2" * 32, "label": "Debit"},
                {"field_id": "f_" + "3" * 32, "label": "Credit"},
            ]}, **common)
        if name == "query_odoo_records":
            evidence = tuple(SimpleNamespace(evidence_id=value) for value in (
                "query#1", "record#1", "record#2", "record#3", "record#4"))
            return SimpleNamespace(
                content={
                    "source_total": 3,
                    "completeness": {
                        "returned_count": 2, "matched_count": 3,
                        "matching_count_exact": True, "result_truncated": True,
                    },
                },
                audit=common["audit"], evidence=evidence, query_answer=object(),
                data_classes=("business.generic_record", "business.identifier"),
            )
        raise AssertionError(name)


class _ProbeAdapter:
    def __init__(self):
        self.business_tools = _ProbeRuntime()

    @staticmethod
    def bind_browser_session(_path, *, delegation_id, scopes):
        now = int(time.time() * 1_000)
        return PrincipalContext(
            schema_version=1, application_id="odoo.reference",
            tenant_id="db", workspace_id="company", actor_id="reader",
            delegation_id=delegation_id, issued_at=now - 1_000,
            expires_at=now + 60_000, scopes=scopes,
        )


class GenericReadProbeTests(unittest.TestCase):
    def test_probe_uses_scoped_runtime_and_persists_no_record_values(self):
        adapter = _ProbeAdapter()
        result = generic_read_probe(adapter, "unused.json")
        self.assertTrue(result["passed"])
        self.assertEqual(result["returned_count"], 2)
        self.assertEqual(result["record_evidence_count"], 4)
        self.assertEqual(
            [name for name, _args, _loaded, _context in adapter.business_tools.calls],
            ["discover_odoo_models", "describe_odoo_model", "query_odoo_records"],
        )
        for _name, _args, loaded, context in adapter.business_tools.calls:
            self.assertEqual(loaded, {"odoo.runtime.records-query"})
            self.assertEqual(context.principal.scopes, ("generic_records:read",))
        self.assertNotIn("records", result)


if __name__ == "__main__":
    unittest.main()
