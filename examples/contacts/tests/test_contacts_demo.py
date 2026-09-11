from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

from web_operation_agent.model import ProviderError, require_chat_port
from examples.contacts.app import (DEMO_API_TOKEN, DEMO_SESSION_COOKIE,
                                   DEMO_SESSION_VALUE, MinimalContactsApplication)
from examples.contacts.adapter import MinimalContactsAdapter
from examples.contacts.demo import DemoConfig, ScriptedDemoChat, run_demo
from web_operation_agent.business import (BusinessMutationIndeterminate,
                                          BusinessToolError)
from web_operation_agent.identity import PrincipalError
from web_operation_agent.mutation import MemoryMutationJournal
from web_operation_agent.tests.adapter_contract import assert_adapter_contract


def _chromium_launches() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


class MinimalDemoUnitTests(unittest.TestCase):
    def test_second_adapter_binds_and_reads_its_live_typed_api(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(application.origin)
            bound = assert_adapter_contract(self, adapter)
            self.assertTrue(adapter.health()["passed"])
            self.assertEqual(bound.descriptor.application_id, "minimal.contacts")
            self.assertEqual(
                [item.skill_id for item in bound.business_tools.relevant(
                    "Open Grace Hopper's contact")],
                ["minimal.contacts.query"],
            )
            result = bound.business_tools.execute(
                "list_contacts", {"name_contains": "Grace"},
                {"minimal.contacts.query"})
            self.assertEqual(result.content["matched_count"], 1)
            self.assertTrue(result.content["completeness"]["source_complete"])
            self.assertEqual(result.allowed_routes, ("/contacts/grace-hopper",))

    def test_application_serves_the_same_origin_controller(self):
        with MinimalContactsApplication() as application:
            with urllib.request.urlopen(
                    application.origin + "/woa/page-controller.js", timeout=2) as response:
                source = response.read().decode("utf-8")
            self.assertIn("export class PageController", source)
            self.assertIn("export function getElementByIndex", source)

    def test_scripted_apparatus_implements_the_chat_contract_and_cancels(self):
        chat = require_chat_port(ScriptedDemoChat(), role="minimal demo chat")
        reply = chat.complete([], [], "plain")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "load_skill")
        with self.assertRaises(ProviderError) as raised:
            chat.complete_cancellable([], [], "cancelled", cancelled=lambda: True)
        self.assertEqual(raised.exception.code, "provider_cancelled")


class MinimalDemoBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for minimal-demo qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_scripted_walkthrough_uses_the_real_gateway_and_opens_api_route(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(DemoConfig(
                scripted=True,
                headless=True,
                output_dir=Path(directory),
                keep_open_seconds=0,
                turn_timeout_s=30,
            ))
            self.assertEqual(summary["mode"], "scripted_apparatus")
            self.assertEqual(summary["model_usage"]["provider_calls"], 0)
            self.assertEqual(summary["model_usage"]["scripted_decisions"], 4)
            self.assertEqual(len(summary["turns"]), 1)
            self.assertEqual(summary["turns"][0]["status"], "finished")
            self.assertEqual(summary["turns"][0]["terminal_reason"], "agent_finished")
            self.assertGreaterEqual(summary["turns"][0]["page_actions"], 1)
            self.assertTrue(summary["last_url"].endswith("/contacts/grace-hopper"))
            self.assertIn("Grace Hopper", summary["last_page_text"])
            self.assertIn("grace@example.test", summary["last_page_text"])
            audit = next(Path(directory).glob("*/audit.jsonl"))
            events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
            self.assertIn("business_skill_loaded", events)
            self.assertIn("business_tool_result", events)
            self.assertIn("action_receipt", events)


if __name__ == "__main__":
    unittest.main()


class TheWriteWalkthroughInARealBrowser(unittest.TestCase):
    """The whole chain as a person sees it, through the panel's own approval control.

    This is the one test that cannot be faked into passing: the approval has to come from
    the page session holding the lease, so a composition that found some other way to
    approve would be qualifying a product this repository does not ship.
    """

    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for minimal-demo qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_one_approval_creates_one_contact_and_opens_its_record(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(DemoConfig(
                scripted=True, headless=True, allow_mutations=True,
                output_dir=Path(directory), keep_open_seconds=0, turn_timeout_s=45,
            ))
            turn = summary["turns"][0]
            self.assertEqual(turn["status"], "finished")
            self.assertEqual(summary["model_usage"]["provider_calls"], 0)
            self.assertTrue(summary["last_url"].endswith("/contacts/radia-perlman"))
            # The record page shows the contact that did not exist when the page loaded,
            # which is the only evidence that the write reached the application.
            self.assertIn("Radia Perlman", summary["last_page_text"])
            self.assertIn("radia@example.test", summary["last_page_text"])
            audit = next(Path(directory).glob("*/audit.jsonl"))
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            kinds = [row["event"] for row in events]
            self.assertEqual(kinds.count("business_mutation_proposed"), 1,
                             "one approval, asked for exactly once")
            self.assertEqual(kinds.count("business_mutation_result"), 1)
            self.assertNotIn("business_mutation_indeterminate", kinds)
            written = next(row for row in events
                           if row["event"] == "business_mutation_result")
            self.assertTrue(written["audit"]["readback_verified"])
            # The proposal is in the trail; the values the user approved are not, beyond
            # what the redacted projection carries.
            proposed = next(row for row in events
                            if row["event"] == "business_mutation_proposed")
            self.assertEqual(proposed["effect"], "Create exactly one contact")
            self.assertEqual(sorted(proposed["proposal"]["argument_names"]),
                             ["email", "name"])
            self.assertNotIn("radia@example.test", json.dumps(proposed["proposal"]))


class TheWriteChainAnAuthorCopies(unittest.TestCase):
    """All five ports on a template a person is meant to read, then imitate.

    An example with only a read left every author to invent the hard half for themselves.
    Each test here is one step of the chain and the specific failure it exists to prevent.
    """

    def _composed(self, application, *, allow_mutations=True):
        adapter = MinimalContactsAdapter(
            application.origin, api_token=DEMO_API_TOKEN,
            allow_mutations=allow_mutations,
            mutation_journal=MemoryMutationJournal() if allow_mutations else None)
        return adapter, assert_adapter_contract(self, adapter)

    def _principal(self, adapter):
        page = adapter.identity.page_identity(
            f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}")
        return adapter.ports.identity.bind(page, delegation_id="contacts-test")

    def test_all_five_ports_are_the_applications_own(self):
        """A stock implementation in a port is a decision; an author should see which."""
        with MinimalContactsApplication() as application:
            adapter, bound = self._composed(application)
            ports = bound.ports
            self.assertTrue(ports.identity.required)
            self.assertEqual(ports.routes.application_id, "minimal.contacts")
            self.assertEqual(ports.postconditions.supported_tools, {"create_contact"})
            self.assertEqual(ports.reconciliation.supported_tools, {"create_contact"})
            self.assertIn("create_contact", ports.data.tool_names)

    def test_the_two_credentials_have_to_name_the_same_actor(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            principal = self._principal(adapter)
            self.assertEqual(principal.actor_id, "ada-lovelace")
            self.assertEqual(sorted(principal.scopes),
                             ["contacts:read", "contacts:write"])

    def test_a_page_assertion_from_somewhere_else_is_refused(self):
        """The whole point of binding: neither credential may authorise a write alone."""
        from web_operation_agent.identity import (IdentityAssertion,
                                                  PRINCIPAL_CONTEXT_VERSION)
        import time
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            now = int(time.time() * 1000)
            somebody_else = IdentityAssertion(
                schema_version=PRINCIPAL_CONTEXT_VERSION, source="page_session",
                application_id="minimal.contacts", tenant_id="synthetic",
                workspace_id="northstar", actor_id="grace-hopper",
                issued_at=now, expires_at=now + 60_000)
            with self.assertRaisesRegex(PrincipalError, "different principals"):
                adapter.ports.identity.bind(somebody_else, delegation_id="contacts-test")

    def test_a_read_only_composition_never_offers_the_write(self):
        with MinimalContactsApplication() as application:
            adapter, bound = self._composed(application, allow_mutations=False)
            self.assertEqual(sorted(bound.business_tools.tool_names), ["list_contacts"])
            self.assertEqual(bound.business_tools.mutation_tool_names, frozenset())
            self.assertEqual(bound.ports.postconditions.supported_tools, frozenset())
            self.assertEqual(bound.ports.reconciliation.supported_tools, frozenset())

    def test_the_proposal_states_exactly_what_the_user_is_approving(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            spec = executor.prepare_mutation(
                "create_contact", {"name": "  Radia   Perlman ",
                                   "email": " Radia@Example.TEST "})
            self.assertEqual(spec.args, {"name": "Radia Perlman",
                                         "email": "radia@example.test"})
            self.assertEqual(spec.targets,
                             ("Name: Radia Perlman", "Email: radia@example.test"))
            self.assertEqual(len(application.contacts), 3, "a proposal writes nothing")

    def test_a_duplicate_is_refused_before_the_user_is_asked(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            with self.assertRaisesRegex(BusinessToolError, "already exists"):
                executor.prepare_mutation(
                    "create_contact", {"name": "grace  HOPPER",
                                       "email": "other@example.test"})
            self.assertEqual(len(application.contacts), 3)

    def test_the_write_is_read_back_under_the_same_actor(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            result = executor.execute_mutation(
                "create_contact", {"name": "Radia Perlman",
                                   "email": "radia@example.test"})
            self.assertTrue(result.content["readback_verified"])
            self.assertEqual(result.content["contact"]["id"], "radia-perlman")
            self.assertEqual(result.allowed_routes, ("/contacts/radia-perlman",))
            self.assertEqual(len(application.contacts), 4)
            verified = adapter.ports.postconditions.verify(
                "create_contact", {"name": "Radia Perlman",
                                   "email": "radia@example.test"}, result)
            self.assertTrue(verified.verified)

    def test_a_readback_that_disagrees_is_indeterminate_not_a_failure(self):
        """A failure invites a retry, and something is already there."""
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            executor = adapter.business_tools._executor
            original = executor._existing

            def drifted(name):
                rows = original(name)
                return [{**row, "email": "somebody-else@example.test"} for row in rows]

            executor._existing = lambda name: (
                [] if not application.contacts or len(application.contacts) == 3
                else drifted(name))
            with self.assertRaises(BusinessMutationIndeterminate) as raised:
                executor.execute_mutation(
                    "create_contact", {"name": "Radia Perlman",
                                       "email": "radia@example.test"})
            self.assertEqual(raised.exception.phase, "readback")

    def test_reconciliation_reports_evidence_and_never_a_verdict(self):
        with MinimalContactsApplication() as application:
            adapter, _bound = self._composed(application)
            principal = self._principal(adapter)
            outcome = adapter.ports.reconciliation.reconcile(
                "create_contact", {"name": "Grace Hopper", "email": "grace@example.test"},
                evidence={}, principal=principal)
            self.assertEqual(outcome.outcome, "unknown")
            self.assertTrue(outcome.requires_human)
            self.assertIn("1 contacts currently carry the name", outcome.reason)

    def test_the_application_refuses_a_write_carrying_only_the_browser_session(self):
        """A page that could write with its own cookie is one CSRF from an unasked write."""
        import urllib.error
        with MinimalContactsApplication() as application:
            request = urllib.request.Request(
                application.origin + "/api/contacts",
                json.dumps({"name": "Mallory", "email": "m@example.test"}).encode(),
                {"Cookie": f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}",
                 "Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(len(application.contacts), 3)
