"""The real loop queries source on the server and reasons again from a fresh page."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.affordance import (AffordanceParameter,
                                             resolve_source_affordances)
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.schemas import empty_contract
from web_operation_agent.session import SCOPE_FIRST_SYSTEM, ServerAgentSession, TaskSpec
from web_operation_agent.tests.route_fixture import (FIXTURE_ROUTE_PORT,
                                                     fixture_route_identity)
from web_operation_agent.privacy import (SESSION_HANDOFF_QUERY_KEYS,
                                         SessionHandoffPolicy)
from web_operation_agent.tests.test_contract import _map
from web_operation_agent.tests.test_session import _Chat, _Control, _Browser, _obs


def _candidate_url(candidate, bindings):
    return ServerAgentSession._candidate_url(
        candidate, bindings, route_identity=fixture_route_identity,
        handoffs=SessionHandoffPolicy.extending(
            FIXTURE_ROUTE_PORT.session_handoff_query_keys))


class RuntimeSourceLoop(unittest.TestCase):
    def test_scope_first_policy_is_one_shared_context_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("finish", {"done": False, "why": "inspect prompt"})])
            browser = _Browser([_obs(1)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "inspect"), arm="S", chat=chat, browser=browser,
                control=_Control([[], []]), ledger=SessionLedger(root / "prompt.jsonl", "p"),
                source_map=_map(root), runtime_source=True,
                scope_first_policy=True)
            session.entry_observation = _obs(1)
            session.run()
            system = chat.seen[0][0]["content"]
            self.assertIn(SCOPE_FIRST_SYSTEM, system)

    def test_policy_refusal_can_recover_through_the_complete_source_route_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            route = "/glasshouse/do/findTrays?trayId=42"
            chat = _Chat([
                ("fill", {"ref": "e1", "value": "forbidden search"}),
                ("lookup_source", {"query": "find trays"}),
                ("open_source_route", {"url": route}),
                ("finish", {"done": True, "why": "target page is visible"}),
            ])
            browser = _Browser([
                _obs(1), _obs(2), _obs(3), _obs(4, "http://app" + route),
            ])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "open and inspect trays"), arm="S", chat=chat,
                browser=browser, control=_Control([[], [], [], [], []]),
                ledger=SessionLedger(root / "recovery.jsonl", "recovery"),
                source_map=_map(root), runtime_source=True,
                disabled_actions={"fill", "select", "submit"}, policy_block_limit=2)
            session.entry_observation = _obs(1)

            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.policy_blocks, 1)
            self.assertEqual([action for action, _ in browser.done],
                             ["observe", "observe", "goto"])
            self.assertEqual(browser.done[-1][1]["url"], route)
            policy_receipt = next(event for event in session.ledger.of_kind("action_receipt")
                                  if event.get("policy_block"))
            self.assertTrue(policy_receipt["fresh_observation"])
            self.assertGreater(policy_receipt["observation_epoch"],
                               policy_receipt["page_epoch_before"])
            self.assertEqual(len(session.ledger.of_kind("runtime_source_retrieved")), 1)
            self.assertEqual(len(session.ledger.of_kind("source_route_opened")), 1)

    def test_lookup_after_withdrawal_reaches_next_model_call_and_refreshes_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smap = _map(root)
            chat = _Chat([("lookup_source", {"query": "sprout a cultivar into a seed tray"}),
                          ("fill", {"ref": "e1", "value": "new cultivar"}),
                          ("finish", {"done": False, "why": "diagnostic complete"})])
            browser = _Browser([_obs(1), _obs(2), _obs(3)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "sprout seedling"), arm="S",
                chat=chat, browser=browser, control=_Control([[], [], []]),
                ledger=SessionLedger(root / "run.jsonl", "react-test"),
                source_map=smap, runtime_source=True)
            session.contract = empty_contract("T")
            session.contract_source = "compiled"
            session.plan_state._withdraw("synthetic counterevidence to the initial route")
            session.entry_observation = _obs(1)
            result = session.run()
            self.assertEqual(result.terminal_reason, "agent_refused")
            second = "\n".join(m.get("content", "") for m in chat.seen[1])
            self.assertIn("sproutSeedling", second)
            self.assertIn("epoch 2", second)
            events = session.ledger.of_kind("runtime_source_retrieved")
            self.assertEqual(len(events), 1)
            self.assertIn("sproutSeedling", "\n".join(events[0]["evidence"]))
            # Source never crosses the browser boundary: only generic observe and fill.
            self.assertEqual([a for a, _ in browser.done], ["observe", "fill"])
            self.assertEqual(browser.done[1][1]["epoch"], 2)
            self.assertNotIn("sproutSeedling", str(browser.done))

    def test_current_page_title_is_audit_context_not_an_implicit_search_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smap = _map(root)
            chat = _Chat([("lookup_source", {"query": "find trays"}),
                          ("finish", {"done": False, "why": "done"})])
            misleading = _obs(1, "http://app/beacon/do/LampSchedule/create")
            misleading.title = "Lamp Schedule Lighthouse"
            browser = _Browser([misleading, _obs(2)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect"), arm="S",
                chat=chat, browser=browser, control=_Control([[], []]),
                ledger=SessionLedger(root / "run.jsonl", "query-context"),
                source_map=smap, runtime_source=True)
            session.entry_observation = misleading
            session.served_webapps = {"glasshouse", "beacon"}
            session.run()
            evidence = session.ledger.of_kind("runtime_source_retrieved")[0]["evidence"]
            self.assertTrue(any("glasshouse/findTrays" in line for line in evidence))
            self.assertFalse(any("LampSchedule/create" in line for line in evidence))

    def test_source_tools_are_absent_from_the_default_and_page_only_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for enabled in (False, True):
                session = ServerAgentSession(
                    application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect"),
                    arm="S", chat=_Chat([]), browser=_Browser([_obs(1)]),
                    control=_Control([[], []]),
                    ledger=SessionLedger(root / f"tools-{enabled}.jsonl", "t"),
                    source_map=_map(root), runtime_source=enabled)
                names = [t["function"]["name"] for t in session.tool_schemas()]
                self.assertEqual("lookup_source" in names, enabled)
                self.assertEqual("open_source_route" in names, enabled)

    def test_a_cited_view_route_can_be_opened_and_is_a_generic_browser_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smap = _map(root)
            chat = _Chat([("lookup_source", {"query": "find trays"}),
                          ("open_source_route", {
                              "url": "/glasshouse/do/findTrays?trayId=42"}),
                          ("finish", {"done": False, "why": "mechanism observed"})])
            browser = _Browser([_obs(1), _obs(2),
                                _obs(3, "http://app/glasshouse/do/findTrays?trayId=42")])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect trays"), arm="S",
                chat=chat, browser=browser, control=_Control([[], [], []]),
                ledger=SessionLedger(root / "run.jsonl", "source-route"),
                source_map=smap, runtime_source=True)
            session.entry_observation = _obs(1)
            result = session.run()
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual([a for a, _ in browser.done], ["observe", "goto"])
            self.assertEqual(browser.done[1][1]["url"],
                             "/glasshouse/do/findTrays?trayId=42")
            opened = session.ledger.of_kind("source_route_opened")
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0]["citations"])

    def test_an_absolute_url_is_rejected_even_after_its_route_was_cited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smap = _map(root)
            chat = _Chat([("lookup_source", {"query": "find trays"}),
                          ("open_source_route", {
                              "url": "https://elsewhere.invalid/glasshouse/do/findTrays"}),
                          ("finish", {"done": False, "why": "rejected as expected"})])
            browser = _Browser([_obs(1), _obs(2)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect trays"), arm="S",
                chat=chat, browser=browser, control=_Control([[], []]),
                ledger=SessionLedger(root / "run.jsonl", "source-route-reject"),
                source_map=smap, runtime_source=True)
            session.entry_observation = _obs(1)
            session.run()
            self.assertEqual([a for a, _ in browser.done], ["observe"])
            self.assertEqual(len(session.ledger.of_kind("source_route_rejected")), 1)

    def test_source_route_rejects_uncited_and_path_escape_variants(self):
        bad_urls = ("//elsewhere.invalid/glasshouse/do/findTrays",
                    "/glasshouse/do/findTrays/%2e%2e/private",
                    "/glasshouse/do/findTrays/../private",
                    "/glasshouse/do/notCited")
        for url in bad_urls:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chat = _Chat([("lookup_source", {"query": "find trays"}),
                              ("open_source_route", {"url": url}),
                              ("finish", {"done": False, "why": "rejected"})])
                browser = _Browser([_obs(1), _obs(2)])
                session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect trays"), arm="S",
                    chat=chat, browser=browser, control=_Control([[], []]),
                    ledger=SessionLedger(root / "run.jsonl", "source-route-unsafe"),
                    source_map=_map(root), runtime_source=True)
                session.entry_observation = _obs(1)
                session.run()
                self.assertEqual([a for a, _ in browser.done], ["observe"])
                self.assertEqual(len(session.ledger.of_kind("source_route_rejected")), 1)

    def test_cited_view_route_can_verify_after_a_write_without_dispatching_another_write(self):
        permit = {"event": "permit_consumed", "webapp": "glasshouse",
                  "uri": "updateTray"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("click", {"ref": "e1"}),
                          ("lookup_source", {"query": "find trays"}),
                          ("open_source_route", {
                              "url": "/glasshouse/do/findTrays?trayId=42"}),
                          ("finish", {"done": False, "why": "verified read-only page"})])
            browser = _Browser([_obs(1), _obs(2), _obs(3),
                                _obs(4, "http://app/glasshouse/do/findTrays?trayId=42")])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect trays"), arm="S",
                chat=chat, browser=browser, control=_Control([[], [permit], [], []]),
                ledger=SessionLedger(root / "run.jsonl", "source-route-after-write"),
                source_map=_map(root), runtime_source=True)
            session.entry_observation = _obs(1)
            result = session.run()
            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual([a for a, _ in browser.done], ["click", "observe", "goto"])
            self.assertEqual(result.page_actions, 2)
            self.assertEqual(result.spend["provider_calls"], 0)

    def test_generic_goto_cannot_invent_a_route_absent_from_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("goto", {"url": "/invented"}),
                          ("finish", {"done": False, "why": "rejected"})])
            browser = _Browser([_obs(1)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect"), arm="P",
                chat=chat, browser=browser, control=_Control([[]]),
                ledger=SessionLedger(root / "run.jsonl", "page-route-reject"))
            session.entry_observation = _obs(1)
            session.run()
            self.assertEqual(browser.done, [])
            self.assertEqual(len(session.ledger.of_kind("page_route_rejected")), 1)

    def test_generic_goto_accepts_the_exact_href_on_the_current_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("goto", {"url": "/y"}),
                          ("finish", {"done": False, "why": "opened"})])
            browser = _Browser([_obs(1), _obs(2, "http://app/y")])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "inspect"), arm="P",
                chat=chat, browser=browser, control=_Control([[], []]),
                ledger=SessionLedger(root / "run.jsonl", "page-route-open"))
            session.entry_observation = _obs(1)
            session.run()
            self.assertEqual([a for a, _ in browser.done], ["goto"])

    def test_cannot_enable_source_without_a_source_map(self):
        with self.assertRaises(ValueError):
            ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "x"), arm="P", chat=None, browser=None,
                               control=None, ledger=None, runtime_source=True)
        with self.assertRaisesRegex(ValueError, "auto source affordances"):
            ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "x"), arm="P", chat=None, browser=None,
                               control=None, ledger=None,
                               source_affordance_mode="auto")
        with self.assertRaisesRegex(ValueError, "source_contract"):
            ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT, task=TaskSpec("T", "x"), arm="P", chat=None, browser=None,
                               control=None, ledger=None,
                               source_contract="yes")

    def test_source_navigation_requires_an_application_route_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "application route port"):
                ServerAgentSession(
                    task=TaskSpec("T", "x"), arm="P", chat=None,
                    browser=_Browser([_obs(1)]), control=_Control([[]]),
                    ledger=SessionLedger(Path(tmp) / "route-port.jsonl", "route-port"),
                    source_map=_map(Path(tmp)), runtime_source=True)


class SourceAffordanceLoop(unittest.TestCase):
    @staticmethod
    def _candidate(smap):
        return resolve_source_affordances(
            smap, task="Inspect the seed trays", page_text="",
            served_webapps={"glasshouse"}, route_port=FIXTURE_ROUTE_PORT)[0]

    def test_auto_and_empty_expose_identical_tools_but_only_auto_delivers_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smap = _map(root)
            sessions = []
            chats = []
            for mode, source_map in (("auto", smap), ("empty", None)):
                chats.append(_Chat([("finish", {"done": False, "why": "inspect"})]))
                session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                    task=TaskSpec("T", "Inspect the seed trays"), arm="P",
                    chat=chats[-1],
                    browser=_Browser([_obs(1)]), control=_Control([[], []]),
                    ledger=SessionLedger(root / f"{mode}.jsonl", mode),
                    source_map=source_map, source_contract=False,
                    source_affordance_mode=mode)
                session.entry_observation = _obs(1)
                session.served_webapps = {"glasshouse"}
                sessions.append(session)
            self.assertEqual(sessions[0].tool_schemas(), sessions[1].tool_schemas())
            names = [tool["function"]["name"] for tool in sessions[0].tool_schemas()]
            self.assertIn("open_source_candidate", names)
            self.assertNotIn("lookup_source", names,
                             "the causal pair must not gain a second source channel")
            with self.assertRaisesRegex(ValueError, "runtime source lookup is disabled"):
                sessions[0].lookup_source("find trays", _obs(1))

            for session in sessions:
                session.run()
            auto_opening = "\n".join(str(m.get("content") or "")
                                      for m in chats[0].seen[0])
            empty_opening = "\n".join(str(m.get("content") or "")
                                       for m in chats[1].seen[0])
            self.assertIn('"request":"findTrays"', auto_opening)
            self.assertIn("SOURCE AFFORDANCES", empty_opening)
            self.assertIn("[]", empty_opening)

    def test_candidate_is_delivered_on_entry_opened_as_generic_goto_and_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smap = _map(root)
            candidate = self._candidate(smap)
            chat = _Chat([
                ("open_source_candidate", {
                    "candidate_id": candidate.candidate_id,
                    "bindings": {"trayId": "A&B/../"},
                }),
                ("finish", {"done": False, "why": "opened"}),
            ])
            browser = _Browser([
                _obs(1), _obs(2, "http://app/glasshouse/do/findTrays"),
            ])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "Inspect the seed trays"), arm="P", chat=chat,
                browser=browser, control=_Control([[], [], []]),
                ledger=SessionLedger(root / "auto.jsonl", "auto"),
                source_map=smap, source_contract=False, source_affordance_mode="auto")
            session.entry_observation = _obs(1)
            session.served_webapps = {"glasshouse"}

            session.run()

            self.assertEqual([action for action, _ in browser.done], ["goto"])
            sent_url = browser.done[0][1]["url"]
            self.assertEqual(sent_url,
                             "/glasshouse/do/findTrays?trayId=A%26B%2F..%2F&viewMode=summary")
            opened = session.ledger.of_kind("source_candidate_opened")
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0]["candidate_id"], candidate.candidate_id)
            self.assertTrue(opened[0]["citations"])
            delivered = session.ledger.of_kind("source_affordances_delivered")
            self.assertEqual([event["reason"] for event in delivered],
                             ["entry", "observation"])
            self.assertTrue(all(event["citations"] for event in delivered))
            # No candidate object crosses the process boundary; only generic goto does.
            self.assertNotIn("candidate", str(browser.done).lower())

    def test_lookup_tool_result_refreshes_candidates_with_the_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([("lookup_source", {"query": "find seed trays"}),
                          ("finish", {"done": False, "why": "done"})])
            browser = _Browser([_obs(1), _obs(2)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "Inspect records"), arm="S", chat=chat,
                browser=browser, control=_Control([[], [], []]),
                ledger=SessionLedger(root / "lookup.jsonl", "lookup"),
                source_map=_map(root), source_contract=False,
                runtime_source=True, source_affordance_mode="auto")
            session.entry_observation = _obs(1)
            session.served_webapps = {"glasshouse"}
            session.run()
            second_turn = "\n".join(str(m.get("content") or "") for m in chat.seen[1])
            self.assertIn("SOURCE EVIDENCE", second_turn)
            self.assertIn("SOURCE AFFORDANCES", second_turn)
            self.assertIn('"request":"findTrays"', second_turn)
            self.assertEqual(session.ledger.of_kind("source_affordances_delivered")[-1]["reason"],
                             "lookup")

    def test_invalid_candidate_is_rejected_then_observed_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _Chat([
                ("open_source_candidate", {
                    "candidate_id": "sa_unknown", "bindings": {"externalLoginKey": "secret"},
                }),
                ("finish", {"done": False, "why": "used fresh page"}),
            ])
            browser = _Browser([_obs(1), _obs(2)])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "Inspect records"), arm="P", chat=chat,
                browser=browser, control=_Control([[], [], []]),
                ledger=SessionLedger(root / "empty.jsonl", "empty"),
                source_contract=False, source_affordance_mode="empty")
            session.entry_observation = _obs(1)
            session.run()
            self.assertEqual([action for action, _ in browser.done], ["observe"])
            rejected = session.ledger.of_kind("source_candidate_rejected")
            self.assertEqual(len(rejected), 1)
            self.assertFalse(rejected[0]["requested_tool_dispatched"])
            receipt = next(event for event in session.ledger.of_kind("action_receipt")
                           if event.get("source_candidate_rejection"))
            self.assertTrue(receipt["fresh_observation"])
            self.assertEqual(receipt["observation_epoch"], 2)
            # The rejected session value is redacted before the ordinary tool-call audit.
            self.assertNotIn("secret", str(session.ledger.events))

    def test_candidate_url_rejects_unsafe_bindings_and_source_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = self._candidate(_map(Path(tmp)))
            cases = [
                ({"undeclared": "x"}, "undeclared"),
                ({"trayId": "${parameters.partyId}"}, "forbidden"),
                ({"trayId": "x\nnext"}, "forbidden"),
                ({f"p{i}": "x" for i in range(17)}, "maximum"),
            ]
            for bindings, phrase in cases:
                with self.subTest(bindings=bindings):
                    _, error = _candidate_url(candidate, bindings)
                    self.assertIn(phrase, error)

            fixed = replace(candidate, parameters=(AffordanceParameter(
                "viewMode", value="summary"),))
            _, error = _candidate_url(fixed, {"viewMode": "full"})
            self.assertIn("override", error)
            conflict = replace(candidate, parameters=(
                AffordanceParameter("viewMode", value="summary"),
                AffordanceParameter("viewMode", value="full")))
            _, error = _candidate_url(conflict, {})
            self.assertIn("conflicting", error)
            # The refusal must follow the adapter's declared vocabulary, not a name this
            # package happens to ship: `fixturesessionkey` is unknown to the static baseline.
            adapter_key = next(iter(FIXTURE_ROUTE_PORT.session_handoff_query_keys))
            self.assertNotIn(adapter_key, SESSION_HANDOFF_QUERY_KEYS)
            session_parameter = replace(candidate, parameters=(
                AffordanceParameter(adapter_key),))
            _, error = _candidate_url(session_parameter, {adapter_key: "secret"})
            self.assertIn("session", error)
            for route in ("https://elsewhere.invalid/x", "//elsewhere.invalid/x"):
                with self.subTest(route=route):
                    with self.assertRaisesRegex(ValueError, "same-webapp relative"):
                        replace(candidate, route_template=route)
            unsafe = replace(candidate, route_template="/glasshouse/do/../private")
            _, error = _candidate_url(unsafe, {})
            self.assertIn("safe relative", error)

    def test_source_map_can_feed_affordances_without_compiling_the_contract(self):
        from web_operation_agent.tests.test_contract import _StubModel

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = _StubModel([
                '{"task_ref":"T","summary":"page-derived","citations":[]}'
            ])
            session = ServerAgentSession(application_routes=FIXTURE_ROUTE_PORT,
                task=TaskSpec("T", "Inspect the seed trays"), arm="P", chat=model,
                browser=_Browser([_obs(1)]), control=_Control([[]]),
                ledger=SessionLedger(root / "plan.jsonl", "plan"),
                source_map=_map(root), source_contract=False, plan_from_page=True,
                source_affordance_mode="auto")
            session.entry_observation = _obs(1)
            session.served_webapps = {"glasshouse"}
            session.plan()
            self.assertEqual(session.contract_source, "page")
            self.assertEqual(session.bundle.lines, [])
            self.assertNotIn("source code", model.calls[0][0]["content"])
