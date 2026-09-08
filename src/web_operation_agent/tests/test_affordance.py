"""Typed source affordances are conservative, deterministic and source-bound."""
import unittest

from web_operation_agent.affordance import resolve_source_affordances
from web_operation_agent.schemas import EvidenceRef, SourceMap, SourceRelation


def ref(line: int) -> EvidenceRef:
    return EvidenceRef("fixture/public", "v1", "applications/demo/widget/Public.xml",
                       line, line, f"{line:064x}", f"public line {line}")


def relation(kind: str, subject: str, obj: str, line: int, **attrs) -> SourceRelation:
    return SourceRelation(kind, subject, obj, attrs, (ref(line),))


def fixture(attestation: str = "deployment-a") -> SourceMap:
    return SourceMap(1, attestation, "fixture/public", "v1", [
        relation("menu_link", "marketing:LeadMenu:clone", "CloneLead", 1,
                 title="Clone Lead", title_resolved=True, webapps=["sfa"],
                 parameters=[{"name": "partyId", "from_field": "parameters.partyId",
                              "value": ""}]),
        # A second declaration of the same route must enrich, rather than duplicate, it.
        relation("menu_link", "marketing:LeadActions:clone", "CloneLead", 2,
                 title="Clone Lead", title_resolved=True, webapps=["sfa"],
                 parameters=[{"name": "partyGroupId", "from_field": "",
                              "value": "${parameters.partyGroupId}"},
                             {"name": "partyId", "from_field": "parameters.partyId",
                              "value": ""}]),
        relation("request_handler", "sfa/CloneLead", "", 3, webapp="sfa",
                 event_type="view-only"),
        relation("request_response", "sfa/CloneLead", "CloneLead", 4, webapp="sfa",
                 response_name="success", response_type="view"),
        relation("view_screen", "sfa/CloneLead", "apps/sfa/LeadScreens.xml#CloneLead", 5,
                 webapp="sfa"),
        relation("screen_includes_form", "apps/sfa/LeadScreens.xml#CloneLead",
                 "apps/sfa/LeadForms.xml#CreateLead", 6, form_name="CreateLead"),

        relation("menu_link", "party:Profile:identifications", "viewidentifications", 11,
                 title="Identifications", title_resolved=True, webapps=["partymgr"],
                 parameters=[{"name": "partyId", "from_field": "parameters.partyId",
                              "value": ""}]),
        relation("request_handler", "partymgr/viewidentifications", "", 12,
                 webapp="partymgr", event_type="view-only"),
        relation("request_response", "partymgr/viewidentifications", "viewidentifications",
                 13, webapp="partymgr", response_name="success", response_type="view"),
        relation("view_screen", "partymgr/viewidentifications",
                 "apps/party/PartyScreens.xml#ListPartyIdentifications", 14,
                 webapp="partymgr"),
        relation("screen_includes_form",
                 "apps/party/PartyScreens.xml#ListPartyIdentifications",
                 "apps/party/PartyForms.xml#ListPartyIdentifications", 15,
                 form_name="ListPartyIdentifications"),

        # Lexically attractive, but it dispatches a service and is never an affordance.
        relation("menu_link", "marketing:LeadMenu:delete", "deleteLead", 20,
                 title="Delete Lead", title_resolved=True, webapps=["sfa"], parameters=[]),
        relation("request_handler", "sfa/deleteLead", "deleteLead", 21, webapp="sfa",
                 event_type="service"),

        # Generic action words must not outrank the business noun in
        # ``viewidentifications`` for natural-language task phrasing.
        relation("menu_link", "party:Generic:view", "view", 40,
                 title="View", title_resolved=True, webapps=["partymgr"], parameters=[]),
        relation("request_handler", "partymgr/view", "", 41, webapp="partymgr",
                 event_type="view-only"),
    ])


class TypedSourceAffordances(unittest.TestCase):
    def test_near_meaning_tasks_recall_the_exact_sfa_and_party_candidates(self):
        smap = fixture()
        sfa = resolve_source_affordances(
            smap, task="Duplicate this lead record", query="copy the selected lead",
            page_text="Lead profile", served_webapps={"sfa", "partymgr"})
        self.assertEqual(sfa[0].request, "CloneLead")
        self.assertIn("copy", sfa[0].matched_terms)

        party = resolve_source_affordances(
            smap, task="Inspect the customer's identity records", query="party identifiers",
            page_text="Profile", served_webapps={"sfa", "partymgr"})
        self.assertEqual(party[0].request, "viewidentifications")
        self.assertIn("identity", party[0].matched_terms)

    def test_query_free_r031_party_wording_is_not_crowded_out_by_generic_handlers(self):
        candidates = resolve_source_affordances(
            fixture(),
            task=("View identifications for party DemoCustomer and stop when the page is "
                  "visible. This is view-only: do not add, change, or submit anything."),
            page_text="", served_webapps={"sfa", "partymgr"})
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].request, "viewidentifications")
        self.assertNotIn("view", [candidate.request for candidate in candidates])

    def test_write_handlers_and_conflicting_handler_declarations_are_filtered(self):
        smap = fixture()
        found = resolve_source_affordances(
            smap, task="Delete or clone the lead", served_webapps={"sfa"})
        self.assertTrue(found)
        self.assertEqual({candidate.request for candidate in found}, {"CloneLead"})

        smap.relations.append(relation("request_handler", "sfa/CloneLead", "cloneLead", 22,
                                       webapp="sfa", event_type="service"))
        self.assertEqual(resolve_source_affordances(
            smap, task="Clone the lead", served_webapps={"sfa"}), ())

    def test_same_route_with_different_menu_schemas_stays_as_separate_candidates(self):
        candidates = resolve_source_affordances(
            fixture(), task="Clone Lead", served_webapps={"sfa"})
        self.assertEqual(len(candidates), 2)
        self.assertEqual({candidate.route_template for candidate in candidates},
                         {"/sfa/control/CloneLead"})
        schemas = [[p.to_json() for p in candidate.parameters]
                   for candidate in candidates]
        self.assertIn([
            {"name": "partyId", "from_field": "parameters.partyId", "value": ""},
        ], schemas)
        self.assertIn([
            {"name": "partyGroupId", "from_field": "",
             "value": "${parameters.partyGroupId}"},
            {"name": "partyId", "from_field": "parameters.partyId", "value": ""},
        ], schemas)
        self.assertEqual(len({candidate.candidate_id for candidate in candidates}), 2)
        for candidate in candidates:
            self.assertEqual(candidate.view, "CloneLead")
            self.assertEqual(candidate.screen, "apps/sfa/LeadScreens.xml#CloneLead")
            self.assertEqual(candidate.forms, ("apps/sfa/LeadForms.xml#CreateLead",))
            self.assertIn(ref(3).key, candidate.citations)
            self.assertIn(ref(6).key, candidate.citations)
            menu_lines = {ref(1).key, ref(2).key} & set(candidate.citations)
            self.assertEqual(len(menu_lines), 1,
                             "one candidate must not merge distinct menu contexts")
            self.assertEqual(set(candidate.to_json()), {
                "candidate_id", "source_map_digest", "title", "webapp", "request",
                "route_template", "parameters", "view", "screen", "forms", "citations",
                "rank_score", "matched_terms",
            })

    def test_ambiguous_success_views_are_not_presented_as_one_arbitrary_chain(self):
        smap = fixture()
        smap.relations.append(relation(
            "request_response", "sfa/CloneLead", "OtherLeadView", 30, webapp="sfa",
            response_name="success", response_type="view"))
        candidates = resolve_source_affordances(
            smap, task="Clone Lead", served_webapps={"sfa"})
        self.assertTrue(candidates)
        self.assertTrue(all(candidate.view == "" and candidate.screen == ""
                            and candidate.forms == () for candidate in candidates))

    def test_candidate_id_changes_with_the_source_map_digest(self):
        one = resolve_source_affordances(
            fixture("deployment-a"), task="Clone Lead", served_webapps={"sfa"})[0]
        two = resolve_source_affordances(
            fixture("deployment-b"), task="Clone Lead", served_webapps={"sfa"})[0]
        self.assertNotEqual(one.source_map_digest, two.source_map_digest)
        self.assertNotEqual(one.candidate_id, two.candidate_id)

    def test_served_webapp_allowlist_is_enforced(self):
        candidates = resolve_source_affordances(
            fixture(), task="Inspect party identity", query="Clone Lead",
            served_webapps={"partymgr"})
        self.assertEqual([candidate.webapp for candidate in candidates], ["partymgr"])

    def test_page_text_alone_cannot_trigger_a_candidate(self):
        self.assertEqual(resolve_source_affordances(
            fixture(), task="Reconcile quarterly revenue", query="",
            page_text="Clone Lead Identifications Clone Lead",
            served_webapps={"sfa", "partymgr"}), ())

    def test_page_text_only_breaks_a_tie_after_public_intent_matches(self):
        without_page = resolve_source_affordances(
            fixture(), task="Review the record", query="lead identity",
            served_webapps={"sfa", "partymgr"})
        with_page = resolve_source_affordances(
            fixture(), task="Review the record", query="lead identity",
            page_text="Identifications", served_webapps={"sfa", "partymgr"})
        self.assertEqual({c.request for c in without_page},
                         {"CloneLead", "viewidentifications"})
        self.assertEqual(with_page[0].request, "viewidentifications")


if __name__ == "__main__":
    unittest.main()
