"""Real actor REST reads through the same core decision loop as the native adapter."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import ACTORS_BY_ID, DEMO_SESSION_COOKIE, MinimalContactsApplication
from testapps.contacts.goal import ContactsGoalPort
from testapps.contacts.model_loop import contacts_profile
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.business import _Chat


class ContactsModelLoopTests(unittest.TestCase):
    def execute(self, app, actor_id, replies):
        actor = ACTORS_BY_ID[actor_id]
        adapter = MinimalContactsAdapter(app.origin, api_token=actor.token)
        identity = adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}")
        principal = adapter.identity.bind(identity, delegation_id="model-loop-test")
        port = ContactsGoalPort(adapter)
        channel = _Channel(page_url=app.origin + "/contacts", principal=principal)
        chat = _Chat(replies)
        with tempfile.TemporaryDirectory() as tmp:
            executor = ModelDrivenTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="Read actor-visible contacts.",
                    require_identity=True, ledger_path=Path(tmp) / "audit.jsonl"),
                chat=chat, application=bind_application(adapter), goal_port=port,
                profile=contacts_profile(adapter, port), control=ReadOnlyControl(),
                run_id_factory=lambda _: "contacts-model-run")
            with patch.object(goal_executor, "RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({"message_id": "contact-model-turn", "text": "Grace Hopper email?",
                                           "page_url": app.origin + "/contacts"})
        return result, chat, executor

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


if __name__ == "__main__":
    unittest.main()

class ModelBrowserTests(unittest.TestCase):
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
