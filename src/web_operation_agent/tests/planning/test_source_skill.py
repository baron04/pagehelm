from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path

from web_operation_agent.tests.route_fixture import FIXTURE_ROUTE_PORT
from web_operation_agent.schemas import EvidenceRef, SourceMap, SourceRelation
from web_operation_agent.planning.source_skill import (SourceEvidenceProvider as _SourceEvidenceProvider,
                                               SourceSkillBootstrapper as _SourceSkillBootstrapper,
                                               humanize)


def SourceSkillBootstrapper(source_map):
    return _SourceSkillBootstrapper(source_map, route_port=FIXTURE_ROUTE_PORT)


def SourceEvidenceProvider(source_map, source_root=None):
    return _SourceEvidenceProvider(source_map, source_root, route_port=FIXTURE_ROUTE_PORT)


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
        self.assertEqual(skill["route_template"], "/accounting/do/FindDue")
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
        self.assertEqual(draft.skill["route_template"], "/accounting/do/FindDue")
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


# ---------------------------------------------------------------- Business Skill drafts


from web_operation_agent.business.runtime import BusinessSkill                      # noqa: E402
from web_operation_agent.planning.source_skill import BusinessSkillCompiler          # noqa: E402
from web_operation_agent.tests.source_fixture import fixture_source_map     # noqa: E402


def _business_skill(path: Path, provenance: list[str], **overrides) -> BusinessSkill:
    import json
    artifact = {
        "schema_version": 1, "skill_id": "fixture.trays-query",
        "title": "Seed trays", "intent": "Read the seed trays.",
        "triggers": ["trays"], "instructions": ["Call query_trays."],
        "tool_names": ["query_trays"], "provenance": provenance, "read_only": True,
    }
    artifact.update(overrides)
    path.write_text(json.dumps(artifact, ensure_ascii=False) + "\n")
    return BusinessSkill.load(path)


class DraftingFromSource(unittest.TestCase):
    """What a compiler may write for a person, and what it must leave to them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map = fixture_source_map(self.root)
        self.compiler = BusinessSkillCompiler(
            self.map, application="fixture", route_port=FIXTURE_ROUTE_PORT)

    def tearDown(self):
        self._tmp.cleanup()

    def _draft(self, subject="glasshouse/findTrays", **overrides):
        arguments = {"skill_id": "fixture.trays-query", "subject": subject,
                     "tool_names": ("query_trays",), "read_only": True}
        arguments.update(overrides)
        return self.compiler.draft(**arguments)

    def test_the_draft_is_a_loadable_skill_artifact(self):
        """A draft nobody can load is a document, not a starting point."""
        import json
        draft = self._draft()
        path = self.root / "draft.json"
        path.write_text(json.dumps(draft.skill) + "\n")
        skill = BusinessSkill.load(path)
        self.assertEqual(skill.skill_id, "fixture.trays-query")
        self.assertEqual(skill.tool_names, ("query_trays",))

    def test_triggers_and_route_come_from_the_application_descriptors(self):
        draft = self._draft()
        self.assertIn("trays", draft.skill["triggers"])
        self.assertIn("seed", draft.skill["triggers"],
                      "the menu label is a trigger source a person would have missed")
        self.assertIn("its collection route is /glasshouse/do/findTrays", draft.facts)

    def test_the_port_decides_whether_a_subject_is_an_address(self):
        """A declared request with no address of its own must not acquire a fake one.

        Every compiler records a subject for requests that are only reachable from inside
        the application -- Odoo's `action-<id>` is the case this exists for. Only the
        application's own tool-route contract can tell those apart, so the compiler asks it
        rather than deciding with a pattern of its own.
        """
        from web_operation_agent.composition.ports import DeclaredRoutePort
        single_segment_only = DeclaredRoutePort(
            application_id="fixture.app",
            tool_route_patterns=(r"/[A-Za-z0-9_]+/do/[A-Za-z0-9_]+",),
            session_handoff_query_keys=frozenset(),
            contract_route_pattern=r"/(?P<scope>[A-Za-z0-9_]+)/do/(?P<route>[^?;#]+)",
            contract_route_template="/{scope}/do/{route}")
        strict = BusinessSkillCompiler(self.map, application="fixture",
                                       route_port=single_segment_only)
        draft = strict.draft(skill_id="fixture.lamp", subject="beacon/LampSchedule/Edit",
                             tool_names=("query_trays",), read_only=True)
        self.assertNotIn("route", " ".join(draft.facts))
        self.assertIn("the request declares no route; a Skill that opens a page needs "
                      "one supplied by hand", draft.review)
        # The same subject under the ordinary grammar *is* an address.
        self.assertIn("its collection route is /beacon/do/LampSchedule/Edit",
                      self._draft(subject="beacon/LampSchedule/Edit").facts)

    def test_instructions_are_marked_as_a_draft_not_presented_as_a_playbook(self):
        draft = self._draft()
        self.assertTrue(draft.skill["instructions"][0].startswith("DRAFT:"))
        self.assertIn("instructions: a compiler cannot write the playbook; this draft only "
                      "lists what the source declared", draft.review)

    def test_a_request_the_map_does_not_declare_is_refused(self):
        with self.assertRaisesRegex(ValueError, "declared request"):
            self._draft(subject="glasshouse/noSuchRequest")

    def test_every_citation_it_writes_verifies_against_the_map_it_came_from(self):
        """The generator and the checker must agree, or one of them is wrong."""
        import json
        draft = self._draft()
        path = self.root / "roundtrip.json"
        path.write_text(json.dumps(draft.skill) + "\n")
        verification = self.compiler.verify(BusinessSkill.load(path))
        self.assertTrue(verification.passed,
                        [check.reason for check in verification.checks
                         if not check.resolved])


class VerifyingCitations(unittest.TestCase):
    """Citation rot is invisible until something resolves the citation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map = fixture_source_map(self.root)
        self.compiler = BusinessSkillCompiler(
            self.map, application="fixture", route_port=FIXTURE_ROUTE_PORT)

    def tearDown(self):
        self._tmp.cleanup()

    def _verify(self, provenance):
        return self.compiler.verify(
            _business_skill(self.root / "skill.json", provenance))

    def test_a_cited_path_and_anchor_resolve(self):
        report = self._verify(
            ["src:fixture@v0:apps/greenhouse/glasshouse.decl#glasshouse/findTrays"])
        self.assertTrue(report.passed)

    def test_a_path_no_relation_cites_does_not_resolve(self):
        report = self._verify(["src:fixture@v0:apps/greenhouse/moved.decl#findTrays"])
        self.assertFalse(report.passed)
        self.assertIn("no relation in this map cites that path",
                      report.checks[0].reason)

    def test_an_anchor_the_file_no_longer_declares_does_not_resolve(self):
        report = self._verify(
            ["src:fixture@v0:apps/greenhouse/glasshouse.decl#glasshouse/renamedRequest"])
        self.assertFalse(report.passed)
        self.assertIn("nothing in it declares", report.checks[0].reason)

    def test_a_citation_for_another_application_does_not_resolve(self):
        report = self._verify(["src:other@v0:apps/greenhouse/glasshouse.decl#x"])
        self.assertFalse(report.passed)
        self.assertIn("cites other, not fixture", report.checks[0].reason)

    def test_a_citation_for_another_release_does_not_resolve(self):
        report = self._verify(["src:fixture@v9:apps/greenhouse/glasshouse.decl#x"])
        self.assertFalse(report.passed)
        self.assertIn("is not a build of", report.checks[0].reason)

    def test_a_release_line_matches_any_build_of_it(self):
        """A Skill is written for 18.0; the map records the image it was checked against."""
        from web_operation_agent.schemas import SourceMap
        built = SourceMap(schema_version=self.map.schema_version,
                          attestation_id=self.map.attestation_id,
                          repository_id=self.map.repository_id,
                          release="v0-20260911+image:sha256:abc",
                          relations=self.map.relations)
        compiler = BusinessSkillCompiler(built, application="fixture",
                                         route_port=FIXTURE_ROUTE_PORT)
        report = compiler.verify(_business_skill(
            self.root / "skill.json",
            ["src:fixture@v0:apps/greenhouse/glasshouse.decl#glasshouse/findTrays"]))
        self.assertTrue(report.passed)

    def test_an_interface_citation_is_carried_as_unchecked_not_as_a_pass(self):
        report = self._verify(["api:fixture@v0:/rpc#call"])
        self.assertTrue(report.passed, "an api citation must not fail a source gate")
        self.assertEqual(report.source_citations, ())
        self.assertIn("no source map can confirm it", report.checks[0].reason)


class ReissuingASkill(unittest.TestCase):
    """Recompiling an artifact's facts while keeping what a person wrote.

    The friction this removes is specific and was measured: a Skill's citation rots -- the
    application renames a descriptor, or moves a file out of the compiled tree -- and before
    this there was no path that recomputed the compiled facts without also throwing away the
    instructions, so a one-line repair meant hand-resealing the whole artifact.

    The rule underneath every test here is the one `BusinessSkillDraft` already states:
    descriptors are facts a compiler may recompute; a playbook is judgement it must not
    touch. `instructions` crossing over byte for byte is the load-bearing assertion.
    """

    #: A rename, in the shape the application actually produces one: the cited file is still
    #: read, and the identifier inside it is gone.
    ROTTED = "src:fixture@v0:apps/greenhouse/glasshouse.decl#glasshouse/renamedRequest"
    INTACT = "src:fixture@v0:apps/greenhouse/glasshouse.decl#glasshouse/findTrays"
    INTERFACE = "api:fixture@v0:/rpc#call"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.map = fixture_source_map(self.root)
        self.compiler = BusinessSkillCompiler(
            self.map, application="fixture", route_port=FIXTURE_ROUTE_PORT)

    def tearDown(self):
        self._tmp.cleanup()

    def _skill(self, provenance=None, **overrides) -> BusinessSkill:
        return _business_skill(self.root / "skill.json",
                               provenance if provenance is not None
                               else [self.ROTTED, self.INTERFACE],
                               **overrides)

    def _reissue(self, skill=None, **overrides):
        arguments = {"subject": "glasshouse/findTrays"}
        arguments.update(overrides)
        return self.compiler.reissue(skill or self._skill(), **arguments)

    def test_a_rotted_citation_is_red_then_green_and_the_playbook_never_moves(self):
        """The whole point, end to end, in the order a person would do it.

        `verify` is the entry: it names the citation that no longer resolves. The reissue
        recomputes it from the same map the check used. `verify` then passes, and the
        instructions are the same bytes they were before any of it.
        """
        skill = self._skill(instructions=[
            "Call query_trays and trust its current result.",
            "Never answer from this Skill; the tool result is the runtime truth.",
            "查询育苗盘时只用 query_trays，不要照抄本 Skill 里的内容。",
        ])
        before = self.compiler.verify(skill)
        self.assertFalse(before.passed)
        self.assertIn("nothing in it declares", before.source_citations[0].reason)

        reissue = self._reissue(skill)
        path = self.root / "reissued.json"
        path.write_text(json.dumps(reissue.skill, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        after = self.compiler.verify(BusinessSkill.load(path))
        self.assertTrue(after.passed,
                        [check.reason for check in after.source_citations
                         if not check.resolved])
        self.assertEqual(BusinessSkill.load(path).instructions, skill.instructions)
        self.assertEqual(
            json.dumps(reissue.skill["instructions"], ensure_ascii=False),
            json.dumps(list(skill.instructions), ensure_ascii=False),
            "the instructions must survive byte for byte, non-ASCII included")

    def test_it_produces_a_diff_rather_than_a_silent_new_artifact(self):
        reissue = self._reissue()
        self.assertFalse(reissue.unchanged)
        self.assertEqual({change.field for change in reissue.changed},
                         {"title", "triggers", "provenance"})
        provenance = next(change for change in reissue.changed
                          if change.field == "provenance")
        self.assertIn(self.ROTTED, provenance.before)
        self.assertNotIn(self.ROTTED, provenance.after)
        # And the diff is readable without a JSON viewer, because that is what gets read.
        rendered = reissue.render()
        self.assertIn("provenance:", rendered)
        self.assertIn("kept verbatim: ", rendered)

    def test_everything_a_person_owns_crosses_over_unchanged(self):
        skill = self._skill(title="Seed trays, current", intent="Read the current trays.",
                            triggers=["trays", "育苗盘"], read_only=True)
        reissue = self._reissue(skill, recompute=("provenance",))
        for field, expected in (("skill_id", skill.skill_id), ("title", skill.title),
                                ("intent", skill.intent),
                                ("triggers", list(skill.triggers)),
                                ("instructions", list(skill.instructions)),
                                ("tool_names", list(skill.tool_names)),
                                ("read_only", skill.read_only)):
            self.assertEqual(reissue.skill[field], expected, field)
        self.assertEqual(reissue.recomputed, ("provenance",))
        self.assertEqual([change.field for change in reissue.changed], ["provenance"])

    def test_a_citation_the_source_cannot_speak_for_is_carried_not_reinvented(self):
        """`api:` names a published interface. A compiler can neither check nor write one.

        Dropping it would quietly delete the only provenance a typed tool has; emitting one
        would be a claim no map supports. So it crosses over verbatim, after the recomputed
        source citations.
        """
        reissue = self._reissue()
        self.assertEqual(reissue.skill["provenance"][-1], self.INTERFACE)
        self.assertEqual(
            [entry for entry in reissue.skill["provenance"]
             if entry.startswith("api:")], [self.INTERFACE])

    def test_the_result_is_always_a_loadable_artifact(self):
        reissue = self._reissue()
        path = self.root / "loadable.json"
        path.write_text(json.dumps(reissue.skill, ensure_ascii=False) + "\n")
        self.assertEqual(BusinessSkill.load(path).skill_id, "fixture.trays-query")

    def test_reissuing_an_intact_skill_changes_nothing_it_did_not_have_to(self):
        """Nothing to repair means nothing to review; the diff says so rather than churning."""
        first = self._reissue(self._skill([self.INTACT, self.INTERFACE]),
                              recompute=("provenance",))
        settled = _business_skill(self.root / "settled.json",
                                  list(first.skill["provenance"]))
        self.assertTrue(self._reissue(settled, recompute=("provenance",)).unchanged)

    def test_a_field_no_compiler_can_derive_is_refused_rather_than_guessed(self):
        for field in ("instructions", "intent", "tool_names", "read_only"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "cannot recompute"):
                    self._reissue(recompute=(field,))

    def test_a_subject_this_release_does_not_declare_fails_loudly(self):
        """When the application removes the request, a person decides what the Skill is now.

        Producing a confident artifact about some other request is the one outcome worse
        than stopping, and it is what deriving the subject from the rotted citations would
        eventually do.
        """
        with self.assertRaisesRegex(ValueError, "declared request"):
            self._reissue(subject="glasshouse/noSuchRequest")
