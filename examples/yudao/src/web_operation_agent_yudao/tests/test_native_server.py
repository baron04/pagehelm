"""Host lifecycle over synthetic identity responses; no application or model calls."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from web_operation_agent.schemas import ClientHello
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent_yudao.application import SESSION_HANDOFF_KEYS, YudaoIdentityPort
from web_operation_agent_yudao.native_server import BootstrapRefused, YudaoHost, YudaoHostConfig


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hosts = []
        self.addCleanup(lambda: [h.close() for h in self.hosts])
        self.config = YudaoHostConfig(
            api_base="http://yudao.invalid", public_origin="https://yudao.invalid",
            state_dir=Path(self.tmp.name), gateway_host="127.0.0.1", gateway_port=0)
        self.addCleanup(patch.stopall)
        self.documents = patch("web_operation_agent_yudao.native_server._api_docs", return_value={}).start()
        self.chat = patch("web_operation_agent_yudao.native_server.ChatModel").start()
        self.chat.return_value.model = "scripted/no-model"
        self.chat.return_value.egress_policy = ProviderEgressPolicy(
            session_handoff_keys=SESSION_HANDOFF_KEYS)
        self.chat.return_value.complete.side_effect = AssertionError("models are forbidden in this test")

        def actor(token):
            if token not in ("synthetic-a", "synthetic-a-new", "synthetic-b"):
                raise PrincipalError("not signed in")
            return {"id": "2" if token == "synthetic-b" else "1", "deptId": "10"}

        def page_identity(port, token):
            return port._assertion(actor(token), "page_session")

        def request(client, method, path, **kwargs):
            if method != "GET" or path != "system/auth/get-permission-info":
                raise AssertionError((method, path))
            return 0, {"code": 0, "data": {"user": actor(kwargs["token"])}}

        patch.object(YudaoIdentityPort, "page_identity", page_identity).start()
        patch("web_operation_agent_yudao.client.YudaoClient._request", request).start()

    def host(self):
        host = YudaoHost(self.config)
        self.hosts.append(host)
        return host

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
        from web_operation_agent_yudao.ui_source import UiSource
        self.config = replace(self._native_form_config(update=False, create=False),
                              executor_mode="page-agent")
        path = self.config.source_root / "ui/src/utils/refund.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const batchLimit = 125;\n")
        self.documents.side_effect = AssertionError("business metadata is unavailable")
        question = "代码中的batchLimit是多少？"
        chat = _Chat([
            ("declare_task", {"action_request": "", "action_kind": "none",
                 "answer_requests": [question], "answer_kinds": ["knowledge"]}),
            ("lookup_source", {"query": "batchLimit"}),
            ("finish", {"outcome": "answered", "answer": {"version": 1,
                "coverage": "complete", "conclusions": [{"kind": "source_excerpt",
                    "evidence_ref": "source#1", "statement": "batchLimit定义为125。",
                    "support": "export const batchLimit = 125;"}],
                "answers_for": [{"request_ref": "a1", "evidence_refs": ["source#1"]}]}}),
        ])
        chat.model = "scripted/no-model"
        chat.egress_policy = ProviderEgressPolicy(session_handoff_keys=SESSION_HANDOFF_KEYS)
        self.chat.return_value = chat
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy"), patch(
                "web_operation_agent.execution.turn.RemoteBrowserClient.start",
                side_effect=AssertionError("browser transport is unavailable")):
            host = self.host()
            host.bootstrap("synthetic-a", "1", "source-only")
            session = host._sessions[("1", "1")].session
            result = session.runner.executor.execute({"message_id": "source-question",
                "text": question, "page_url": self.config.public_origin + "/crm/customer"})
        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertIsNone(result.entry_observation_id)
        self.assertIsNone(session.channel.latest_observation_envelope)
        self.documents.assert_not_called()
        self.assertIn("125", result.user_answer)


    def test_tabs_share_the_actor_s_session_and_renew_only_its_token(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        active = host._sessions[("1", "1")]
        renewed = host.bootstrap("synthetic-a-new", "1", "tab-b")
        self.assertEqual(first["config"]["sessionId"], renewed["config"]["sessionId"])
        self.assertIs(host._sessions[("1", "1")], active)
        self.assertEqual(active.client._token, "synthetic-a-new")
        self.assertEqual(active.session.channel.principal.delegation_id, active.delegation_id)
        self.assertEqual(host.server.session_count, 1)
        self.chat.return_value.complete.assert_not_called()

    def test_production_uses_deterministic_goals_without_source_as_a_prerequisite(self):
        host = self.host()
        host.bootstrap("synthetic-a", "1", "tab")
        executor = host._sessions[("1", "1")].session.runner.executor
        self.assertIsInstance(executor, GoalTurnExecutor)
        self.assertFalse(executor.config.source.runtime_source)
        self.assertIsNotNone(executor.goal_port)
        self.assertIn("submit", executor.config.disabled_actions)
        self.assertNotIn("goto", executor.config.disabled_actions)
        policy = self.chat.call_args.kwargs["egress_policy"]
        self.assertTrue({k.casefold() for k in SESSION_HANDOFF_KEYS} <= policy.handoffs.query_keys)
        self.chat.return_value.complete.assert_not_called()

    def test_page_agent_selection_is_explicit_and_requires_a_reviewed_guard(self):
        self.assertEqual("goal", self.config.executor_mode)
        for mode in ("fallback", None, True, []):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                replace(self.config, executor_mode=mode)
        with self.assertRaisesRegex(ValueError, "reviewed native read policy"):
            replace(self.config, executor_mode="page-agent")

    def test_reviewed_effect_policy_is_explicit_and_uses_the_real_session_ports(self):
        from web_operation_agent_yudao.ui_source import UiSource
        from web_operation_agent_yudao.read_actions import NativeReadActions
        self.assertEqual(self.config.native_action_effect_review, "")
        for review in (" ", [], "r" * 201):
            with self.assertRaises(ValueError):
                replace(self.config, native_action_effect_review=review)
        with self.assertRaises(ValueError):
            replace(self.config, native_action_effect_review="review")
        self.config = replace(self._native_form_config(), executor_mode="page-agent",
            native_navigation=True, native_action_effect_review="synthetic-deployment-review")
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy"):
            host = self.host()
            host.bootstrap("synthetic-a", "1", "reviewed-effects")
            executor = host._sessions[("1", "1")].session.runner.executor
            policy = executor.config.action_policy
            self.assertIsInstance(policy, NativeReadActions)
            self.assertEqual(policy.review, "synthetic-deployment-review")
            self.assertIsNone(executor.config.click_policy)
            self.assertIsNotNone(policy.form_open_actions)
            self.assertIn("inspect_data", executor.config.supported_scope)
            self.assertNotIn("Prefer resolve_goal_candidates", executor.config.supported_scope)
            self.assertIn("manual submission", executor.config.supported_scope)
            self.documents.assert_not_called()
            self.chat.return_value.complete.assert_not_called()

    def test_page_agent_reuses_the_actor_session_and_only_reviewed_form_openers(self):
        from web_operation_agent.execution.product import AgentTurnExecutor
        from web_operation_agent.schemas import ElementRef, PageObservation
        from web_operation_agent_yudao.form_entries import NativeEntryClickPolicy
        from web_operation_agent_yudao.ui_source import UiSource
        self.config = replace(self._native_form_config(), executor_mode="page-agent")
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy"):
            host = self.host()
            first = host.bootstrap("synthetic-a", "1", "page-agent")
            active = host._sessions[("1", "1")]
            executor = active.session.runner.executor
            self.assertIsInstance(executor, AgentTurnExecutor)
            self.assertTrue(executor.config.require_page_route)
            self.assertTrue(executor.config.require_task_contract)
            self.assertIs(executor.config.source, host.source)
            self.assertTrue(executor.config.source.runtime_source)
            self.assertTrue(executor.config.allow_form_prefill)
            self.assertFalse(executor.config.allow_application_entry)
            self.assertFalse(executor.config.allow_page_facets)
            self.assertTrue({"click", "fill", "select", "submit", "apply_facet"}
                            <= executor.config.disabled_actions)
            self.assertIsInstance(executor.config.click_policy, NativeEntryClickPolicy)
            page = PageObservation(1, 1, "https://yudao.invalid/crm/customer/detail/1",
                "Customer", [], [], render_state="ready", record_route="/crm/customer/detail/1")
            tab = ElementRef("e1", "tab", "Contacts", "button", 1,
                             {"id": "tab-contacts", "aria-controls": "pane-contacts"})
            self.assertEqual("", executor.config.click_policy.decide(page, tab))
            self.assertTrue(executor.config.click_policy.decide(page, replace(tab, role="button", name="编辑")))
            renewed = host.bootstrap("synthetic-a-new", "1", "another-tab")
            self.assertEqual(first["config"]["sessionId"], renewed["config"]["sessionId"])
            self.assertIs(active.session.runner.executor, executor)
            self.assertEqual("synthetic-a-new", active.client._token)
            self.chat.return_value.complete.assert_not_called()

    def test_bootstrap_and_renewal_leave_navigation_to_the_application_document(self):
        host = self.host()
        first = host.bootstrap("synthetic-a", "1", "tab-a")
        renewed = host.bootstrap("synthetic-a-new", "1", "tab-a")
        # A synthetic popstate with the Runtime's own state discards Vue Router's
        # current path; its next ordinary push then falls back to origin + undefined.
        for response in (first, renewed):
            self.assertEqual(response["config"]["navigationMode"], "document")
        self.assertEqual(host._sessions[("1", "1")].session.navigation_mode, "document")
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
        session = host._sessions[("1", "1")].session
        session.channel.publish("progress", {"state": "finished", "message": "synthetic previous answer"}, turn_id="previous")
        host.close()
        restored = self.host()
        second = restored.bootstrap("synthetic-a", "1", "tab-new")
        recovered = restored._sessions[("1", "1")].session
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

    def test_native_tabs_require_verified_ingress_and_reach_runtime_configuration(self):
        from web_operation_agent_yudao.ui_source import UiSource
        from web_operation_agent_yudao.guard_proxy import GUARD_PATH
        policy = Path(self.tmp.name) / "reviewed-reads.json"
        policy.write_text(json.dumps({"schema_version": 1, "release": "fixture-release",
            "read_paths": ["/admin-api/crm/customer/get"]}))
        self.config = replace(self.config, native_read_policy=policy, native_guard_port=0,
            ui_source=Path(self.tmp.name), source_release="fixture-release")
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy") as proxy:
            host = self.host()
            proxy.return_value.start.return_value.verify_ingress.assert_called_once()
            result = host.bootstrap("synthetic-a", "1", "native-tab")
            self.assertEqual(result["config"]["pageWriteGuard"], GUARD_PATH)
            self.assertTrue(result["config"]["pageControllerModule"].endswith("/woa/yudao-page-controller.js"))
            session = host._sessions[("1", "1")].session
            executor = session.runner.executor
            from web_operation_agent_yudao.page_goals import NativePageGoalPort
            self.assertIsInstance(executor.goal_port, NativePageGoalPort)
            self.assertIsNotNone(executor.config.click_policy)
            self.assertEqual(executor.application.descriptor.page_write_guard, GUARD_PATH)
            self.assertIn("click", executor.disabled_page_actions)
            self.assertIn("submit", executor.disabled_page_actions)
            self.assertIn("fill", executor.disabled_page_actions)

    def _native_form_config(self, *, update=True, create=True):
        import hashlib
        from web_operation_agent_yudao.form_entries import REVIEWED_FILES, CREATE_ENTRIES
        root = Path(self.tmp.name) / "source"
        paths = set(REVIEWED_FILES) | {CREATE_ENTRIES["customer-create"][4]}
        hashes = {}
        for name in paths:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<template><div /></template>")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        read = root / "reads.json"
        read.write_text(json.dumps({"schema_version": 1, "release": "fixture-release",
                                   "read_paths": ["/admin-api/crm/customer/get"]}))
        update_path = root / "update.json"
        update_path.write_text(json.dumps({"schema_version": 1, "release": "fixture-release",
                                          "hashes": {p: hashes[p] for p in REVIEWED_FILES}}))
        create_path = root / "create.json"
        create_files = (CREATE_ENTRIES["customer-create"][4], REVIEWED_FILES[1], REVIEWED_FILES[2])
        create_path.write_text(json.dumps({"schema_version": 1, "release": "fixture-release",
                                          "reference": "customer-create",
                                          "hashes": {p: hashes[p] for p in create_files}}))
        return replace(self.config, source_root=root, source_release="fixture-release",
                       ui_source=root, native_guard_port=0, native_read_policy=read,
                       native_form_policy=update_path if update else None,
                       native_create_policies=(create_path,) if create else ())

    def test_host_installs_reviewed_create_and_update_in_the_same_actor_session(self):
        from web_operation_agent_yudao.ui_source import UiSource
        from web_operation_agent_yudao.form_entries import NativeEntryVerifiers
        self.config = self._native_form_config()
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy"):
            host = self.host()
            host.bootstrap("synthetic-a", "1", "native-forms")
            executor = host._sessions[("1", "1")].session.runner.executor
            forms = executor.application.ports.forms
            self.assertIsInstance(forms._verifier, NativeEntryVerifiers)
            self.assertTrue(forms.goal_updates)
            self.assertEqual([e[0] for e in forms.goal_entries], ["customer-create"])
            self.assertTrue(all(v.client is host._sessions[("1", "1")].client
                                for v in forms._verifier.verifiers))
            self.assertTrue(executor.config.allow_form_prefill)
            self.assertIn("submit", executor.disabled_page_actions)
            self.chat.return_value.complete.assert_not_called()

    def test_create_only_policy_does_not_enable_update_and_requires_guard(self):
        from web_operation_agent_yudao.ui_source import UiSource
        self.config = self._native_form_config(update=False)
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy"):
            host = self.host()
            self.assertFalse(host._application(None).ports.forms.goal_updates)
            self.config = replace(self.config, native_read_policy=None)
            with self.assertRaisesRegex(ValueError, "verified guard"):
                self.host()

    def test_duplicate_or_changed_create_review_fails_before_opening_host(self):
        from web_operation_agent_yudao.ui_source import UiSource
        self.config = self._native_form_config(update=False)
        self.config = replace(self.config, native_create_policies=self.config.native_create_policies * 2)
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource()):
            with self.assertRaisesRegex(ValueError, "unique entry references"):
                self.host()
            self.config = replace(self.config, native_create_policies=self.config.native_create_policies[:1])
            (self.config.source_root / "ui/src/views/crm/customer/CustomerForm.vue").write_text("changed")
            with self.assertRaisesRegex(ValueError, "current source"):
                self.host()

    def test_failed_native_ingress_probe_prevents_host_startup(self):
        from web_operation_agent_yudao.ui_source import UiSource
        from web_operation_agent_yudao.page_guard import GuardRefused
        policy = Path(self.tmp.name) / "reviewed-reads.json"
        policy.write_text(json.dumps({"schema_version": 1, "release": "fixture-release",
            "read_paths": ["/admin-api/crm/customer/get"]}))
        self.config = replace(self.config, native_read_policy=policy, native_guard_port=0,
            ui_source=Path(self.tmp.name), source_release="fixture-release")
        with patch("web_operation_agent_yudao.native_server.UiSource.read", return_value=UiSource(
                records={"/crm/customer": "/crm/customer/detail/{id}"})), patch(
                "web_operation_agent_yudao.guard_proxy.NativeGuardProxy") as proxy:
            proxy.return_value.start.return_value.verify_ingress.side_effect = GuardRefused("guard_ingress_unverified")
            with self.assertRaises(GuardRefused): self.host()
            proxy.return_value.start.return_value.close.assert_called_once()
            self.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
