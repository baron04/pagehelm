"""The goal turn also runs over a REST application with actor-scoped data."""
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import (ACTORS_BY_ID, DEMO_SESSION_COOKIE,
                                   MinimalContactsApplication)
from testapps.contacts.goal import ContactsGoalPort
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.state.identity import OperationContext, PrincipalError
from web_operation_agent.schemas.browser import ElementRef
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.business import _Chat


class ContactsGoalPortTests(unittest.TestCase):
    def test_current_entityless_help_page_needs_no_record_read_or_repository(self):
        with MinimalContactsApplication() as application:
            adapter, principal, context = self._bound(application, "ada-lovelace")
            port = ContactsGoalPort(adapter)
            page = port.index(context=context).page("help")
            self.assertEqual("", page.entity)
            self.assertFalse(page.views)
            spec = GoalSpec.from_json({"deliverables": [{"kind": "page", "page": {"key": "help"}}]})
            with patch.object(port._executor, "_contact_rows", side_effect=AssertionError("not a data page")):
                result = port.resolve(spec, context=context)
                found = adapter.business_tools._executor.execute("find_page_entries", {"query": "Help"}, context=context)
            self.assertEqual("/contacts/help", result.deliverables[0].page.url)
            self.assertEqual((), result.reads)
            self.assertEqual(("/contacts/help",), found.allowed_routes)
            self.assertTrue(found.content["catalog_complete"])
            # This route is a real application page under the same page-session actor.
            request = urllib.request.Request(application.origin + "/contacts/help",
                headers={"Cookie": f"{DEMO_SESSION_COOKIE}={ACTORS_BY_ID[principal.actor_id].session}"})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertIn("Workspace help", response.read().decode())

    def test_entityless_help_refuses_every_data_or_form_request_without_reads(self):
        with MinimalContactsApplication() as application:
            adapter, _principal, context = self._bound(application, "ada-lovelace")
            port = ContactsGoalPort(adapter, form_prefill=True)
            for declaration in ({"kind": "fact", "fields": ["Email"]},
                    {"kind": "aggregate", "entity": "contacts", "metric": {"op": "count"}},
                    {"kind": "list", "entity": "contacts"},
                    {"kind": "page", "record": "Ada Lovelace"},
                    {"kind": "page", "page": {"filters_on": ["Mine"]}},
                    {"kind": "page", "rules": ["unverified-rule"]},
                    {"kind": "handoff", "action": "create"},
                    {"kind": "handoff", "field_values": [{"field": "Name", "value": "Synthetic"}]}):
                with self.subTest(declaration=declaration), patch.object(port._executor, "_contact_rows",
                        side_effect=AssertionError("not a data page")):
                    spec = GoalSpec.from_json({"deliverables": [{**declaration,
                        "page": {"key": "help", **declaration.get("page", {})}}]})
                    resolved = port.resolve(spec, context=context)
                    self.assertEqual("refused", resolved.deliverables[0].read_failure)
                    self.assertIn("navigation only", resolved.deliverables[0].problem)
                    self.assertIsNone(resolved.deliverables[0].page)
                    self.assertEqual((), resolved.reads)
            self.assertEqual(0, application.write_requests)

    def test_page_visibility_revocation_rechecks_current_actor_menu(self):
        with MinimalContactsApplication() as application:
            adapter, principal, context = self._bound(application, "ada-lovelace")
            port = ContactsGoalPort(adapter)
            before = port.index(context=context)
            application.hidden_pages[principal.actor_id] = {"help"}
            after = port.index(context=context)
            self.assertIsNone(after.page("help"))
            self.assertNotEqual(before.source_digest, after.source_digest)
            found = adapter.business_tools._executor.execute("find_page_entries", {"query": "Help"}, context=context)
            self.assertEqual((), found.allowed_routes)
            self.assertEqual([], found.content["candidates"])
            denied = port.resolve(GoalSpec.from_json({"deliverables": [
                {"kind": "page", "page": {"key": "help"}}]}), context=context)
            self.assertIsNone(denied.deliverables[0].page)
            other, _other_principal, other_context = self._bound(application, "rosalind-franklin")
            self.assertIsNotNone(ContactsGoalPort(other).index(context=other_context).page("help"))
            request = urllib.request.Request(application.origin + "/contacts/help",
                headers={"Cookie": f"{DEMO_SESSION_COOKIE}={ACTORS_BY_ID[principal.actor_id].session}"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(403, error.exception.code)

    def test_actor_menu_metadata_cannot_add_an_unreviewed_route(self):
        with MinimalContactsApplication() as application, patch("testapps.contacts.app.PAGE_MENUS", (
                {"key": "unknown", "label": "Unreviewed page", "path": "/contacts/unreviewed"},)):
            adapter, _principal, context = self._bound(application, "ada-lovelace")
            found = adapter.business_tools._executor.execute("find_page_entries", {"query": "Unreviewed page"}, context=context)
            self.assertEqual((), found.allowed_routes)
            self.assertFalse(found.content["candidates"][0]["openable"])
            self.assertEqual("unreviewed_route", found.content["candidates"][0]["unavailable_reason"])
            self.assertEqual((), ContactsGoalPort(adapter).index(context=context).pages)

    def test_staged_goal_keeps_direct_help_page_choice_with_no_entity(self):
        with MinimalContactsApplication() as application, tempfile.TemporaryDirectory() as tmp:
            adapter, principal, _context = self._bound(application, "ada-lovelace")
            chat = _Chat([("declare_scope", {"parts": [{"kind": "page", "page": "help"}]}),
                          ("declare_goal", {"deliverables": [{"kind": "page", "page": {"key": "help"}}]})])
            class Browser(_RemoteBrowser):
                def __init__(self, channel, **kwargs):
                    super().__init__(channel, **kwargs)
                    self.url = channel.latest_observation_envelope.observation.url

                def observe(self):
                    return self.ready(super().observe())

                @staticmethod
                def ready(observation):
                    observation.render_state = "ready"
                    observation.page_route = "/contacts/help" if observation.url.endswith("/contacts/help") else ""
                    observation.elements = [ElementRef(ref="back", role="link", name="All contacts",
                                                        tag="a", epoch=observation.epoch)]
                    return observation

                def execute(self, action, args=None, expect_navigation=False):
                    receipt = super().execute(action, args, expect_navigation=expect_navigation)
                    if action == "goto":
                        self.url = application.origin + args["url"]
                        receipt.url_after = self.url
                        receipt.observation.url = self.url
                    self.ready(receipt.observation)
                    return receipt
            executor = GoalTurnExecutor(
                channel=_Channel(page_url=application.origin + "/contacts", principal=principal),
                config=AgentTurnConfig(supported_scope="read", require_identity=True,
                    ledger_path=Path(tmp) / "audit.jsonl"), chat=chat,
                application=bind_application(adapter), goal_port=ContactsGoalPort(adapter),
                control=ReadOnlyControl(), readings=1, run_id_factory=lambda _m: "help-navigation")
            with patch.object(goal_executor, "RemoteBrowserClient", Browser):
                result = executor.execute({"message_id": "help", "text": "Open Help",
                                           "page_url": application.origin + "/contacts"})
            self.assertEqual(AgentTurnStatus.FINISHED, result.status, result.user_answer)
            self.assertIn("Help", result.user_answer)
            self.assertEqual(2, len(chat.messages))
            detail = chat.messages[1][-1]["content"]
            self.assertIn("help: Help | navigation only (no records)", detail)
            self.assertNotIn("fields: Identifier", detail)
            self.assertEqual(0, application.write_requests)

    def test_rest_fact_and_repository_ownership_share_one_goal_turn(self):
        from web_operation_agent.tests.goal.test_repository_answers import Lookup, source_reply, scope
        with MinimalContactsApplication() as application, tempfile.TemporaryDirectory() as tmp:
            adapter, principal, _ = self._bound(application, "ada-lovelace")
            chat = _Chat([
                scope([{"kind": "fact", "entity": "contacts", "about": "Grace Hopper"}]),
                ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "contacts",
                    "record": "Grace Hopper", "fields": ["Email"]}]}),
                ("declare_source_queries", {"queries_0": []}), source_reply()])
            executor = GoalTurnExecutor(
                channel=_Channel(page_url=application.origin + "/contacts", principal=principal),
                config=AgentTurnConfig(supported_scope="read", require_identity=True,
                    ledger_path=Path(tmp) / "audit.jsonl",
                    source=SourceCapability(runtime_source=True, lookup=Lookup())),
                chat=chat, application=bind_application(adapter), goal_port=ContactsGoalPort(adapter),
                control=ReadOnlyControl(), run_id_factory=lambda _m: "mixed-repository", readings=1,
                repository_answers=True)
            with patch.object(goal_executor, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "mixed", "page_url": application.origin + "/contacts",
                    "text": "What is Grace Hopper's email and who maintains the interface code?"})
            self.assertEqual(AgentTurnStatus.FINISHED, result.status)
            self.assertIn("grace@example.test", result.user_answer)
            self.assertIn("Aurora team", result.user_answer)
            self.assertIn("docs/owners.md:1-1@synthetic-1 sha256:", result.user_answer)
            self.assertEqual(4, len(chat.messages))

    def _bound(self, application, actor_id):
        actor = ACTORS_BY_ID[actor_id]
        adapter = MinimalContactsAdapter(application.origin, api_token=actor.token)
        page = adapter.identity.page_identity(
            f"{DEMO_SESSION_COOKIE}={actor.session}")
        principal = adapter.identity.bind(page, delegation_id="goal-test")
        context = OperationContext.create(
            principal, session_id="goal-session", turn_id="goal-turn",
            operation_id="goal-resolve", deadline_ms=int(time.time() * 1000) + 60_000)
        return adapter, principal, context

    def test_rest_facts_are_actor_scoped_and_a_staged_goal_turn_answers_them(self):
        with MinimalContactsApplication() as application, tempfile.TemporaryDirectory() as tmp:
            north, principal, context = self._bound(application, "ada-lovelace")
            port = ContactsGoalPort(north)
            index = port.index(context=context)
            self.assertEqual(index.entities[0].label, "Contacts")
            self.assertEqual(index.pages[0].key, "contacts")
            spec = GoalSpec.from_json({"deliverables": [
                {"kind": "fact", "entity": "contacts", "record": "Grace Hopper",
                 "fields": ["Email"]}]})
            result = port.resolve(spec, context=context)
            self.assertEqual(result.deliverables[0].facts[0].value, "grace@example.test")
            self.assertEqual(result.reads, (("GET", "/api/contacts"),))

            channel = _Channel(page_url=application.origin + "/contacts",
                               principal=principal)
            # The default staged path: the entity first, then the details.
            chat = _Chat([("declare_scope", {"parts": [{"kind": "fact", "entity": "contacts",
                                                        "about": "Grace Hopper"}]}),
                          ("declare_goal", spec.to_json())])
            executor = GoalTurnExecutor(
                channel=channel, config=AgentTurnConfig(
                    supported_scope="Read actor-visible contacts.",
                    ledger_path=Path(tmp) / "goal.jsonl", require_identity=True),
                chat=chat, application=bind_application(north), goal_port=port,
                control=ReadOnlyControl(), run_id_factory=lambda _m: "contacts-goal-run",
                readings=1)
            with patch.object(goal_executor, "RemoteBrowserClient", _RemoteBrowser):
                execution = executor.execute({
                    "message_id": "contacts-goal", "text": "Grace Hopper 的邮箱是什么？",
                    "page_url": application.origin + "/contacts"})
            self.assertEqual(execution.status, AgentTurnStatus.FINISHED)
            self.assertIn("grace@example.test", execution.user_answer)
            self.assertEqual(len(chat.messages), 2)
            self.assertEqual(execution.agent.page_actions, 0)

            south, _south_principal, south_context = self._bound(application, "mary-jackson")
            south_result = ContactsGoalPort(south).resolve(spec, context=south_context)
            self.assertEqual(south_result.deliverables[0].facts[0].value,
                             "grace.hopper@southwind.test")
            with self.assertRaises(PrincipalError):
                port.resolve(spec, context=south_context)

    def test_candidate_values_are_the_actors_own_contacts(self):
        with MinimalContactsApplication() as application:
            north, _principal, north_context = self._bound(application, "ada-lovelace")
            south, _south_principal, south_context = self._bound(application, "mary-jackson")
            port = ContactsGoalPort(north)
            self.assertIn("values", port.capabilities())
            offered = port.candidates(entities=("contacts",), context=north_context)
            self.assertIn(("Grace Hopper", "grace@example.test"),
                          [(c.display, c.detail) for c in offered])
            self.assertTrue(all(c.kind == "record" and c.entity == "contacts" for c in offered))
            # Another actor is offered their own records, never these.
            other = ContactsGoalPort(south).candidates(entities=("contacts",), context=south_context)
            self.assertIn(("Grace Hopper", "grace.hopper@southwind.test"),
                          [(c.display, c.detail) for c in other])
            self.assertNotIn("grace@example.test", [c.detail for c in other])
            # Not about contacts: nothing offered; no actor: refused.
            self.assertEqual(port.candidates(entities=("orders",), context=north_context), ())
            with self.assertRaises(PrincipalError):
                port.candidates(entities=("contacts",), context=None)

    def test_rest_page_and_counts_require_supported_scope(self):
        with MinimalContactsApplication() as application:
            adapter, _principal, context = self._bound(application, "ada-lovelace")
            port = ContactsGoalPort(adapter)
            spec = GoalSpec.from_json({"deliverables": [
                {"kind": "page", "entity": "contacts", "record": "Ada Lovelace"},
                {"kind": "aggregate", "entity": "contacts",
                 "metric": {"op": "count"}},
                {"kind": "page", "page": {"key": "contacts", "search": "Ada"}}]})
            result = port.resolve(spec, context=context)
            self.assertEqual(result.deliverables[0].page.url, "/contacts/ada-lovelace")
            self.assertEqual(result.deliverables[1].aggregate.count, 3)
            self.assertIn("no verified search", result.deliverables[2].problem)

    def test_create_handoff_is_actor_scoped_and_does_not_search_proposed_name(self):
        with MinimalContactsApplication() as application:
            writer, _principal, writer_context = self._bound(application, "ada-lovelace")
            reader, _reader_principal, reader_context = self._bound(
                application, "rosalind-franklin")
            spec = GoalSpec.from_json({"deliverables": [{
                "kind": "handoff", "entity": "contacts", "page": {"key": "contacts"},
                "record": "New Person", "action": "create"}], "write_intent": True})
            writer_port, reader_port = ContactsGoalPort(writer), ContactsGoalPort(reader)
            self.assertEqual(writer_port.index(context=writer_context).pages[0].operations,
                             ("create",))
            self.assertEqual(reader_port.index(context=reader_context).pages[0].operations,
                             ())
            permitted = writer_port.resolve(spec, context=writer_context).deliverables[0]
            self.assertEqual((permitted.page.url, permitted.page.highlight),
                             ("/contacts/new", "Name"))
            denied = reader_port.resolve(spec, context=reader_context).deliverables[0]
            self.assertIn("not available", denied.problem)

    def test_a_change_to_one_record_opens_it_and_names_the_control(self):
        with MinimalContactsApplication() as application:
            writer, _principal, context = self._bound(application, "ada-lovelace")
            spec = GoalSpec.from_json({"deliverables": [{
                "kind": "handoff", "entity": "contacts", "record": "Grace Hopper",
                "action": "Delete"}], "write_intent": True})
            resolved = ContactsGoalPort(writer).resolve(spec, context=context).deliverables[0]
            self.assertEqual((resolved.page.url, resolved.page.label, resolved.page.highlight),
                             ("/contacts/grace-hopper", "Grace Hopper", "Delete"))
            missing = GoalSpec.from_json({"deliverables": [{
                "kind": "handoff", "entity": "contacts", "record": "Nobody Here",
                "action": "Delete"}], "write_intent": True})
            self.assertTrue(ContactsGoalPort(writer).resolve(
                missing, context=context).deliverables[0].problem)


if __name__ == "__main__":
    unittest.main()
