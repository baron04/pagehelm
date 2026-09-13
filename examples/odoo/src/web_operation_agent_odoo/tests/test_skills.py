"""The packaged Skills, and what a compiled release has to say about their citations."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from web_operation_agent.business import Citation
from web_operation_agent_odoo.demo.skills import (APPLICATION, compiler, packaged_skills,
                                                  reissue_packaged_skills,
                                                  skill_artifact_path,
                                                  verify_packaged_skills)
from web_operation_agent_odoo.skill_sources import SKILL_SOURCES


class PackagedSkills(unittest.TestCase):
    def test_every_shipped_skill_loads_from_the_package(self):
        skills = packaged_skills()
        self.assertEqual(
            sorted(skill.skill_id for skill in skills),
            ["odoo.sales.customer-invoices-query", "odoo.sales.customers-create",
             "odoo.sales.customers-draft", "odoo.sales.customers-query",
             "odoo.sales.products-query",
             "odoo.sales.quotations-query", "odoo.sales.sale-orders-confirm",
             "odoo.sales.sale-orders-invoice", "odoo.sales.sale-orders-query"])

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


class DraftingSourcesStayWithTheSkills(unittest.TestCase):
    """Every shipped Skill has to say what it was compiled from, and nothing else may.

    A Skill artifact records the facts it was drafted with, not the request they were about,
    and the one time that matters is when a citation rots -- which is exactly when reading
    the subject back out of the citations would mean trusting the broken thing. So it is
    declared alongside the Skills, and drift between the two sets is a defect: a Skill with
    no entry cannot be repaired, and an entry with no Skill is a name nobody maintains.
    """

    def test_the_two_sets_are_the_same_set(self):
        self.assertEqual(sorted(SKILL_SOURCES),
                         sorted(skill.skill_id for skill in packaged_skills()))

    def test_every_declared_subject_is_this_application_s_source_scope(self):
        from web_operation_agent_odoo.route import ODOO_SOURCE_SCOPE

        for skill_id, (subject, _methods) in sorted(SKILL_SOURCES.items()):
            with self.subTest(skill_id=skill_id):
                scope, separator, route = subject.partition("/")
                self.assertEqual(scope, ODOO_SOURCE_SCOPE)
                self.assertTrue(separator and route, subject)

    def test_every_skill_id_resolves_to_exactly_one_packaged_artifact(self):
        for skill_id in sorted(SKILL_SOURCES):
            with self.subTest(skill_id=skill_id):
                self.assertTrue(skill_artifact_path(skill_id).is_file())


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

    def test_nothing_is_reissued_while_every_citation_resolves(self):
        """`verify` is the entry point, so a green release must produce an empty diff."""
        report = reissue_packaged_skills(self.root, self.release, accept=("provenance",))
        self.assertEqual(report["reissued"], [])

    def test_a_named_skill_is_reissued_with_its_playbook_untouched(self):
        """The repair itself, against the real release, on a Skill nothing is wrong with.

        Selecting by name rather than by rot is what lets this run on a green tree; what it
        checks is the invariant that holds either way -- the recomputed citations resolve,
        and every field a person owns is the same object it was.
        """
        report = reissue_packaged_skills(
            self.root, self.release, accept=("provenance",),
            only=("odoo.sales.sale-orders-query",))
        reissue = report["reissues"][0]
        original = next(skill for skill in packaged_skills()
                        if skill.skill_id == "odoo.sales.sale-orders-query")
        self.assertEqual(reissue.skill["instructions"], list(original.instructions))
        self.assertEqual(reissue.skill["title"], original.title)
        self.assertEqual(reissue.skill["triggers"], list(original.triggers))
        self.assertEqual([change.field for change in reissue.changed], ["provenance"])

        from web_operation_agent.business import BusinessSkill
        import json
        verified = compiler(self.root, self.release).verify(
            BusinessSkill.from_bytes(
                json.dumps(reissue.skill, ensure_ascii=False).encode(),
                artifact_uri="memory://reissued"))
        self.assertTrue(verified.passed,
                        [check.reason for check in verified.source_citations
                         if not check.resolved])

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
