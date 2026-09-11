"""The packaged Skills, and what a compiled release has to say about their citations."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from web_operation_agent.business import Citation
from web_operation_agent_odoo.demo.skills import (APPLICATION, compiler, packaged_skills,
                                                  verify_packaged_skills)


class PackagedSkills(unittest.TestCase):
    def test_every_shipped_skill_loads_from_the_package(self):
        skills = packaged_skills()
        self.assertEqual(
            sorted(skill.skill_id for skill in skills),
            ["odoo.sales.customers-create", "odoo.sales.customers-query",
             "odoo.sales.products-query", "odoo.sales.sale-orders-query"])

    def test_every_citation_names_this_application_and_a_release_line(self):
        """A build-specific citation would have to be reissued for every deployment image."""
        for skill in packaged_skills():
            for entry in skill.provenance:
                with self.subTest(entry=entry):
                    citation = Citation.parse(entry)
                    self.assertEqual(citation.application, APPLICATION)
                    self.assertEqual(citation.release, "18.0")

    def test_every_source_citation_uses_a_globally_qualified_odoo_identifier(self):
        """`action_orders` names four addons at once; `sale.action_orders` names one.

        Every shipped Skill cited the unqualified form until the source verifier resolved
        them for the first time, and none of those citations pointed anywhere.
        """
        for skill in packaged_skills():
            for entry in skill.provenance:
                citation = Citation.parse(entry)
                if citation.kind != "src" or not citation.anchor:
                    continue
                with self.subTest(entry=entry):
                    self.assertIn(".", citation.anchor,
                                  "an Odoo XML id or model name is module-qualified")


@unittest.skipUnless(os.environ.get("WOA_ODOO_SOURCE_ROOT")
                     and os.environ.get("WOA_ODOO_SOURCE_RELEASE"),
                     "needs a source tree matching the deployed release")
class CitationsAgainstARealRelease(unittest.TestCase):
    """Runs for real in the live gate; here it is whatever the developer has to hand.

    The authoritative run of this is `qualify.py`'s
    `every_skill_citation_resolves_in_this_release`, which cannot be skipped.
    """

    def setUp(self):
        self.root = os.environ["WOA_ODOO_SOURCE_ROOT"]
        self.release = os.environ["WOA_ODOO_SOURCE_RELEASE"]

    def test_every_packaged_citation_resolves(self):
        report = verify_packaged_skills(self.root, self.release)
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(report["status"], "READY")

    def test_a_renamed_anchor_stops_resolving(self):
        """Reverse verification: the checker must be able to fail."""
        from web_operation_agent.business import BusinessSkill
        import json, tempfile
        checker = compiler(self.root, self.release)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rotted.json"
            path.write_text(json.dumps({
                "schema_version": 1, "skill_id": "odoo.sales.sale-orders-query",
                "title": "x", "intent": "x", "triggers": ["orders"],
                "instructions": ["x"], "tool_names": ["query_sale_orders"],
                "provenance": ["src:odoo@18.0:addons/sale/views/sale_order_views.xml"
                               "#sale.action_orders_renamed_in_19"],
                "read_only": True,
            }) + "\n")
            report = checker.verify(BusinessSkill.load(path))
        self.assertFalse(report.passed)
        self.assertIn("nothing in it declares", report.checks[0].reason)


if __name__ == "__main__":
    unittest.main()
