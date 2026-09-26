"""The goal turn also runs over a REST application with actor-scoped data."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import (ACTORS_BY_ID, DEMO_SESSION_COOKIE,
                                   MinimalContactsApplication)
from testapps.contacts.goal import ContactsGoalPort
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent.state.identity import OperationContext, PrincipalError
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.business import _Chat


class ContactsGoalPortTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
