from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from web_operation_agent_ofbiz.route import OFBIZ_ROUTE_PORT
from web_operation_agent.schemas import EvidenceRef, SourceMap, SourceRelation
from web_operation_agent.source_skill import (SourceEvidenceProvider as _SourceEvidenceProvider,
                                               SourceSkillBootstrapper as _SourceSkillBootstrapper,
                                               humanize)


def SourceSkillBootstrapper(source_map):
    return _SourceSkillBootstrapper(source_map, route_port=OFBIZ_ROUTE_PORT)


def SourceEvidenceProvider(source_map, source_root=None):
    return _SourceEvidenceProvider(source_map, source_root, route_port=OFBIZ_ROUTE_PORT)


def _row(kind, subject, obj, line, **attrs):
    return SourceRelation(kind=kind, subject=subject, object=obj, attrs=attrs,
                          evidence=(EvidenceRef("repo", "v1", "f.xml", line, line,
                                                f"{line:064x}", f"line {line}"),))


def _map(event_type="view-only", extra_forms=0):
    rows = [
        _row("request_handler", "accounting/FindDue", "", 1, event_type=event_type),
        _row("request_response", "accounting/FindDue", "FindDue", 2,
             response_name="success", response_type="view"),
        _row("view_screen", "accounting/FindDue", "PaymentScreens.xml#FindDue", 3),
        _row("screen_includes_form", "PaymentScreens.xml#FindDue",
             "PaymentForms.xml#FindDue", 4),
        _row("form_field", "PaymentForms.xml#FindDue", "organizationPartyId", 5,
             widget="drop-down"),
        _row("form_field", "PaymentForms.xml#FindDue", "daysOffset", 6,
             widget="text"),
        _row("form_field", "PaymentForms.xml#FindDue", "invoiceTypeId", 7,
             widget="hidden"),
        _row("form_field", "PaymentForms.xml#FindDue", "searchButton", 8,
             widget="submit"),
    ]
    if extra_forms:
        rows.append(_row("screen_includes_form", "PaymentScreens.xml#FindDue",
                         "OtherForms.xml#Other", 9))
    return SourceMap(schema_version=1, attestation_id="attest", repository_id="repo",
                     release="v1", relations=rows)


class SourceSkillBootstrapperTests(unittest.TestCase):
    def test_humanizes_camel_case_and_preserves_id(self):
        self.assertEqual(humanize("organizationPartyId"), "Organization Party ID")
        self.assertEqual(humanize("FindSalesInvoicesByDueDate"),
                         "Find Sales Invoices By Due Date")

    def test_compiles_cited_read_only_route_and_visible_verification_fields(self):
        draft = SourceSkillBootstrapper(_map()).compile("accounting", "FindDue")
        skill = draft.skill
        self.assertEqual(skill["route_template"], "/accounting/control/FindDue")
        self.assertTrue(skill["read_only"])
        self.assertEqual(skill["expected_text"], ["Organization Party ID", "Days Offset"])
        self.assertNotIn("Invoice Type ID", skill["expected_text"])
        self.assertNotIn("Search Button", skill["expected_text"])
        self.assertEqual(len(skill["provenance"]), 8)
        self.assertEqual(draft.relation_counts["visible_form_field"], 2)

    def test_search_can_find_unlinked_view_only_request_from_business_words(self):
        bootstrap = SourceSkillBootstrapper(_map())
        found = bootstrap.search("review customer invoices by days past due", "accounting")
        self.assertEqual(found[0]["request"], "FindDue")
        self.assertIn("due", found[0]["matched_terms"])
        candidates, draft = bootstrap.bootstrap(
            "review customer invoices by days past due", "accounting")
        self.assertEqual(candidates[0]["request"], "FindDue")
        self.assertEqual(draft.skill["route_template"], "/accounting/control/FindDue")
        self.assertEqual(bootstrap.search("unrelated warehouse", "accounting"), ())

    def test_bootstrap_refuses_no_match_or_tied_top_candidates(self):
        bootstrap = SourceSkillBootstrapper(_map())
        with self.assertRaisesRegex(ValueError, "no matching"):
            bootstrap.bootstrap("unrelated warehouse", "accounting")
        source_map = _map()
        source_map.relations.append(
            _row("request_handler", "accounting/DueReview", "", 10,
                 event_type="view-only"))
        with self.assertRaisesRegex(ValueError, "tied top"):
            SourceSkillBootstrapper(source_map).bootstrap("due", "accounting")

    def test_refuses_write_request_or_ambiguous_form_chain(self):
        with self.assertRaisesRegex(ValueError, "not declared view-only"):
            SourceSkillBootstrapper(_map("service")).compile("accounting", "FindDue")
        with self.assertRaisesRegex(ValueError, "manual selection"):
            SourceSkillBootstrapper(_map(extra_forms=1)).compile("accounting", "FindDue")
        with self.assertRaisesRegex(ValueError, "limit"):
            SourceSkillBootstrapper(_map()).search("due", "accounting", 0)

    def test_evidence_provider_reads_only_exact_compiled_citations(self):
        provider = SourceEvidenceProvider(_map())
        found = provider.search_source("days due", "accounting")
        citation = found[0]["provenance"][0]
        read = provider.read_source_slice(citation)
        self.assertEqual(read["citation"], citation)
        self.assertEqual(read["excerpt"], "line 1")
        self.assertFalse(read["truncated"])
        with self.assertRaisesRegex(ValueError, "not in"):
            provider.read_source_slice("../secret.env:1-2")

    def test_slice_reader_verifies_file_bytes_against_compiled_hash(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.xml"
            path.write_text("first\nsecond\n")
            body = "first\n"
            relation = SourceRelation(
                "request_handler", "accounting/Main", "", {"event_type": "view-only"},
                (EvidenceRef("repo", "v1", "f.xml", 1, 1,
                             hashlib.sha256(body.encode()).hexdigest()),))
            source_map = SourceMap(1, "attest", "repo", "v1", [relation])
            provider = SourceEvidenceProvider(source_map, tmp)
            self.assertEqual(provider.read_source_slice("f.xml:1-1")["excerpt"], body)
            path.write_text("changed\nsecond\n")
            with self.assertRaisesRegex(ValueError, "version-bound hash"):
                provider.read_source_slice("f.xml:1-1")


if __name__ == "__main__":
    unittest.main()
