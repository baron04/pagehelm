"""Host lifecycle over synthetic identity responses; no application or model calls."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from web_operation_agent.schemas import ClientHello
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent_yudao.host.application import SESSION_HANDOFF_KEYS, YudaoIdentityPort
from web_operation_agent_yudao.reads.client import YudaoClient, YudaoReadRefused
from web_operation_agent_yudao.host.native_server import BootstrapRefused, YudaoHost, YudaoHostConfig
from web_operation_agent_yudao.host.guard_proxy import NativeGuardProxy as RealGuardProxy
from web_operation_agent_yudao.executor.read_only_window_actions import ReadOnlyWindowPageActions


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hosts = []
        self.addCleanup(lambda: [h.close() for h in self.hosts])
        self.config = YudaoHostConfig(
            api_base="http://yudao.invalid", public_origin="https://yudao.invalid",
            state_dir=Path(self.tmp.name), gateway_host="127.0.0.1", gateway_port=0, warm_index=False,
            native_page_admission="reviewed")
        self.config = replace(self._fence(), native_page_policy=self._page_policy())
        self.addCleanup(patch.stopall)
        patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy").start()
        self.documents = patch("web_operation_agent_yudao.host.native_server._api_docs", return_value={}).start()
        self.chat = patch("web_operation_agent_yudao.host.native_server.ChatModel").start()
        self.chat.return_value.model = "scripted/no-model"
        self.chat.return_value.egress_policy = ProviderEgressPolicy(
            session_handoff_keys=SESSION_HANDOFF_KEYS)
        self.chat.return_value.complete.side_effect = AssertionError("models are forbidden in this test")
        self.real_page_identity = YudaoIdentityPort.page_identity

        def actor(token):
            if token not in ("synthetic-a", "synthetic-a-new", "synthetic-b"):
                raise PrincipalError("not signed in")
            return {"id": "2" if token == "synthetic-b" else "1", "deptId": "10"}

        def page_identity(port, token):
            return port._assertion(actor(token), "page_session")

        def request(client, method, path, **kwargs):
            if method == "GET" and path == "system/dict-data/simple-list":
                actor(kwargs["token"])
                return 0, {"code": 0, "data": [{"dictType": "crm_follow_up_type", "label": "合成电话", "value": "1"}]}
            if method != "GET" or path != "system/auth/get-permission-info":
                raise AssertionError((method, path))
            return 0, {"code": 0, "data": {"user": actor(kwargs["token"])}}

        patch.object(YudaoIdentityPort, "page_identity", page_identity).start()
        patch("web_operation_agent_yudao.reads.client.YudaoClient._request", request).start()

    def host(self):
        host = YudaoHost(self.config)
        self.hosts.append(host)
        return host

    def test_two_minute_turn_deadline_reaches_model_and_executor(self):
        host = self.host()
        host.bootstrap("synthetic-a", "1", "source-only")
        session = host._sessions[("1", "1", "source-only")].session
        self.assertEqual(self.config.turn_timeout_s, 120)
        self.assertEqual(self.chat.call_args.kwargs["timeout"], 120)
        self.assertEqual(session.runner.executor.config.wall_budget_s, 120)

    def test_unavailable_business_documents_do_not_block_host_or_authenticated_session(self):
        from web_operation_agent.goal.data import DataRefused
        self.documents.side_effect = OSError("synthetic catalog outage")
        host = self.host()
        host.bootstrap("synthetic-a", "1", "source-only")
        self.documents.assert_not_called()
        with self.assertRaises(DataRefused) as failed:
            host._load_api_docs()
        self.assertTrue(failed.exception.read_failed)
        self.assertIsNone(host._api_documents)
        self.documents.side_effect = None
        self.documents.return_value = {"paths": {}, "components": {}}
        self.assertEqual(host._load_api_docs(), self.documents.return_value)
        host.bootstrap("synthetic-a-new", "1", "renewed")
        self.assertEqual(self.documents.call_count, 2)

    def test_native_source_answer_completes_without_browser_or_business_metadata(self):
        from web_operation_agent.tests.support.business import _Chat
        from web_operation_agent_yudao.rules.ui_source import UiSource
        root = Path(self.tmp.name) / "source"
        root.mkdir()
        self.config = replace(self.config, source_root=root, warm_index=False, runtime_source=True)
        path = self.config.source_root / "ui/src/utils/refund.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const batchLimit = 125;\n")
        self.documents.side_effect = AssertionError("business metadata is unavailable")
        question = "代码中的batchLimit是多少？"
        chat = _Chat([
            ("lookup_source", {"query": "batchLimit"}),
            ("finish", {"status": "done", "answer": "ignored", "source_conclusions": [
                {"kind": "source_excerpt", "evidence_ref": "source#1", "statement": "batchLimit定义为125。",
                 "support": "export const batchLimit = 125;"}]}),
        ])
        chat.model = "scripted/no-model"
        chat.egress_policy = ProviderEgressPolicy(session_handoff_keys=SESSION_HANDOFF_KEYS)
        self.chat.return_value = chat
        with patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"), patch(
                "web_operation_agent.goal.executor.RemoteBrowserClient.start",
                side_effect=AssertionError("browser transport is unavailable")):
            host = self.host()
            host.bootstrap("synthetic-a", "1", "source-only")
            session = host._sessions[("1", "1", "source-only")].session
            result = session.runner.executor.execute({"message_id": "source-question",
                "text": question, "page_url": self.config.public_origin + "/crm/customer"})
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIsNone(result.entry_observation_id)
        self.assertIsNone(session.channel.latest_observation_envelope)
        self.documents.assert_not_called()
        self.assertIn("125", result.user_answer)

    def test_user_facing_source_reading_is_off_by_default_and_explicitly_flagged_on(self):
        # D3：--source-root 服务构建期用途；用户态源码工具默认关闭，关闭时提示词
        # 要求实现类问题用固定答复说明本部署不提供源码说明。
        root = Path(self.tmp.name) / "source"
        (root / "ui/src/utils").mkdir(parents=True)
        (root / "ui/src/utils/refund.ts").write_text("export const batchLimit = 125;\n")
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, runtime_source=value)
        self.config = replace(self.config, source_root=root, warm_index=False)
        host = self.host()
        host.bootstrap("synthetic-a", "1", "tab")
        executor = next(iter(host._sessions.values())).session.runner.executor
        self.assertFalse(executor.config.source.runtime_source)
        self.assertIn("本部署不提供源码说明", executor.profile.instructions)
        self.config = replace(self.config, runtime_source=True)
        flagged = self.host()
        flagged.bootstrap("synthetic-b", "1", "tab")
        enabled = next(iter(flagged._sessions.values())).session.runner.executor
        self.assertTrue(enabled.config.source.runtime_source)
        self.assertNotIn("本部署不提供源码说明", enabled.profile.instructions)

    def test_pages_hold_their_own_sessions_and_a_renewal_keeps_its_page_s(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        second = host.bootstrap("synthetic-a", "1", "tab-b")
        # Two pages of one person are two conversations, not two owners of one.
        self.assertNotEqual(first["config"]["sessionId"], second["config"]["sessionId"])
        renewed = host.bootstrap("synthetic-a-new", "1", "tab-a")
        self.assertEqual(first["config"]["sessionId"], renewed["config"]["sessionId"])
        active = host._sessions[("1", "1", "tab-a")]
        self.assertEqual(active.client._token, "synthetic-a-new")
        self.assertEqual(active.session.channel.principal.delegation_id, active.delegation_id)
        self.assertIsNot(host._sessions[("1", "1", "tab-b")].session, active.session)
        self.assertEqual(host.server.session_count, 2)
        self.chat.return_value.complete.assert_not_called()

    def test_an_actor_s_extra_page_lets_the_least_recent_idle_one_go(self):
        for value in (0, 65, True, "8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, sessions_per_actor=value)
        self.config = replace(self.config, sessions_per_actor=1)
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        second = host.bootstrap("synthetic-a", "1", "tab-b")
        self.assertNotEqual(first["config"]["sessionId"], second["config"]["sessionId"])
        # The idle session gave its slot; the busy cap stays the actor's own problem.
        self.assertEqual(host.server.session_count, 1)
        # The page it left recovers the conversation the next time it asks: the stable
        # session id is re-derived from its key and history comes back with it.
        third = host.bootstrap("synthetic-a", "1", "tab-a")
        self.assertEqual(third["config"]["sessionId"], first["config"]["sessionId"])
        self.assertEqual(host.server.session_count, 1)

    def test_sole_loop_does_not_require_source(self):
        host = self.host()
        host.bootstrap("synthetic-a", "1", "tab")
        executor = host._sessions[("1", "1", "tab")].session.runner.executor
        self.assertIsInstance(executor, GoalTurnExecutor)
        self.assertFalse(executor.config.source.runtime_source)
        self.assertIsNotNone(executor.goal_port)
        self.assertIn("submit", executor.config.disabled_actions)
        self.assertNotIn("goto", executor.config.disabled_actions)
        policy = self.chat.call_args.kwargs["egress_policy"]
        self.assertTrue({k.casefold() for k in SESSION_HANDOFF_KEYS} <= policy.handoffs.query_keys)
        self.chat.return_value.complete.assert_not_called()

    def test_explicit_page_scope_reaches_host_review_goal_contract_and_model_tools(self):
        self.config = replace(self.config,
            native_page_disabled_handlers=frozenset({'query', 'paging', 'fields', 'lines'}),
            native_edit_prefill=False)
        host = self.host()
        host.bootstrap('synthetic-a', '1', 'scoped-tab')
        executor = host._sessions[('1', '1', 'scoped-tab')].session.runner.executor
        self.assertEqual(host.page_review.disabled_handlers, self.config.native_page_disabled_handlers)
        self.assertIs(executor.goal_port.review, host.page_review)
        self.assertFalse(host.page_review.allow_edit_prefill)
        self.assertFalse(executor.goal_port.capabilities() & {'facets', 'paging', 'fill', 'lines'})
        procedures = {p.schema['function']['name']: p for p in executor.profile.procedures}
        self.assertFalse(procedures.keys() & {'input', 'select', 'fill_fields'})
        self.assertIn('call_api', procedures)
        self.assertFalse(procedures['call_api'].required_capabilities)
        self.assertIn('本部署关闭', executor.profile.instructions)
        self.chat.return_value.complete.assert_not_called()

    def test_optional_method_fence_selects_page_policy_and_blocks_every_scanned_write_get(self):
        from web_operation_agent_yudao.executor.method_fenced_actions import MethodFencedPageActions
        from web_operation_agent_yudao.host.page_guard import GuardRefused

        with self.assertRaises(ValueError):
            replace(self.config, native_page_admission="all-actions")
        writing_get = "/admin-api/fixture/initializing-list"
        self.config = self._fence(writing_gets=[writing_get],
                                  scan={"reasons": {writing_get: "FixtureMapper.insert"}})
        self.assertNotIn(writing_get, self.host()._fenced)
        self.config = replace(self.config, native_page_admission="method-fence",
                              native_page_disabled_handlers=frozenset({"paging"}))
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy") as proxy:
            host = self.host()
            host.bootstrap("synthetic-a", "1", "method-fence-tab")
        executor = host._sessions[("1", "1", "method-fence-tab")].session.runner.executor
        self.assertIsInstance(executor.config.action_policy, MethodFencedPageActions)
        self.assertFalse(host.page_review.permits("query", "/crm/customer"))
        self.assertTrue(executor.goal_port.review.permits("query", "/crm/customer"))
        self.assertFalse(executor.goal_port.review.permits("paging", "/crm/customer"))
        self.assertIn("submit", executor.config.disabled_actions)
        guard = proxy.call_args.kwargs["guard"]
        actor = "a" * 64
        guard.begin(actor, "b" * 64)
        with guard.request(actor, "GET", "/admin-api/fixture/ordinary-list"):
            pass
        for method, path in (("GET", writing_get), ("POST", "/admin-api/fixture/save")):
            with self.assertRaisesRegex(GuardRefused, "page_write_blocked"), guard.request(actor, method, path):
                self.fail("write reached dispatch")
        self.chat.return_value.complete.assert_not_called()

    def test_read_only_window_uses_live_fence_without_route_or_handler_gates(self):
        from web_operation_agent_yudao.host.page_guard import NativePageGuard
        writing_get="/admin-api/fixture/initializing-list"
        self.config=replace(self._fence(writing_gets=[writing_get],
            scan={"reasons":{writing_get:"FixtureMapper.insert"}}),
            native_page_admission="read-only-window",location_bindings=True,
            native_page_disabled_handlers=frozenset({"query","paging","fields","lines"}))
        with self.assertRaises(ValueError):
            replace(self.config,native_writes=True)
        guard=NativePageGuard(frozenset(),writing_gets=frozenset({writing_get}))
        proxy=RealGuardProxy(upstream="http://127.0.0.1:9",origin="https://fixture.test",
            guard=guard,resolve_actor=lambda *_:"a"*64).start()
        self.addCleanup(proxy.close)
        proxy.ingress_base=proxy.url
        proxy.verify_ingress()
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy",return_value=proxy):
            host=self.host()
            host.bootstrap("synthetic-a","1","isolated-tab")
        executor=host._sessions[("1","1","isolated-tab")].session.runner.executor
        self.assertIsInstance(executor.config.action_policy,ReadOnlyWindowPageActions)
        self.assertIs(executor.config.action_policy.guard_proxy,proxy)
        self.assertIn(writing_get,host._fenced)
        self.assertFalse(host.page_review.permits("query","/unregistered"))
        for handler in ("query","paging","fields","lines"):
            self.assertTrue(executor.goal_port.review.permits(handler,"/unregistered","unknown"))
        names={p.schema["function"]["name"] for p in executor.profile.procedures}
        self.assertTrue({"goto","fill_fields","filter_page","open_record"}<=names)
        self.chat.return_value.complete.assert_not_called()

    def test_normal_host_budget_and_write_opt_in_reach_the_real_executor(self):
        from web_operation_agent_yudao.host.page_guard import NativePageGuard
        self.config = replace(self.config, native_page_admission="read-only-window", native_writes=True,
                              native_edit_prefill=False)
        proxy = RealGuardProxy(upstream="http://127.0.0.1:9", origin="https://fixture.test",
            guard=NativePageGuard(frozenset(), writing_gets=frozenset()), resolve_actor=lambda *_: "a" * 64).start()
        self.addCleanup(proxy.close)
        proxy.ingress_base = proxy.url
        proxy.verify_ingress()
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy", return_value=proxy):
            host = self.host()
            host.bootstrap("synthetic-a", "1", "normal-host-defaults")
        executor = host._sessions[("1", "1", "normal-host-defaults")].session.runner.executor
        self.assertEqual(executor.model_steps, 33)
        self.assertEqual(executor.config.step_budget, 33)
        self.assertEqual(executor.config.wall_budget_s, 120)
        self.assertEqual(self.chat.call_args.args[0].run_input_token_abort, 16_000_000)
        self.assertTrue(host.config.native_submit)
        self.assertTrue(host.config.native_edit_prefill)
        self.assertIsNotNone(executor.native_submit)
        self.assertTrue(callable(executor.procedures.page_reads))
        self.chat.return_value.complete.assert_not_called()

    def test_native_read_turn_uses_only_table_tools_and_item_delivery(self):
        host = self.host()
        host.bootstrap("synthetic-a", "1", "tab")
        executor = host._sessions[("1", "1", "tab")].session.runner.executor
        tools = {t["function"]["name"]: t["function"]["parameters"]
                 for t in executor._tools({})}
        self.assertTrue({"tables", "sql", "date_range"} <= tools.keys())
        self.assertNotIn("summary", tools["call_api"]["properties"])
        self.assertNotIn("deliver", tools["call_api"]["properties"])
        self.assertNotIn("read_answer", tools["finish"]["properties"])
        self.assertIn("items", tools["finish"]["required"])
        self.assertIn("kind", tools["finish"]["properties"]["items"]["items"]["required"])
        # The untraced-number closing check is wired for row-table turns too
        # (v1.10): the profile hook is present on every product executor.
        self.assertIsNotNone(executor.profile.untraced_numbers)
        self.assertNotIn("call_api.summary", executor.profile.instructions)
        self.assertIn("date_range", executor.profile.instructions)

    def test_native_slim_can_deliver_a_complete_long_roster_without_a_smaller_hidden_text_cap(self):
        host = self.host()
        host.bootstrap("synthetic-a", "1", "tab")
        executor = host._sessions[("1", "1", "tab")].session.runner.executor
        long_answer = "\n".join(f"{i}. Synthetic contact full name {i:03d}" for i in range(180))
        declared = executor._finish_tool({})["function"]["parameters"]["properties"]["answer"]["maxLength"]
        self.assertLessEqual(len(long_answer), declared)
        from web_operation_agent.transport.panel_view import panel_limits
        self.assertEqual(panel_limits()["result_max_chars"], executor.config.max_user_answer_chars)
        self.assertLess(declared, executor.config.max_user_answer_chars)

    def test_turn_records_are_on_by_default_bounded_and_pruned_at_start(self):
        self.assertEqual(14, self.config.turn_trace_days)
        for value in (-1, 91, True, "14", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, turn_trace_days=value)
        session = Path(self.tmp.name) / "runs" / "20261001-000000-abcdef12-000000"
        (session / "turns").mkdir(parents=True)
        old, audit = session / "turns" / "0001.json", session / "audit.jsonl"
        for path in (old, audit):
            path.write_text("{}")
            os.utime(path, (time.time() - 15 * 86400,) * 2)
        host = self.host()
        self.assertEqual((False, True), (old.exists(), audit.exists()))
        host.bootstrap("synthetic-a", "1", "tab")
        self.assertTrue(host._sessions[("1", "1", "tab")].session.runner.executor.turn_traces)
        host.close()
        self.config = replace(self.config, turn_trace_days=0)
        off = self.host()
        off.bootstrap("synthetic-b", "1", "tab")
        self.assertFalse(off._sessions[("1", "2", "tab")].session.runner.executor.turn_traces)

    def test_read_only_window_can_prepare_without_binding_or_dispatch(self):
        from web_operation_agent_yudao.host.page_guard import NativePageGuard
        self.config = replace(self.config, native_page_admission="read-only-window",
                              native_write_preparation=True, location_bindings=False)
        guard = NativePageGuard(frozenset(), writing_gets=frozenset())
        proxy = RealGuardProxy(upstream="http://127.0.0.1:9", origin="https://fixture.test",
                               guard=guard, resolve_actor=lambda *_: "a" * 64).start()
        self.addCleanup(proxy.close)
        proxy.ingress_base = proxy.url
        proxy.verify_ingress()
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy", return_value=proxy):
            host = self.host()
            host.bootstrap("synthetic-a", "1", "preparation-window")
        executor = host._sessions[("1", "1", "preparation-window")].session.runner.executor
        self.assertIsInstance(executor.config.action_policy, ReadOnlyWindowPageActions)
        self.assertTrue(executor.application.business_tools.mutation_tool_names)
        self.assertFalse(host.config.native_writes)
        names = {p.schema["function"]["name"] for p in executor.profile.procedures}
        self.assertTrue({"fill_fields", "highlight"} <= names)
        self.assertNotIn("filter_page", names)
        self.assertIsNotNone(executor.profile.draft_steps)
        self.assertIsNotNone(executor.profile.mutation_prefill)
        enabled = replace(self.config, native_write_preparation=False, native_writes=True)
        self.assertTrue(enabled.native_submit)
        self.assertTrue(enabled.native_edit_prefill)
        self.chat.return_value.complete.assert_not_called()

    def test_preparation_mode_offers_existing_tools_but_never_enables_native_writes(self):
        self.config = replace(self.config, native_write_preparation=True)
        host = self.host()
        host.bootstrap("synthetic-a", "1", "preparation-host")
        executor = host._sessions[("1", "1", "preparation-host")].session.runner.executor
        self.assertFalse(host.config.native_writes)
        self.assertTrue(executor.config.allow_business_mutations)
        self.assertTrue(executor.application.business_tools.mutation_tool_names)
        self.assertIn("submit", executor.config.disabled_actions)
        for value in (True, 1, "true"):
            with self.assertRaises(ValueError):
                replace(self.config, native_writes=value)
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                replace(self.config, native_write_preparation=value)
        self.chat.return_value.complete.assert_not_called()

    def test_native_writes_require_explicit_host_enablement_and_keep_browser_submission_disabled(self):
        self.assertFalse(self.config.native_writes)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, native_writes=value)
        self.config = replace(self.config, native_writes=True)
        host = self.host()
        host.bootstrap("synthetic-a", "1", "skill-write-host")
        active = host._sessions[("1", "1", "skill-write-host")]
        executor = active.session.runner.executor
        self.assertIsInstance(executor, GoalTurnExecutor)
        self.assertTrue(executor.config.allow_business_mutations)
        self.assertIn("submit", executor.config.disabled_actions)
        self.assertEqual(executor.application.business_tools.mutation_tool_names, frozenset({
            "crm_customer_create", "crm_customer_update", "crm_contact_create", "crm_contact_update",
            "crm_follow_up_create", "erp_supplier_create", "erp_supplier_update",
            "erp_warehouse_create", "erp_warehouse_update", "erp_customer_create", "erp_customer_update",
            "system_post_create", "system_post_update", "member_tag_create", "member_group_create", "bpm_category_create",
            "crm_clue_create", "crm_clue_update", "erp_product_create", "erp_product_update",
            "erp_product_unit_create", "erp_product_unit_update", "erp_account_create", "erp_account_update",
            "erp_document_status", "promotion_coupon_template_status", "promotion_seckill_activity_close",
            "system_dept_create", "system_dept_update", "product_brand_create", "product_brand_update", "product_category_create",
            "crm_customer_put_pool", "crm_customer_receive", "crm_customer_lock", "crm_customer_deal_status",
            "crm_receivable_plan_create", "update_record", "toggle_record_status"}))
        # Required dictionary choices are absent in this actor fixture: these
        # operations must not become malformed schemas or guessed numeric inputs.
        self.assertNotIn("system_notice_create", executor.application.business_tools.mutation_tool_names)
        self.assertNotIn("oa_announcement_create", executor.application.business_tools.mutation_tool_names)
        follow = executor.application.ports.operations.tools.get("crm_follow_up_create")
        self.assertEqual(follow.schema["function"]["parameters"]["properties"]["type"]["enum"], ["合成电话"])
        self.assertIn("yudao.write", executor.application.ports.identity.required_scopes)
        self.assertTrue((self.config.state_dir / "mutations.sqlite3").exists())
        self.documents.assert_not_called()
        self.chat.return_value.complete.assert_not_called()

    def test_removed_config_fields_are_not_accepted(self):
        path = Path(self.tmp.name) / "old-policy.json"
        for changes in ({"executor_mode": "goal"}, {"native_form_policy": path},
                        {"native_create_policies": (path,)}, {"native_create_class": path},
                        {"native_filter_policies": (path,)}, {"native_action_effect_review": "old-review"},
                        {"native_navigation": True}, {"auto_read_catalog": True},
                        {"native_live_forms": False}, {"page_reach_steps": 8}):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(self.config, **changes)

    def test_bootstrap_and_renewal_leave_navigation_to_the_application_document(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        renewed = host.bootstrap("synthetic-a-new", "1", "tab-a")
        # A synthetic popstate with the Runtime's own state discards Vue Router's
        # current path; its next ordinary push then falls back to origin + undefined.
        for response in (first, renewed):
            self.assertEqual(response["config"]["navigationMode"], "document")
        self.assertEqual(host._sessions[("1", "1", "tab-a")].session.navigation_mode, "document")
        self.chat.return_value.complete.assert_not_called()

    def test_actors_and_tenants_get_separate_sessions_and_tokens_never_reach_disk(self):
        host = self.host()
        responses = [host.bootstrap(token, tenant, "tab") for token, tenant in (
            ("synthetic-a", "1"), ("synthetic-b", "1"), ("synthetic-a", "2"))]
        self.assertEqual(len({r["config"]["sessionId"] for r in responses}), 3)
        self.assertEqual(host.server.session_count, 3)
        host.close()
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"synthetic-a", path.read_bytes())
                self.assertNotIn(b"synthetic-b", path.read_bytes())

    def test_restart_recovers_history_with_a_new_capability_without_replaying_a_turn(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab")
        session = host._sessions[("1", "1", "tab")].session
        session.channel.publish("progress", {"state": "finished", "message": "synthetic previous answer"}, turn_id="previous")
        host.close()
        restored = self.host()
        second = restored.bootstrap("synthetic-a", "1", "tab")
        recovered = restored._sessions[("1", "1", "tab")].session
        self.assertEqual(first["config"]["sessionId"], second["config"]["sessionId"])
        self.assertNotEqual(first["config"]["clientToken"], second["config"]["clientToken"])
        self.assertTrue(recovered.channel.recovered_history)
        principal = recovered.channel.principal
        lease = recovered.channel.accept_hello(ClientHello(
            schema_version=1, session_id=recovered.grant.session_id,
            client_id="tab", runtime="test/1", runtime_instance_id="restored-document",
            capabilities=("dom.observe",), page_origin=self.config.public_origin,
            application_id=principal.application_id, principal_digest=principal.binding_digest))["lease_id"]
        self.assertIn("synthetic previous answer", json.dumps(
            recovered.channel.panel_timeline(lease_id=lease)))
        self.chat.return_value.complete.assert_not_called()

    def test_invalid_bootstrap_does_not_create_a_session(self):
        host = self.host()
        for token, tenant, instance in (("", "1", "tab"), ("invalid", "1", "tab"),
                                        ("synthetic-a", "bad", "tab"), ("synthetic-a", "1", "")):
            with self.subTest(token=token), self.assertRaises(BootstrapRefused):
                host.bootstrap(token, tenant, instance)
        self.assertEqual(host.server.session_count, 0)
        self.chat.assert_not_called()

    def test_expired_token_during_identity_binding_is_a_bootstrap_401(self):
        host = self.host()
        for code, expected in ((401, (401, "sign_in_required")),
                               (403, (403, "access_denied"))):
            with self.subTest(code=code), patch.object(
                    YudaoIdentityPort, "bind",
                    side_effect=YudaoReadRefused("synthetic refusal", code=code)):
                with self.assertRaises(BootstrapRefused) as refused:
                    host.bootstrap("synthetic-a", "1", "tab")
                self.assertEqual((refused.exception.status, refused.exception.code), expected)
        self.assertEqual(host.server.session_count, 0)
        with patch.object(YudaoIdentityPort, "bind",
                          side_effect=YudaoReadRefused("synthetic outage", code=503)):
            with self.assertRaises(YudaoReadRefused):
                host.bootstrap("synthetic-a", "1", "tab")
        self.assertEqual(host.server.session_count, 0)

    def test_expired_token_precedes_write_catalog_and_is_not_reported_as_503(self):
        self.config = replace(self.config, native_writes=True)
        host = self.host()
        with patch.object(YudaoIdentityPort, "page_identity", self.real_page_identity), \
                patch.object(YudaoClient, "_request",
                             return_value=(401, {"code": 401})) as request:
            with self.assertRaises(BootstrapRefused) as refused:
                host.bootstrap("synthetic-a", "1", "tab")
        self.assertEqual((refused.exception.status, refused.exception.code),
                         (401, "sign_in_required"))
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         ["system/auth/get-permission-info"])
        self.assertEqual(host.server.session_count, 0)

        # The token can expire between the identity read and the catalog read.
        with patch.object(YudaoClient, "_request", return_value=(401, {"code": 401})):
            with self.assertRaises(BootstrapRefused) as refused:
                host.bootstrap("synthetic-a", "1", "tab")
        self.assertEqual((refused.exception.status, refused.exception.code),
                         (401, "sign_in_required"))
        self.assertEqual(host.server.session_count, 0)

    def test_expired_token_during_existing_session_renewal_is_a_bootstrap_401(self):
        host = self.host()
        host.bootstrap("synthetic-a", "1", "tab")
        active = host._sessions[("1", "1", "tab")]
        with patch.object(YudaoIdentityPort, "bind",
                          side_effect=YudaoReadRefused("synthetic expired", code=401)):
            with self.assertRaises(BootstrapRefused) as refused:
                host.bootstrap("synthetic-a-new", "1", "tab")
        self.assertEqual((refused.exception.status, refused.exception.code),
                         (401, "sign_in_required"))
        self.assertIs(host._sessions[("1", "1", "tab")], active)
        self.assertEqual(host.server.session_count, 1)

    def test_page_identity_preserves_application_auth_and_availability_results(self):
        client = YudaoClient.for_token("http://yudao.invalid", "server-token", "1")
        port = YudaoIdentityPort("http://yudao.invalid", client)
        def response(_, method, path, **kwargs):
            self.assertEqual((method, path, kwargs["token"]),
                             ("GET", "system/auth/get-permission-info", "browser-token"))
            return 0, {"code": 0, "data": {"user": {"id": 9, "deptId": 2}}}
        with patch.object(YudaoClient, "_request", response):
            self.assertEqual(self.real_page_identity(port, "browser-token").actor_id, "9")
        for code, error in ((401, PrincipalError), (403, YudaoReadRefused),
                            (503, YudaoReadRefused)):
            with self.subTest(code=code), patch.object(
                    YudaoClient, "_request", return_value=(code, {"code": code})):
                with self.assertRaises(error):
                    self.real_page_identity(port, "browser-token")
        with patch.object(YudaoClient, "_request",
                          side_effect=DataRefused("synthetic outage", read_failed=True)):
            with self.assertRaises(DataRefused):
                self.real_page_identity(port, "browser-token")

    def _fence(self, **changes):
        policy = Path(self.tmp.name) / f"native-fence-{len(list(Path(self.tmp.name).glob('native-fence-*')))}.json"
        policy.write_text(json.dumps({**{
            "schema_version": 2, "release": "fixture-release", "mode": "method-fence",
            "writing_gets": ["/admin-api/pms/pm/project-group/list"], "scan": {"get_handlers": 3}}, **changes}))
        return replace(self.config, native_read_policy=policy, native_guard_port=0,
                       source_release="fixture-release")

    def test_location_bindings_are_explicit_and_reach_the_runtime_and_real_profile(self):
        from web_operation_agent_yudao.rules.ui_source import UiSource
        from web_operation_agent_yudao.reads.location import LocationProcedures
        self.config = replace(self._fence(), location_bindings=True)
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"), \
                patch("web_operation_agent_yudao.host.page_guard.NativePageGuard"), \
                patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource.none()):
            host = self.host()
            bootstrap = host.bootstrap("synthetic-a", "1", "native-tab")
            self.assertTrue(bootstrap["config"]["pageControllerModule"].endswith("?bindings=1"))
            executor = host._sessions[("1", "1", "native-tab")].session.runner.executor
            self.assertIsInstance(executor.procedures, LocationProcedures)
            names = {p.schema["function"]["name"] for p in executor.profile.procedures}
            self.assertIn("filter_page", names)
            self.assertIn("fill_fields", names)
        with self.assertRaises(ValueError):
            replace(self.config, native_writes=True)

    def test_method_fence_binds_live_forms_inside_the_applications_window(self):
        from web_operation_agent_yudao.rules.ui_source import UiSource
        from web_operation_agent_yudao.reads.live_filters import LivePageFilters
        from web_operation_agent_yudao.writes.live_forms import LiveFormPort
        from web_operation_agent_yudao.goal_compat.live_goals import LiveFormsGoalPort
        self.config = self._fence()
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy") as proxy, \
                patch("web_operation_agent_yudao.host.page_guard.NativePageGuard") as guard, \
                patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource.none()):
            host = self.host()
            guard.assert_called_once()
            self.assertEqual(guard.call_args.args, (frozenset(),))
            self.assertEqual(guard.call_args.kwargs["writing_gets"],
                             frozenset({"/admin-api/pms/pm/project-group/list"}))
            proxy.return_value.start.return_value.verify_ingress.assert_called_once()
            # The window is opened only when the application confirms its own.
            self.assertIs(proxy.call_args.kwargs["application_window"], True)
            bootstrap = host.bootstrap("synthetic-a", "1", "native-tab")
            from web_operation_agent_yudao.host.guard_proxy import GUARD_PATH
            self.assertEqual(bootstrap["config"]["pageWriteGuard"], GUARD_PATH)
            self.assertTrue(bootstrap["config"]["pageControllerModule"].endswith("/woa/yudao-page-controller.js"))
            executor = host._sessions[("1", "1", "native-tab")].session.runner.executor
            self.assertIsInstance(executor.goal_port._goal_port, LiveFormsGoalPort)
            self.assertIsInstance(executor.goal_port.page_filters, LivePageFilters)
            self.assertIsInstance(executor.application.ports.forms, LiveFormPort)
            self.assertNotIn("fill", executor.goal_port.capabilities())  # no form handlers in this review
            # Inputs are allowed so that a draft can be prepared; submitting never is.
            self.assertNotIn("fill", executor.disabled_page_actions)
            self.assertNotIn("select", executor.disabled_page_actions)
            self.assertIn("submit", executor.disabled_page_actions)
        self.chat.return_value.complete.assert_not_called()

    def test_the_application_index_is_built_ahead_only_where_the_host_asks_for_it(self):
        import threading
        from types import SimpleNamespace
        built, read, done = [], [], threading.Event()

        def warm(app, knowledge):
            built.append(app)
            read.append(knowledge)
            done.set()
        with patch.object(YudaoHost, "_warm_index", staticmethod(warm)):
            self.host().bootstrap("synthetic-a", "1", "plain")
            self.assertFalse(done.wait(.2))            # explicitly disabled: nothing is read ahead
            self.config = replace(self.config, warm_index=True, state_dir=Path(self.tmp.name) / "warm")
            host = self.host()
            host.bootstrap("synthetic-a", "1", "warm")
            self.assertTrue(done.wait(5))
            # The index built is the one this actor's turns will read.
            executor = host._sessions[("1", "1", "warm")].session.runner.executor
            self.assertIs(built[0].goal_port, executor.goal_port._goal_port)
            # The sole loop warms its own menu/API knowledge as well.
            self.assertIs(read[0], executor.knowledge)
            host.bootstrap("synthetic-a", "1", "warm")             # the same session: not built again
            self.assertEqual(len(built), 1)
        # What a slim turn searches is read first, then the outline its lookups read, not
        # the whole index; one that fails ahead of time is not an error of the session and
        # does not keep the other from being built.
        calls = []
        away = lambda: (_ for _ in ()).throw(OSError("the application is away"))  # noqa: E731
        index = SimpleNamespace(goal_port=SimpleNamespace(index=lambda: calls.append("index"),
                                                          outline_index=lambda: calls.append("outline")))
        YudaoHost._warm_index(index, SimpleNamespace(_load=lambda: calls.append("menu")))
        YudaoHost._warm_index(index, SimpleNamespace(_load=away))
        YudaoHost._warm_index(SimpleNamespace(goal_port=SimpleNamespace(index=away, outline_index=away)),
                              SimpleNamespace(_load=lambda: calls.append("menu")))
        self.assertEqual(calls, ["menu", "outline", "outline", "menu"])
        with self.assertRaises(ValueError):
            replace(self.config, warm_index=None)
        self.chat.return_value.complete.assert_not_called()

    def _page_policy(self):
        path = Path(self.tmp.name) / "pages.json"
        path.write_text(json.dumps({"schema_version": 1, "release": "synthetic-release", "review": "synthetic-handler-review",
            "handlers": [{"kind": "navigation", "route": "/crm/customer", "evidence": "synthetic/menu", "sha256": "a" * 64, "controls": []}]}))
        # _fence supplies the synthetic release used by the host fixture.
        document = json.loads(path.read_text())
        document["release"] = self._fence().source_release
        path.write_text(json.dumps(document))
        return path

    def test_default_is_shared_loop_and_warmup_can_be_disabled(self):
        from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
        from web_operation_agent_yudao.executor.slim_loop import TableReadTurnExecutor
        from web_operation_agent_yudao.executor.model_page_policy import ModelPageGoalPort
        self.config = replace(self._fence(), native_page_policy=self._page_policy(),
                               ui_source=None, source_root=None)
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"), \
                patch.object(YudaoHost, "_warm_index") as warm:
            host = self.host()
            host.bootstrap("synthetic-a", "1", "default")
            executor = host._sessions[("1", "1", "default")].session.runner.executor
            self.assertIs(type(executor), TableReadTurnExecutor)
            self.assertIs(executor._loop.__func__, ModelDrivenTurnExecutor._loop)
            self.assertIsInstance(executor.goal_port, ModelPageGoalPort)
            self.assertNotIn("source", executor.goal_port.capabilities())
            self.assertFalse(executor.config.allow_business_mutations)
            self.assertIn("submit", executor.disabled_page_actions)
            warm.assert_not_called()

    def test_invalid_default_deployment_fails_before_state_source_proxy_or_model(self):
        valid = replace(self._fence(), native_page_policy=self._page_policy())
        missing = Path(self.tmp.name) / "does-not-exist.json"
        wrong = Path(self.tmp.name) / "wrong-pages.json"
        doc = json.loads(valid.native_page_policy.read_text()); doc["release"] = "wrong"
        wrong.write_text(json.dumps(doc))
        variants = [dict(native_read_policy=None), dict(native_read_policy=missing),
            dict(native_page_policy=None), dict(native_page_policy=missing), dict(native_page_policy=wrong),
            dict(source_release="wrong"), dict(native_money_policy=missing)]
        for number, changes in enumerate(variants):
            with self.subTest(changes=changes), patch("web_operation_agent_yudao.host.native_server.UiSource.read") as source, \
                    patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy") as proxy:
                directory = Path(self.tmp.name) / f"invalid-{number}"
                with self.assertRaises(ValueError):
                    YudaoHost(replace(valid, state_dir=directory, **changes))
                self.assertFalse(directory.exists())
                source.assert_not_called(); proxy.assert_not_called(); self.chat.assert_not_called()

    def test_the_slim_turn_proposes_reviewed_writes_only_where_the_host_enables_them(self):
        from web_operation_agent_yudao.executor.slim_loop import SlimTurnExecutor
        from web_operation_agent_yudao.rules.ui_source import UiSource
        for enabled in (False, True):
            with self.subTest(native_writes=enabled), \
                    patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"), \
                    patch("web_operation_agent_yudao.host.page_guard.NativePageGuard"), \
                    patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource.none()):
                self.config = replace(self._fence(), native_page_policy=self._page_policy(), native_writes=enabled,
                                      state_dir=Path(self.tmp.name) / str(enabled))
                host = self.host()
                host.bootstrap("synthetic-a", "1", "slim-tab")
                executor = host._sessions[("1", "1", "slim-tab")].session.runner.executor
                self.assertIsInstance(executor, SlimTurnExecutor)
                self.assertIs(executor.config.allow_business_mutations, enabled)
                self.assertTrue(executor.skill_knowledge)
                # The page's own submit control is never pressed, with or without writes.
                self.assertIn("submit", executor.disabled_page_actions)
                self.assertEqual(bool(executor.application.business_tools.mutation_tool_names), enabled)
                self.assertEqual((self.config.state_dir / "mutations.sqlite3").exists(), enabled)
        self.assertFalse(replace(self.config, slim_skill_knowledge=False).slim_skill_knowledge)
        with self.assertRaises(ValueError):
            replace(self.config, slim_skill_knowledge=None)
        # Where the host reads ahead, a slim turn's own menu and API description are read too.
        import threading
        read, done = [], threading.Event()
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"), \
                patch("web_operation_agent_yudao.host.page_guard.NativePageGuard"), \
                patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource.none()), \
                patch.object(YudaoHost, "_warm_index",
                             staticmethod(lambda app, knowledge: (read.append(knowledge), done.set()))):
            self.config = replace(self._fence(), native_page_policy=self._page_policy(), warm_index=True,
                                  state_dir=Path(self.tmp.name) / "ahead")
            host = self.host()
            host.bootstrap("synthetic-a", "1", "slim-tab")
            self.assertTrue(done.wait(5))
            self.assertIs(read[0], host._sessions[("1", "1", "slim-tab")].session.runner.executor.knowledge)
        self.chat.return_value.complete.assert_not_called()

    def test_inside_the_applications_window_only_reads_it_cannot_refuse_stay_fenced(self):
        from web_operation_agent_yudao.rules.ui_source import UiSource
        gets = ["/admin-api/pms/pm/project-group/list", "/admin-api/infra/redis/get-monitor-info",
                "/admin-api/oa/untraced/get"]
        scan = {"get_handlers": 3, "reasons": {
            gets[0]: "PmsProjectGroupController.getProjectGroupList -> PmsProjectGroupMapper.insertBatch",
            gets[1]: "RedisController.getRedisMonitorInfo -> StringRedisTemplate.execute (unresolved)"}}
        self.config = self._fence(writing_gets=gets, scan=scan)
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"), \
                patch("web_operation_agent_yudao.host.page_guard.NativePageGuard") as guard, \
                patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource.none()):
            self.host()
            self.assertEqual(guard.call_args.kwargs["writing_gets"], frozenset(gets[1:]))

    def test_method_fence_is_exact(self):
        reviewed = Path(self.tmp.name) / "reviewed-reads.json"
        reviewed.write_text(json.dumps({"schema_version": 1, "release": "fixture-release", "read_paths": []}))
        invalid = [self._fence(release="another-release"), self._fence(mode="allow-all"),
                   self._fence(writing_gets="/admin-api/x"), self._fence(read_paths=[]),
                   replace(self._fence(), source_release=""),
                   replace(self._fence(), native_read_policy=reviewed)]
        with patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy"):
            for config in invalid:
                self.config = config
                with self.subTest(policy=config.native_read_policy.read_text()[:80]), \
                        self.assertRaises(ValueError):
                    self.host()
    def test_failed_native_ingress_probe_prevents_host_startup(self):
        from web_operation_agent_yudao.rules.ui_source import UiSource
        from web_operation_agent_yudao.host.page_guard import GuardRefused
        with patch("web_operation_agent_yudao.host.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy") as proxy:
            proxy.return_value.start.return_value.verify_ingress.side_effect = GuardRefused("guard_ingress_unverified")
            with self.assertRaises(GuardRefused): self.host()
            proxy.return_value.start.return_value.close.assert_called_once()
            self.chat.assert_not_called()


class NativeCliTests(unittest.TestCase):
    def test_policy_diagnostic_precedes_model_and_network(self):
        import contextlib
        import io
        from web_operation_agent_yudao.host import native_server as native
        with tempfile.TemporaryDirectory() as directory, patch.dict(native.os.environ, {}, clear=True), \
                patch.object(native, "YudaoHost") as host:
            error = io.StringIO()
            with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as failed:
                native.main(["--api-base", "http://synthetic.invalid", "--public-origin", "http://synthetic.invalid",
                             "--state-dir", directory + "/not-created"])
            self.assertEqual(2, failed.exception.code)
            self.assertIn("--native-read-policy", error.getvalue())
            self.assertNotIn("API_KEY", error.getvalue())
            self.assertFalse(Path(directory, "not-created").exists())
            host.assert_not_called()

    def test_removed_cli_options_are_rejected_before_startup(self):
        import contextlib
        import io
        from web_operation_agent_yudao.host import native_server as native
        base = ["--api-base", "http://synthetic.invalid", "--public-origin", "http://synthetic.invalid",
                "--state-dir", "/synthetic/unused"]
        for option, value in (("--executor", "slim"), ("--native-form-policy", "old.json"),
                              ("--native-create-policy", "old.json"), ("--native-create-class", "old.json"),
                              ("--native-filter-policy", "old.json"), ("--native-navigation", None),
                              ("--native-action-effect-review", "old"), ("--auto-read-catalog", None),
                              ("--no-native-live-forms", None), ("--warm-index", None),
                              ("--no-warm-index", None)):
            with self.subTest(option=option), patch.object(native, "YudaoHost") as host:
                error = io.StringIO()
                with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as failed:
                    native.main(base + [option] + ([value] if value else []))
                self.assertEqual(2, failed.exception.code)
                self.assertIn("unrecognized arguments", error.getvalue())
                host.assert_not_called()

    def test_real_main_defaults_and_process_metadata_warmup(self):
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        from web_operation_agent_yudao.host import native_server as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fence = root / "fence.json"
            fence.write_text(json.dumps({"schema_version": 2, "release": "synthetic", "mode": "method-fence",
                                         "writing_gets": [], "scan": {}}))
            pages = root / "pages.json"
            pages.write_text(json.dumps({"schema_version": 1, "release": "synthetic", "review": "synthetic",
                "handlers": [{"kind": "navigation", "route": "/contacts", "controls": [],
                              "evidence": "synthetic", "sha256": "a"*64}]}))
            host = Mock()
            host.server.base_url = "http://synthetic.invalid"
            def compose(config):
                host.config = config
                return host
            def thread(**kwargs):
                return NS(start=lambda: kwargs["target"]())
            args = ["--api-base", "http://synthetic.invalid", "--public-origin", "http://synthetic.invalid",
                    "--state-dir", directory, "--source-release", "synthetic", "--native-read-policy", str(fence),
                    "--native-page-policy", str(pages)]
            with patch.dict(native.os.environ, {"AGENT_OPENAI_API_KEY": "synthetic-unused",
                    "AGENT_OPENAI_MODEL": "scripted/no-model"}, clear=True), \
                    patch.object(native, "YudaoHost", side_effect=compose), \
                    patch("web_operation_agent.modeling.spend.price_for", return_value={}), \
                    patch.object(native, "ThreadingHTTPServer") as http, \
                    patch.object(native.threading, "Thread", side_effect=thread), \
                    patch.object(native.threading, "Event") as event, patch.object(native.signal, "signal"):
                event.return_value.wait.side_effect = KeyboardInterrupt
                self.assertEqual(0, native.main(args))
            self.assertTrue(host.config.warm_index)
            self.assertFalse(host.config.native_writes)
            self.assertFalse(host.config.runtime_source)   # D3：用户态源码工具默认关闭
            host.server.start.assert_called_once()
            host._warm_api_docs.assert_called_once()
            http.return_value.shutdown.assert_called_once()
            host.close.assert_called_once()

    def test_the_runtime_source_flag_reaches_the_host_config(self):
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        from web_operation_agent_yudao.host import native_server as native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fence = root / "fence.json"
            fence.write_text(json.dumps({"schema_version": 2, "release": "synthetic", "mode": "method-fence",
                                         "writing_gets": [], "scan": {}}))
            pages = root / "pages.json"
            pages.write_text(json.dumps({"schema_version": 1, "release": "synthetic", "review": "synthetic",
                "handlers": [{"kind": "navigation", "route": "/contacts", "controls": [],
                              "evidence": "synthetic", "sha256": "a"*64}]}))
            host = Mock()
            host.server.base_url = "http://synthetic.invalid"
            def compose(config):
                host.config = config
                return host
            def thread(**kwargs):
                return NS(start=lambda: kwargs["target"]())
            args = ["--api-base", "http://synthetic.invalid", "--public-origin", "http://synthetic.invalid",
                    "--state-dir", directory, "--source-release", "synthetic", "--native-read-policy", str(fence),
                    "--native-page-policy", str(pages), "--runtime-source"]
            with patch.dict(native.os.environ, {"AGENT_OPENAI_API_KEY": "synthetic-unused",
                    "AGENT_OPENAI_MODEL": "scripted/no-model"}, clear=True), \
                    patch.object(native, "YudaoHost", side_effect=compose), \
                    patch("web_operation_agent.modeling.spend.price_for", return_value={}), \
                    patch.object(native, "ThreadingHTTPServer") as http, \
                    patch.object(native.threading, "Thread", side_effect=thread), \
                    patch.object(native.threading, "Event") as event, patch.object(native.signal, "signal"):
                event.return_value.wait.side_effect = KeyboardInterrupt
                self.assertEqual(0, native.main(args))
            self.assertTrue(host.config.runtime_source)


if __name__ == "__main__":
    unittest.main()
