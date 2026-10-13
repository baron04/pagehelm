"""Installed Markdown workflows and their source citations."""
import os
import unittest
from types import SimpleNamespace

from web_operation_agent.business.runtime import Citation
from web_operation_agent_odoo.demo.skills import (
    APPLICATION, compiler, packaged_skills, verify_packaged_skills)
from web_operation_agent_odoo.skill_packages import packaged_skills as installed_skills


class PackagedSkills(unittest.TestCase):
    def test_every_shipped_workflow_loads_with_its_source_resource(self):
        registry = installed_skills()
        self.assertEqual({skill.skill_id for skill in registry.artifacts}, {
            "odoo.catalog-and-records", "odoo.customer-maintenance",
            "odoo.order-billing", "odoo.sales-processing"})
        for skill in registry.artifacts:
            with self.subTest(skill=skill.skill_id):
                self.assertTrue(skill.body.strip())
                self.assertTrue(skill.read_resource("references/source-provenance.json"))
        self.assertEqual({s.skill_id for s in packaged_skills()},
                         {s.skill_id for s in registry.artifacts})

    def test_source_citations_name_the_application_release_and_qualified_anchor(self):
        for skill in packaged_skills():
            self.assertTrue(skill.provenance)
            for entry in skill.provenance:
                with self.subTest(entry=entry):
                    citation = Citation.parse(entry)
                    self.assertEqual(citation.application, APPLICATION)
                    self.assertEqual(citation.release, "18.0")
                    if citation.kind == "src" and citation.anchor:
                        self.assertIn(".", citation.anchor)


@unittest.skipUnless(os.environ.get("WOA_ODOO_SOURCE_ROOT")
                     and os.environ.get("WOA_ODOO_SOURCE_RELEASE"),
                     "needs a source tree matching the deployed release")
class CitationsAgainstARealRelease(unittest.TestCase):
    def test_every_packaged_citation_resolves(self):
        report = verify_packaged_skills(os.environ["WOA_ODOO_SOURCE_ROOT"],
                                        os.environ["WOA_ODOO_SOURCE_RELEASE"])
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(report["status"], "READY")

    def test_a_renamed_anchor_stops_resolving(self):
        checker = compiler(os.environ["WOA_ODOO_SOURCE_ROOT"],
                           os.environ["WOA_ODOO_SOURCE_RELEASE"])
        report = checker.verify(SimpleNamespace(skill_id="odoo.sales-processing",
            provenance=("src:odoo@18.0:addons/sale/views/sale_order_views.xml"
                        "#sale.action_orders_renamed_in_19",)))
        self.assertFalse(report.passed)
        self.assertIn("nothing in it declares", report.checks[0].reason)
