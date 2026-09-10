from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.integrations.ofbiz_knowledge import AppSkill, KnowledgeRouter


VALID = {
    "schema_version": 1,
    "skill_id": "ofbiz.accounting.invoices-by-due-date",
    "title": "Invoices by due date",
    "intent": "Open the report options.",
    "triggers": ["invoices", "past due"],
    "current_webapp": "accounting",
    "route_template": "/accounting/control/FindSalesInvoicesByDueDate",
    "parameters": [],
    "expected_text": ["Days Offset"],
    "provenance": ["controller.xml#FindSalesInvoicesByDueDate"],
    "read_only": True,
}


class KnowledgeRouterTests(unittest.TestCase):
    def _skill(self, update=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            data = dict(VALID)
            data.update(update or {})
            path.write_text(json.dumps(data))
            return AppSkill.load(path)

    def test_matches_task_but_keeps_route_and_provenance_out_of_public_card(self):
        skill = self._skill()
        router = KnowledgeRouter((skill,))
        selected = router.relevant("Review customer invoices that are past due")
        self.assertEqual([row.skill_id for row in selected], [skill.skill_id])
        card = selected[0].public_card()
        self.assertNotIn("route_template", card)
        self.assertNotIn("provenance", card)
        self.assertNotIn("artifact_path", card)

    def test_opens_only_after_agent_reaches_declared_webapp(self):
        skill = self._skill()
        router = KnowledgeRouter((skill,))
        allowed = {skill.skill_id}
        url, error, _ = router.resolve(skill.skill_id, {},
                                       "http://app/partymgr/control/main", allowed)
        self.assertEqual(url, "")
        self.assertIn("accounting", error)
        url, error, resolved = router.resolve(
            skill.skill_id, {}, "http://app/accounting/control/main", allowed)
        self.assertEqual(error, "")
        self.assertEqual(url, "/accounting/control/FindSalesInvoicesByDueDate")
        self.assertEqual(resolved, skill)

    def test_rejects_unoffered_skill_and_extra_bindings(self):
        skill = self._skill()
        router = KnowledgeRouter((skill,))
        self.assertIn("not supplied", router.resolve(
            skill.skill_id, {}, "http://app/accounting/control/main", set())[1])
        self.assertIn("exactly", router.resolve(
            skill.skill_id, {"surprise": "1"},
            "http://app/accounting/control/main", {skill.skill_id})[1])

    def test_app_entry_returns_only_opaque_ref_for_exact_same_origin_link(self):
        from types import SimpleNamespace
        skill = self._skill()
        router = KnowledgeRouter((skill,))
        observation = SimpleNamespace(
            url="http://app/partymgr/control/main",
            elements=[SimpleNamespace(
                ref="e9", role="link", tag="a",
                attrs={"href": "/accounting/control/main?externalLoginKey=[redacted]"})])
        ref, error, resolved = router.app_link(
            skill.skill_id, observation, {skill.skill_id})
        self.assertEqual((ref, error), ("e9", ""))
        self.assertEqual(resolved, skill)

        observation.elements[0].attrs["href"] = "https://evil.test/accounting/control/main"
        self.assertIn("exactly one", router.app_link(
            skill.skill_id, observation, {skill.skill_id})[1])

    def test_gap_is_measured_on_supplied_fresh_application_observation(self):
        from types import SimpleNamespace
        skill = self._skill()
        observation = SimpleNamespace(
            url="http://app/accounting/control/main?externalLoginKey=[redacted]",
            elements=[
                SimpleNamespace(ref="e1", role="link", tag="a",
                                attrs={"href": "/accounting/control/findInvoices"}),
                SimpleNamespace(ref="e2", role="button", tag="button", attrs={}),
            ])
        gap = KnowledgeRouter.gap_evidence(skill, observation)
        self.assertTrue(gap["webapp_match"])
        self.assertTrue(gap["target_visible_link_absent"])
        self.assertEqual(gap["visible_link_count"], 1)

        observation.elements.append(SimpleNamespace(
            ref="e3", role="link", tag="a", attrs={"href": skill.route_template}))
        self.assertEqual(KnowledgeRouter.gap_evidence(
            skill, observation)["target_visible_link_refs"], ["e3"])

    def test_rejects_cross_origin_and_write_capable_skill_shapes(self):
        for update in (
            {"route_template": "https://evil.test/accounting/control/report"},
            {"route_template": "/accounting/control/../createInvoice"},
            {"read_only": False},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self._skill(update)


if __name__ == "__main__":
    unittest.main()
