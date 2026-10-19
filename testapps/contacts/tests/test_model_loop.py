"""Real actor REST reads through the same core decision loop as the native adapter."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import ACTORS_BY_ID, DEMO_SESSION_COOKIE, MinimalContactsApplication
from testapps.contacts.goal import ContactsGoalPort
from testapps.contacts.model_loop import ContactsProcedure, contacts_profile
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.business import _Chat


class ContactsModelLoopTests(unittest.TestCase):
    def test_complete_sorted_rest_list_is_delivered_by_the_same_shared_contract(self):
        with MinimalContactsApplication() as app:
            records=[]
            def inspect(directory):
                records.extend(json.loads(path.read_text()) for path in (directory / "turns").glob("*.json"))
            result, _, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"summary":{"columns":["name","email"],"order_by":[{"column":"name","direction":"desc"}]}}),
                ("finish", {"status":"done","answer":"","read_answer":"read-1"})],
                prompt="List every visible contact in descending name order.",turn_traces=True,inspect=inspect)
            self.assertEqual("agent_finished",result.terminal_reason)
            projection=records[0]["server_deliveries"][0]["projection"]
            self.assertGreater(len(projection["rows"]),1)
            self.assertEqual(sorted((r[0] for r in projection["rows"]),reverse=True),[r[0] for r in projection["rows"]])
            self.assertEqual(projection["matched_count"],len(projection["row_record_ids"]))
            self.assertEqual(len(projection["rows"]),len(projection["row_record_ids"]))
            self.assertIn("| Contact name | Email address |", result.user_answer)
            self.assertEqual(["Contact name", "Email address"], [column["display_name"] for column in projection["columns"]])
            self.assertEqual(["text", "text"], [column["kind"] for column in projection["columns"]])

    def test_summary_contract_delivers_only_the_current_rest_actors_actual_values(self):
        for actor, email in (("ada-lovelace", "grace@example.test"),
                             ("mary-jackson", "grace.hopper@southwind.test")):
            with self.subTest(actor=actor), MinimalContactsApplication() as app:
                records = []
                def inspect(directory):
                    records.extend(json.loads(path.read_text()) for path in (directory / "turns").glob("*.json"))
                result, chat, _ = self.execute(app, actor, [
                    ("list_contacts", {"name_contains": "Grace Hopper", "summary": {
                        "columns": ["name", "email"], "measures": [{"op": "count", "as": "count"}]}}),
                    ("finish", {"status": "done", "answer": "", "read_answer": "read-1"})],
                    prompt="Show Grace Hopper's email and the number of exact matches.", turn_traces=True, inspect=inspect)
                self.assertEqual(result.terminal_reason, "agent_finished")
                self.assertIn(email, result.user_answer)
                other = "grace.hopper@southwind.test" if actor == "ada-lovelace" else "grace@example.test"
                self.assertNotIn(other, result.user_answer)
                delivery = records[0]["server_deliveries"][0]
                self.assertEqual(delivery["rendered_text"], result.user_answer)
                self.assertEqual(delivery["projection"]["matched_count"], 1)
                self.assertIn("Grace Hopper", delivery["projection"]["origin"]["scope"])

    def test_empty_rest_subset_is_complete_without_claiming_the_whole_source_is_empty(self):
        with MinimalContactsApplication() as app:
            result, _, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": "Absent synthetic name", "summary": {
                    "measures": [{"op": "count", "as": "count"}]}}),
                ("finish", {"status": "done", "answer": "", "read_answer": "read-1"})],
                prompt="How many contacts match Absent synthetic name?")
            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertIn("| 0 |", result.user_answer)
            self.assertIn("Absent synthetic name", result.user_answer)

    def test_rest_adapter_refuses_unsupported_numeric_semantics_without_issuing_a_delivery_reference(self):
        with MinimalContactsApplication() as app:
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"summary": {"measures": [{"op": "sum", "column": "email", "as": "total"}]}}),
                ("finish", {"status": "cannot", "answer": "Email is not an additive number."})],
                prompt="Total the email column.")
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertNotIn("read_answer=read-1", str(chat.messages))

    def test_redaction_does_not_collapse_distinct_rest_names_or_block_an_unrelated_count(self):
        rows = [{"id": "first", "name": "token=alpha123", "email": "a@example.test"},
                {"id": "second", "name": "token=beta456", "email": "b@example.test"}]
        with MinimalContactsApplication(contacts=rows) as app:
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"summary": {"measures": [{"op": "count_distinct", "column": "name", "as": "count"}]}}),
                ("finish", {"status": "cannot", "answer": "Those names cannot be delivered exactly."})],
                prompt="How many distinct names are there?")
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertIn("redacted_value", str(chat.messages))
            self.assertNotIn("alpha123", str(chat.messages))
            count, _, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"summary": {"measures": [{"op": "count", "as": "count"}]}}),
                ("finish", {"status": "done", "answer": "", "read_answer": "read-1"})],
                prompt="How many contacts are there?")
            self.assertEqual(count.terminal_reason, "agent_finished")
            self.assertIn("| 2 |", count.user_answer)

    def test_same_server_read_delivery_contract_uses_each_rest_actor_and_cannot_be_replaced_by_prose(self):
        for actor,email in (("ada-lovelace","grace@example.test"),("mary-jackson","grace.hopper@southwind.test")):
            with self.subTest(actor=actor), MinimalContactsApplication() as app:
                traces = []
                def inspect(directory):
                    traces.extend(json.loads(path.read_text()) for path in (directory / "turns").glob("*.json"))
                result,chat,_=self.execute(app,actor,[
                    ("list_contacts",{"name_contains":"Grace Hopper","deliver":True}),
                    ("finish",{"status":"done","answer":"All emails are listed above"}),
                    ("finish",{"status":"done","answer":"","read_answer":"read-1"})],
                    turn_traces=True, inspect=inspect)
                self.assertIn(email,result.user_answer)
                other="grace.hopper@southwind.test" if actor=="ada-lovelace" else "grace@example.test"
                self.assertNotIn(other,result.user_answer)
                self.assertIn("Grace Hopper",result.user_answer)
                self.assertNotIn("listed above",result.user_answer)
                self.assertEqual("agent_finished",result.terminal_reason)
                identity = traces[0]["server_deliveries"][0]["projection"]["identity"]
                self.assertEqual(identity["actor_digest"], traces[0]["actor"]["actor_authority_digest"])
                self.assertEqual(len(identity["record_ids"]), 1)

    def test_a_rest_read_can_finish_after_an_unsent_draft_without_publishing_that_draft(self):
        with MinimalContactsApplication() as app:
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": "Grace Hopper"}),
                (None, {"content": "Unsent draft with an invented address: never-publish@example.invalid"}),
                ("finish", {"status": "done", "answer": "Grace Hopper: grace@example.test"})])
            self.assertIn("grace@example.test", result.user_answer)
            self.assertNotIn("never-publish", result.user_answer)
            self.assertNotIn("never-publish", str(chat.messages[-1]))
            self.assertEqual("agent_finished", result.terminal_reason)

    def test_program_delivery_rejects_undelivered_text_without_an_extra_model_review(self):
        real_profile = contacts_profile
        reviewed = []
        def profile(*args, **kwargs):
            return replace(real_profile(*args, **kwargs), verification_messages=
                lambda prompt, answer, turn, obs, said: [{"role": "user", "content": answer}])
        class Reader(_Chat):
            def complete(self, messages, tools, *args, **kwargs):
                if tools is None:
                    answer = messages[-1]["content"]
                    reviewed.append(answer)
                    return {"content": json.dumps({"ok": False, "problems": ["The final answer omits the email."]})}
                return super().complete(messages, tools, *args, **kwargs)
        with MinimalContactsApplication() as app, patch(__name__ + ".contacts_profile", side_effect=profile):
            result, _, _ = self.execute(app, "ada-lovelace", Reader([
                ("list_contacts", {"name_contains": "Grace Hopper", "summary": {"columns": ["email"]}}),
                (None, {"content": "The email appears only in this unsent draft."}),
                ("finish", {"status": "done", "answer": "See above"}),
                ("finish", {"status": "done", "answer": "Already answered"}),
                ("finish", {"status": "done", "answer": "", "read_answer": "read-1"})]))
            self.assertEqual(reviewed, [])
            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertIn("grace@example.test", result.user_answer)
            self.assertNotIn("See above", result.user_answer)
            self.assertNotIn("Already answered", result.user_answer)

    def execute(self, app, actor_id, replies, *, source=None, observation=True, turn_traces=False,
                inspect=None, number_check=None, prompt="Grace Hopper email?"):
        actor = ACTORS_BY_ID[actor_id]
        adapter = MinimalContactsAdapter(app.origin, api_token=actor.token)
        identity = adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}")
        principal = adapter.identity.bind(identity, delegation_id="model-loop-test")
        port = ContactsGoalPort(adapter)
        channel = _Channel(page_url=app.origin + "/contacts", principal=principal, observation=observation)
        chat = replies if isinstance(replies, _Chat) else _Chat(replies)
        with tempfile.TemporaryDirectory() as tmp:
            executor = ModelDrivenTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="Read actor-visible contacts.",
                    require_identity=True, ledger_path=Path(tmp) / "audit.jsonl",
                    **({"source": source} if source is not None else {})),
                chat=chat, application=bind_application(adapter), goal_port=port,
                profile=replace(contacts_profile(adapter, port), untraced_numbers=number_check),
                control=ReadOnlyControl(),
                run_id_factory=lambda _: "contacts-model-run", turn_traces=turn_traces)
            with patch.object(goal_executor, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "contact-model-turn", "text": prompt,
                                           "page_url": app.origin + "/contacts"})
            if inspect is not None:
                inspect(Path(tmp))
        return result, chat, executor

    def test_number_check_includes_the_starting_page_the_model_was_shown(self):
        seen = []
        def check(answer, sources):
            seen.append(sources)
            return ()
        with MinimalContactsApplication() as app:
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("finish", {"status": "done", "answer": "The contacts page is open."})],
                number_check=check)
        self.assertEqual("agent_finished", result.terminal_reason)
        starting_page = chat.messages[0][-1]["content"].rsplit("\n", 1)[1]
        self.assertTrue(json.loads(starting_page)["url"].endswith("/contacts"))
        self.assertIn(starting_page, seen[0])

    def test_failed_fresh_observation_keeps_the_receipt_code_and_explains_recovery(self):
        execute = _RemoteBrowser.execute
        events = []
        def refused(browser, action, *args, **kwargs):
            receipt = execute(browser, action, *args, **kwargs)
            return replace(receipt, ok=False, error_code="capability_refused")
        def inspect(directory):
            events.extend(json.loads(line) for line in (directory / "audit.jsonl").read_text().splitlines())
        with MinimalContactsApplication() as app, patch.object(_RemoteBrowser, "execute", refused):
            result, chat, _ = self.execute(app, "ada-lovelace", [("list_contacts", {})], inspect=inspect)
        self.assertEqual("page_observation_failed", result.terminal_reason)
        self.assertIn("Refresh the page", result.user_answer)
        self.assertEqual(1, len(chat.messages))
        self.assertTrue(any(event.get("event") == "page_observation_failed"
                            and event.get("error_code") == "capability_refused" for event in events))

    def test_source_only_uses_same_loop_without_browser_or_business_reads(self):
        from web_operation_agent.tests.execution.test_repository_source_turn import RepositorySourceTurns
        with MinimalContactsApplication() as app, tempfile.TemporaryDirectory() as tmp:
            source = RepositorySourceTurns()._source(Path(tmp), "src/policy.py", "batch_limit = 125\n")
            with patch.object(_RemoteBrowser, "start", side_effect=AssertionError("no browser")), \
                    patch("testapps.contacts.model_loop.ContactsProcedure.execute",
                          side_effect=AssertionError("no business reads")):
                result, chat, executor = self.execute(app, "ada-lovelace", [
                    ("lookup_source", {"query": "batch_limit"}),
                    ("finish", {"status": "done", "answer": "invented answer ignored", "source_conclusions": [
                        {"kind": "source_excerpt", "evidence_ref": "source#1",
                         "statement": "The batch limit is 125.", "support": "batch_limit = 125"}]})],
                    source=source, observation=False)
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertEqual("According to the fixture-1 source: The batch limit is 125.",
                             result.user_answer)
            self.assertIsNone(result.entry_observation_id)
            self.assertIsNone(executor.channel.latest_observation_envelope)
            self.assertIn("source#1", str(chat.messages[1]))
            self.assertEqual("not_claimed", result.agent.success_claim)
            self.assertEqual(0, app.write_requests)

    def test_source_only_refusal_does_not_expose_model_guess(self):
        from web_operation_agent.tests.execution.test_repository_source_turn import RepositorySourceTurns
        with MinimalContactsApplication() as app, tempfile.TemporaryDirectory() as tmp:
            source = RepositorySourceTurns()._source(Path(tmp), "src/policy.py", "batch_limit = 125\n")
            result, _chat, _executor = self.execute(app, "ada-lovelace", [
                ("lookup_source", {"query": "missing rule"}),
                ("finish", {"status": "cannot", "answer": "The missing rule always allows it."})],
                source=source, observation=False)
            self.assertEqual("agent_refused", result.terminal_reason)
            self.assertIn("fixture-1", result.user_answer)
            self.assertIn("no direct basis", result.user_answer)
            self.assertNotIn("always allows", result.user_answer)
            self.assertEqual(0, app.write_requests)

    def test_source_without_page_cannot_start_business_tools(self):
        from web_operation_agent.tests.execution.test_repository_source_turn import RepositorySourceTurns
        with MinimalContactsApplication() as app, tempfile.TemporaryDirectory() as tmp:
            source = RepositorySourceTurns()._source(Path(tmp), "src/policy.py", "batch_limit = 125\n")
            with patch("testapps.contacts.model_loop.ContactsProcedure.execute") as business:
                result, chat, _ = self.execute(app, "ada-lovelace", [
                    ("list_contacts", {}),
                    ("finish", {"status": "cannot", "answer": "No browser observation."})],
                    source=source, observation=False)
            business.assert_not_called()
            self.assertIn("observation_unavailable", str(chat.messages[1]))
            self.assertEqual("agent_refused", result.terminal_reason)

    def test_source_then_real_rest_read_binds_page_in_the_same_conversation(self):
        from web_operation_agent.tests.execution.test_repository_source_turn import RepositorySourceTurns
        with MinimalContactsApplication() as app, tempfile.TemporaryDirectory() as tmp:
            source = RepositorySourceTurns()._source(Path(tmp), "src/policy.py", "batch_limit = 125\n")
            class ArrivingPage(_Chat):
                def complete(self, *args, **kwargs):
                    if len(self.messages) == 1:
                        channel = _RemoteBrowser.instances[-1].channel
                        channel.latest_observation_envelope = channel.envelope(1, app.origin + "/contacts")
                    return super().complete(*args, **kwargs)
            chat = ArrivingPage([
                ("lookup_source", {"query": "batch_limit"}),
                ("list_contacts", {"name_contains": "Grace Hopper"}),
                ("finish", {"status": "done", "answer": "grace@example.test", "source_conclusions": [
                    {"kind": "source_excerpt", "evidence_ref": "source#1",
                     "statement": "The batch limit is 125.", "support": "batch_limit = 125"}]})])
            result, chat, executor = self.execute(app, "ada-lovelace", chat, source=source, observation=False)
            self.assertEqual("agent_finished", result.terminal_reason)
            self.assertIn("grace@example.test", result.user_answer)
            self.assertIn("The batch limit is 125.", result.user_answer)
            self.assertIn("grace@example.test", str(chat.messages[2]))
            self.assertTrue(_RemoteBrowser.instances[-1].started)
            self.assertIsNone(result.entry_observation_id)
            self.assertEqual(ModelDrivenTurnExecutor, type(executor))
            self.assertEqual(0, app.write_requests)

    def test_turn_record_keeps_what_the_model_saw_without_the_actor_credentials(self):
        records = []
        def inspect(directory):
            records.extend(sorted((directory / "turns").glob("*.json")))
            records[:] = [json.loads(path.read_text()) for path in records]
        with MinimalContactsApplication() as app:
            actor = ACTORS_BY_ID["ada-lovelace"]
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": "Grace Hopper"}),
                ("finish", {"status": "done", "answer": "grace@example.test"})],
                turn_traces=True, inspect=inspect)
        self.assertEqual(AgentTurnStatus.FINISHED, result.status)
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(("Grace Hopper email?", "/contacts", "agent_finished", "contacts-model-run"),
                         (record["prompt"], record["start_route"], record["terminal_reason"],
                          record["run_id"]))
        self.assertEqual(len(chat.messages), len(record["calls"]))
        self.assertIn("grace@example.test", json.dumps(record["messages"]))
        self.assertEqual("list_contacts",
                         record["calls"][0]["reply"]["tool_calls"][0]["function"]["name"])
        self.assertEqual({"application_id", "tenant_digest", "actor_digest", "actor_authority_digest"},
                         set(record["actor"]))
        self.assertRegex(record["actor"]["actor_authority_digest"], r"^[0-9a-f]{64}$")
        text = json.dumps(record)
        self.assertNotIn(actor.token, text)
        self.assertNotIn(actor.session, text)

    def test_turns_write_no_record_unless_the_host_turns_it_on(self):
        found = []
        with MinimalContactsApplication() as app:
            self.execute(app, "ada-lovelace", [("finish", {"status": "done", "answer": "x"})],
                         inspect=lambda directory: found.extend((directory / "turns").glob("*")))
        self.assertEqual([], found)

    def test_actor_scoped_real_reads_reach_the_next_model_decision(self):
        with MinimalContactsApplication() as app:
            for actor, email in (("ada-lovelace", "grace@example.test"),
                                 ("mary-jackson", "grace.hopper@southwind.test")):
                with self.subTest(actor=actor):
                    result, chat, executor = self.execute(app, actor, [
                        ("list_contacts", {"name_contains": "Grace Hopper"}),
                        ("finish", {"status": "done", "answer": email})])
                    self.assertEqual(AgentTurnStatus.FINISHED, result.status)
                    self.assertEqual("not_claimed", result.agent.success_claim)
                    self.assertIn(email, str(chat.messages[1]))
                    self.assertEqual(ModelDrivenTurnExecutor, type(executor))
            self.assertEqual(0, app.write_requests)

    def test_unique_contact_link_uses_a_second_actor_read_and_the_answer_name(self):
        with MinimalContactsApplication() as app:
            result, _chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": "Grace Hopper"}),
                ("finish", {"status": "done", "answer": "Grace Hopper: grace@example.test"})])
            self.assertEqual([target.to_json() for target in result.answer_targets], [{
                "kind": "record", "address": "/contacts/grace-hopper", "label": "Grace Hopper",
                "identity": "contacts:grace-hopper", "read_ref": "read#1"}])
            missing_name, _chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": "Grace Hopper"}),
                ("finish", {"status": "done", "answer": "grace@example.test"})])
            self.assertEqual((), missing_name.answer_targets)
            ambiguous, _chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": ""}),
                ("finish", {"status": "done", "answer": "Grace Hopper is visible."})])
            self.assertEqual((), ambiguous.answer_targets)

    def test_unavailable_capability_is_not_called_or_claimed(self):
        with MinimalContactsApplication() as app:
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("fill_fields", {"fields": [{"label": "Name", "value": "Synthetic"}]}),
                ("finish", {"status": "cannot", "answer": "Application form entry is unavailable."})])
            self.assertEqual(AgentTurnStatus.REFUSED, result.status)
            self.assertIn("当前应用没有提供这个工具", str(chat.messages[1]))
            self.assertEqual(0, app.write_requests)

    def test_parameter_extras_fail_before_application_read(self):
        with MinimalContactsApplication() as app:
            result, chat, _ = self.execute(app, "ada-lovelace", [
                ("list_contacts", {"name_contains": "Grace", "another_workspace": "south"}),
                ("finish", {"status": "cannot", "answer": "Invalid arguments."})])
            self.assertEqual(AgentTurnStatus.REFUSED, result.status)
            self.assertIn("工具参数有误", str(chat.messages[1]))
            self.assertEqual(0, app.write_requests)


class ContactsAnswerTargetTests(unittest.TestCase):
    def test_actor_page_and_unique_record_are_read_again_before_issuing_targets(self):
        reads = []
        rows = [{"id": "grace-hopper", "name": "Grace Hopper", "ui_path": "/contacts/grace-hopper"}]
        def execute(_name, arguments, *, context):
            reads.append((arguments, context))
            return NS(content={"contacts": rows}, render=lambda: "Grace Hopper")
        actor = object()
        port = NS(_require_actor=lambda context: frozenset({"contacts:read"}),
                  _index=lambda context, permissions: (
                      NS(page=lambda key: NS(label="Contacts") if key == "contacts" else None),
                      {"contacts": "/contacts"}))
        adapter = NS(business_tools=NS(_executor=NS(execute=execute)))
        context = NS(page=NS(checkpoint=lambda: None), operation=actor)
        result = ContactsProcedure(adapter, port, "list_contacts").execute(
            {"name_contains": "Grace"}, context=context)
        self.assertEqual([(t.kind, t.address, t.label) for t in result.answer_targets], [
            ("page", "/contacts", "Contacts"),
            ("record", "/contacts/grace-hopper", "Grace Hopper")])
        self.assertEqual([args for args, _ in reads], [
            {"name_contains": "Grace"}, {"name_contains": "Grace Hopper"}])
        self.assertTrue(all(context is actor for _, context in reads))

        rows[:] = [rows[0], {"id": "grace-2", "name": "Grace Hopper",
                             "ui_path": "/contacts/grace-2"}]
        ambiguous = ContactsProcedure(adapter, port, "list_contacts").execute(
            {"name_contains": "Grace"}, context=context)
        self.assertEqual([target.kind for target in ambiguous.answer_targets], ["page"])


if __name__ == "__main__":
    unittest.main()

class ModelBrowserTests(unittest.TestCase):
    def test_structured_record_link_opens_an_actor_page_without_losing_a_draft(self):
        from playwright.sync_api import sync_playwright
        from testapps.contacts.server import NativeContactsHost, NativeServerConfig
        from testapps.contacts.tests.test_native_server import _CONNECTED, _SUBMIT
        chat = _Chat([
            ("list_contacts", {"name_contains": "Grace Hopper"}),
            ("finish", {"status": "done", "answer": "Grace Hopper: grace@example.test"}),
        ])
        with tempfile.TemporaryDirectory() as directory, \
                patch("testapps.contacts.server._chat", return_value=(chat, None)), \
                NativeContactsHost(NativeServerConfig(Path(directory), port=0, model_loop=True)) as host, \
                sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(host.origin + "/demo/login/ada-lovelace")
                page.goto(host.origin + "/contacts")
                page.evaluate(_CONNECTED)
                session = next(iter(host._sessions.values()))[0]
                page.evaluate(_SUBMIT, "Find Grace Hopper.")
                outcome = session.runner.next_outcome(timeout_s=35)
                self.assertIsNotNone(outcome)
                self.assertFalse(outcome.error, outcome.error)
                self.assertEqual("agent_finished", outcome.result.terminal_reason)
                self.assertEqual("/contacts/grace-hopper",
                                 outcome.result.answer_targets[0].target.address)
                page.locator(".woa-launcher").click()
                link = page.locator(".woa-answer-targets a", has_text="Grace Hopper")
                link.wait_for(state="visible")
                page.evaluate("document.body.insertAdjacentHTML('beforeend', '<input id=local-unsaved>')")
                page.locator("#local-unsaved").fill("keep this draft")
                with page.expect_popup() as popup_event:
                    link.click()
                popup = popup_event.value
                popup.wait_for_load_state()
                self.assertEqual(host.origin + "/contacts/grace-hopper", popup.url)
                self.assertEqual("keep this draft", page.locator("#local-unsaved").input_value())
                self.assertEqual(0, host.application.write_requests)
            finally:
                browser.close()

    def test_selected_section_finishes_on_its_verified_click_in_plain_html(self):
        import json
        from playwright.sync_api import sync_playwright
        from testapps.contacts.server import NativeContactsHost, NativeServerConfig
        from testapps.contacts.tests.test_native_server import _CONNECTED, _SUBMIT, _allow_operation
        real_profile, real_procedure = contacts_profile, ContactsProcedure.execute
        for blocked, required_review in ((False, False), (True, False), (False, True)):
            with self.subTest(blocked=blocked, required_review=required_review):
                def profile(*args, **kwargs):
                    original = real_profile(*args, **kwargs)
                    return replace(original, verification_messages=lambda prompt, answer, turn, obs, said:
                        [{"role":"user", "content":json.dumps({"request":prompt,"route":obs.page_route,
                                                                "answer":answer})}]) if required_review else original
                def procedure(self, arguments, *, context):
                    result = real_procedure(self, arguments, context=context)
                    return replace(result, review_completion=True) if required_review and self.name == "click" \
                        and result.state == "completed" else result
                class PickSection(_Chat):
                    def complete(self, messages, *args, **kwargs):
                        if required_review and args[0] is None:
                            self.messages.append([dict(m) for m in messages])
                            return {"content":json.dumps({"ok":True,"problems":[]})}
                        if len(self.messages) == 1:
                            content = messages[-1]['content']
                            observed = json.loads(content[content.index('{"url":'):])
                            target = next(e for e in observed['controls'] if e['attrs'].get('id') == 'workflow-terms')
                            self.script[0] = ('click', {'index': int(target['index'][1:]), 'answer': 'Terms selected.'})
                        return super().complete(messages, *args, **kwargs)
                chat = PickSection([('goto', {'path': '/contacts/workflows/partner'}),
                    ('click', {}), ('finish', {'status': 'cannot', 'answer': 'Selection could not be verified.'})])
                with tempfile.TemporaryDirectory() as directory, patch('testapps.contacts.server._chat', return_value=(chat, None)), \
                        patch('testapps.contacts.model_loop.contacts_profile', side_effect=profile), \
                        patch.object(ContactsProcedure, 'execute', procedure), \
                        NativeContactsHost(NativeServerConfig(Path(directory), port=0, model_loop=True)) as host, \
                        sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                        if blocked:
                            page.add_init_script("document.addEventListener('click', e => { if(e.target.id === 'workflow-terms') e.stopImmediatePropagation(); }, true)")
                        page.goto(host.origin + '/demo/login/ada-lovelace')
                        page.goto(host.origin + '/contacts/help')
                        page.evaluate(_CONNECTED)
                        session = next(iter(host._sessions.values()))[0]
                        page.evaluate(_SUBMIT, 'Open the partner desk Terms section.')
                        _allow_operation(page)
                        outcome = session.runner.next_outcome(timeout_s=35)
                        self.assertIsNotNone(outcome)
                        self.assertFalse(outcome.error, outcome.error)
                        self.assertEqual('agent_refused' if blocked else 'agent_finished', outcome.result.terminal_reason)
                        self.assertEqual(3 if blocked or required_review else 2, len(chat.messages))
                        self.assertEqual('false' if blocked else 'true', page.locator('#workflow-terms').get_attribute('aria-selected'))
                        self.assertEqual((0, 0), (host.application.write_requests, host.application.records_created))
                    finally:
                        browser.close()

    def test_combined_lines_and_filter_finish_only_after_verified_application_effects(self):
        from playwright.sync_api import sync_playwright
        from testapps.contacts.server import NativeContactsHost, NativeServerConfig
        from testapps.contacts.tests.test_native_server import _CONNECTED, _SUBMIT, _allow_operation
        for mode in ("lines", "changed_line_draft", "unreviewed_add", "filter", "partial_filter"):
            with self.subTest(mode=mode):
                is_filter = "filter" in mode
                args = ({"fields": [{"label": "Name contains", "value": "Grace"}],
                         "then_click": "Search contacts", "answer": "Contact filter applied."} if is_filter else
                        {"add_lines": {"button": "Add local line", "count": 2},
                         "fields": [{"label": "Period start", "value": "2026-10-20"},
                                    {"label": "Local line 1", "value": "First synthetic note"},
                                    {"label": "Local line 2", "value": "Second synthetic note"}],
                         "then_highlight": "Save contact", "answer": "Local lines ready for manual saving."})
                if mode == "partial_filter": args["fields"].append({"label": "Missing", "value": "x"})
                if mode == "unreviewed_add": args["add_lines"]["button"] = "Save contact"
                chat = _Chat([("goto", {"path": "/contacts/filter" if is_filter else "/contacts/new"}),
                              ("fill_fields", args),
                              ("finish", {"status": "cannot", "answer": "The phase could not be verified."})])
                with tempfile.TemporaryDirectory() as directory, patch("testapps.contacts.server._chat", return_value=(chat, None)), \
                        NativeContactsHost(NativeServerConfig(Path(directory), port=0, model_loop=True)) as host, \
                        sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1500})
                        if mode == "changed_line_draft":
                            page.add_init_script("document.addEventListener('click', event => { if (event.target.id === 'contact-add-line') document.querySelector('input[name=name]').value = 'Unexpected mutation'; }, true)")
                        page.goto(host.origin + "/demo/login/ada-lovelace")
                        page.goto(host.origin + "/contacts/help")
                        page.evaluate(_CONNECTED)
                        session = next(iter(host._sessions.values()))[0]
                        page.evaluate(_SUBMIT, "Apply the requested local phase without saving any record.")
                        _allow_operation(page)
                        outcome = session.runner.next_outcome(timeout_s=35)
                        self.assertIsNotNone(outcome)
                        self.assertFalse(outcome.error, outcome.error)
                        success = mode in {"lines", "filter"}
                        self.assertEqual("agent_finished" if success else "agent_refused", outcome.result.terminal_reason,
                                         outcome.result.user_answer)
                        self.assertEqual(2 if success else 3, len(chat.messages))
                        if mode == "lines":
                            self.assertEqual("Second synthetic note", page.get_by_label("Local line 2", exact=True).input_value())
                            self.assertEqual(1, page.locator(".woa-submit-guide").count())
                        if mode == "filter":
                            self.assertEqual("Grace Hopper", page.locator('#contact-filter-results').inner_text())
                        if mode == "partial_filter":
                            self.assertIn("Ada Lovelace", page.locator('#contact-filter-results').inner_text())
                        self.assertEqual((0, 0), (host.application.write_requests, host.application.records_created))
                    finally:
                        browser.close()

    def test_combined_form_phase_and_owned_choice_recovery_use_the_same_executor(self):
        from playwright.sync_api import sync_playwright
        from testapps.contacts.server import NativeContactsHost, NativeServerConfig
        from testapps.contacts.tests.test_native_server import _CONNECTED, _SUBMIT, _allow_operation
        for mode in ("ordinary", "recovery", "changed_draft"):
            recovery = mode != "ordinary"
            with self.subTest(mode=mode):
                steps = [("goto", {"path": "/contacts", "open": "New contact"})]
                if recovery:
                    steps.append(("fill_fields", {"fields": [{"label": "Priority", "value": "Missing priority"}],
                                                  "then_highlight": "Save contact", "answer": "Must not finish."}))
                fields = ([{"label": "Priority", "value": "Urgent"}] if recovery else [
                    {"label": "Period start", "value": "2026-10-20"},
                    {"label": "Period end", "value": "2026-10-22"}])
                steps.append(("fill_fields", {"fields": fields, "then_highlight": "Save contact",
                                              "answer": "Fields ready."}))
                chat = _Chat(steps)
                with tempfile.TemporaryDirectory() as directory, patch("testapps.contacts.server._chat", return_value=(chat, None)), \
                        NativeContactsHost(NativeServerConfig(Path(directory), port=0, model_loop=True)) as host, \
                        sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 1280, "height": 1100})
                        if mode == "changed_draft":
                            page.add_init_script("document.addEventListener('click', event => { if (event.target.id === 'contact-draft-priority') document.querySelector('input[name=name]').value = 'Unexpected local change'; }, true)")
                        page.goto(host.origin + "/demo/login/ada-lovelace")
                        page.goto(host.origin + "/contacts/help")
                        page.evaluate(_CONNECTED)
                        session = next(iter(host._sessions.values()))[0]
                        page.evaluate(_SUBMIT, "Prepare the local fields and leave saving to me.")
                        try:
                            _allow_operation(page, timeout_ms=5_000)
                        except Exception:
                            logs = [f.read_text()[-5000:] for f in Path(directory).rglob("audit.jsonl")]
                            self.fail(f"No operation card; model calls={len(chat.messages)}; logs={logs}; page={page.locator('body').inner_text()[-3000:]}")
                        outcome = session.runner.next_outcome(timeout_s=30)
                        self.assertIsNotNone(outcome)
                        self.assertFalse(outcome.error, outcome.error)
                        logs = [f.read_text()[-6500:] for f in Path(directory).rglob("audit.jsonl")]
                        if mode == "changed_draft":
                            self.assertEqual("agent_refused", outcome.result.terminal_reason)
                            self.assertEqual(2, len(chat.messages))
                            self.assertEqual(0, page.locator(".woa-submit-guide").count())
                            self.assertEqual((0, 0), (host.application.write_requests, host.application.records_created))
                            continue
                        self.assertEqual("agent_finished", outcome.result.terminal_reason, (outcome.result.user_answer, logs))
                        self.assertIn("Fields ready.", outcome.result.user_answer)
                        self.assertIn("请核对后，手动点击", outcome.result.user_answer)
                        self.assertIn("「Save contact」提交。", outcome.result.user_answer)
                        self.assertEqual(3 if recovery else 2, len(chat.messages))
                        if recovery:
                            self.assertIn("option_unavailable", str(chat.messages[-1]))
                            self.assertIn("Urgent", str(chat.messages[-1]))
                            self.assertEqual("Urgent", page.get_by_label("Priority", exact=True).input_value())
                        else:
                            self.assertEqual("2026-10-20/2026-10-22", page.locator('#contact-create').get_attribute('data-period'))
                        self.assertEqual(1, page.locator(".woa-submit-guide").count())
                        self.assertEqual((0, 0), (host.application.write_requests, host.application.records_created))
                    finally:
                        browser.close()

    def test_plain_html_two_dates_and_local_line_use_guarded_common_draft_port(self):
        import json
        from playwright.sync_api import sync_playwright
        from testapps.contacts.server import NativeContactsHost, NativeServerConfig
        from testapps.contacts.tests.test_native_server import _CONNECTED, _SUBMIT, _allow_operation

        class Decisions:
            model = "scripted/no-model"
            def __init__(self): self.position, self.seen = 0, []
            def complete(self, messages, tools, run_id, **kwargs):
                return self.complete_cancellable(messages, tools, run_id, cancelled=lambda: False, **kwargs)
            def complete_cancellable(self, messages, tools, run_id, *, cancelled, **kwargs):
                self.seen.append(messages)
                self.position += 1
                if self.position == 1:
                    tool_name, args = "list_contacts", {"name_contains": "Grace"}
                else:
                    current = json.loads(messages[-1]["content"].split("\n")[-1])
                    target, value = {2: ("Period start", "2026-10-20"),
                                     3: ("Period end", "2026-10-22"),
                                     4: ("Add local line", ""),
                                     5: ("Local line 1", "Synthetic local note"),
                                     6: ("Save contact", "")}[self.position]
                    control = next(e for e in current["controls"]
                        if e["attrs"].get("field_label", e["name"]) == target)
                    args = {"index": int(control["index"][1:])}
                    tool_name = "click" if self.position == 4 else "highlight" if self.position == 6 else "input"
                    if tool_name == "input": args["text"] = value
                    if tool_name == "highlight": args["answer"] = "Dates and local line are ready; review and save."
                return {"role": "assistant", "content": "", "tool_calls": [{"id": str(self.position),
                    "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}]}

        chat = Decisions()
        with tempfile.TemporaryDirectory() as directory, patch("testapps.contacts.server._chat", return_value=(chat, None)), \
                NativeContactsHost(NativeServerConfig(Path(directory), port=0, model_loop=True)) as host, \
                sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1100})
                page.goto(host.origin + "/demo/login/ada-lovelace")
                page.goto(host.origin + "/contacts/new")
                page.evaluate("async () => { window.__WOA_ASSISTANT__.connect(); await Promise.race([window.__WOA_RUNTIME_READY__, new Promise((_, reject) => setTimeout(() => reject(new Error(\"bootstrap timeout\")), 10000))]); }")
                session = next(iter(host._sessions.values()))[0]
                page.evaluate(_SUBMIT, "Enter October 20–22, 2026 and add Synthetic local note; leave saving to me.")
                _allow_operation(page)
                outcome = session.runner.next_outcome(timeout_s=30)
                self.assertIsNotNone(outcome)
                self.assertFalse(outcome.error, outcome.error)
                self.assertEqual("agent_finished", outcome.result.terminal_reason, outcome.result.user_answer)
                self.assertEqual("not_claimed", outcome.result.agent.success_claim)
                self.assertEqual("2026-10-20/2026-10-22", page.locator('#contact-create').get_attribute('data-period'))
                self.assertEqual("Synthetic local note", page.get_by_label("Local line 1", exact=True).input_value())
                self.assertEqual((0, 0), (host.application.write_requests, host.application.records_created))
                events = [json.loads(line) for f in Path(directory).rglob("audit.jsonl") for line in f.read_text().splitlines()]
                self.assertEqual(4, sum(row.get("event") == "action_effect_verified" for row in events))
                self.assertIn("grace@example.test", str(chat.seen[1]))
            finally: browser.close()


class WindowTests(unittest.TestCase):
    def test_window_rejects_real_page_mutations_and_cannot_be_opened_by_delegation(self):
        import json
        import urllib.error
        import urllib.request
        actor = ACTORS_BY_ID["ada-lovelace"]
        with MinimalContactsApplication() as app:
            def post(path, headers, body):
                request = urllib.request.Request(app.origin + path, method="POST",
                    headers={"Content-Type": "application/json", **headers}, data=json.dumps(body).encode())
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)

            cookie = {"Cookie": f"{DEMO_SESSION_COOKIE}={actor.session}", "Origin": app.origin}
            status, _ = post("/api/page-window/begin", {"Authorization": "Bearer " + actor.token,
                "Origin": app.origin}, {"ttl_ms": 60000})
            self.assertEqual(403, status)
            status, _ = post("/api/page-window/begin", {"Cookie": cookie["Cookie"]}, {"ttl_ms": 60000})
            self.assertEqual(403, status)
            status, window = post("/api/page-window/begin", cookie, {"ttl_ms": 60000})
            self.assertEqual((200, True), (status, window["active"]))
            status, error = post("/api/contacts", cookie,
                {"name": "Synthetic guarded contact", "email": "guard@example.test"})
            self.assertEqual((409, "read_only_window"), (status, error["error"]))
            self.assertEqual((0, 0), (app.write_requests, app.records_created))
            self.assertEqual(200, post("/api/page-window/end", cookie, {"ttl_ms": 60000})[0])
