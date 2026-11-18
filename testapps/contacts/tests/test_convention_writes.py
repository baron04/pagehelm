"""Second-adapter proof of direct Skill operation proposals and flat convention writes."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import (
    DEMO_ACTORS,
    DEMO_SESSION_COOKIE,
    DEMO_WORKSPACE,
    OTHER_WORKSPACE,
    MinimalContactsApplication,
)
from testapps.contacts.model_loop import ContactsModelGoalPort, contacts_profile
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.composition import bind_application
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal.ports import capabilities_of
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser


def _tool_reply(*calls):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for idx, (name, args) in enumerate(calls, 1)
        ],
    }


class _ScriptedLoopChat:
    model = "scripted"
    provider = "synthetic"

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages, tools=None, run_id="", **kwargs):
        self.calls.append((list(messages), list(tools or ())))
        return self.replies.pop(0)

    def complete_cancellable(self, messages, tools=None, run_id="", *, cancelled=None, **kwargs):
        return self.complete(messages, tools, run_id, **kwargs)


class _DirectContactsExecutor(ModelDrivenTurnExecutor):
    direct_skill_operations = True


class ContactsConventionWriteTests(unittest.TestCase):
    def setUp(self):
        self.app = MinimalContactsApplication().start()
        self.addCleanup(self.app.close)

    def _executor(self, replies, *, actor_idx=0, decision="approved", on_approval=None):
        actor = DEMO_ACTORS[actor_idx]
        adapter = MinimalContactsAdapter(
            self.app.origin,
            api_token=actor.token,
            allow_mutations=True,
            mutation_journal=MemoryMutationJournal(),
            standard_skills=True,
            convention_updates=True,
        )
        principal = adapter.identity.bind(
            adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}"),
            delegation_id="conv-proof",
        )
        channel = _Channel(
            page_url=self.app.origin + "/contacts",
            principal=principal,
            decision=decision,
        )
        if on_approval is not None:
            orig_request = channel.request_approval

            def wrapped_request(request):
                orig_request(request)
                on_approval(request)

            channel.request_approval = wrapped_request
        goal_port = ContactsModelGoalPort(adapter, updates=True)
        chat = _ScriptedLoopChat(replies)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit_path = Path(tmp.name) / "audit.jsonl"
        executor = _DirectContactsExecutor(
            channel=channel,
            config=AgentTurnConfig(
                supported_scope="Contacts",
                ledger_path=audit_path,
                require_identity=True,
                deployment_profile="test",
                allow_business_mutations=True,
            ),
            application=bind_application(adapter),
            chat=chat,
            goal_port=goal_port,
            profile=contacts_profile(adapter, goal_port),
            control=ReadOnlyControl(),
            verify=False,
            run_id_factory=lambda _m: "conv-proof",
        )
        return executor, channel, chat, goal_port, audit_path

    def test_direct_propose_operation_auto_loads_skill_and_updates_flat_record_preserving_other_fields(self):
        executor, channel, chat, goal_port, audit_path = self._executor([
            _tool_reply((
                "propose_operation",
                {
                    "operation": "update_record",
                    "inputs": {
                        "entity": "contacts",
                        "target": "Ada Lovelace",
                        "changes": {"email": "ada.new@example.test"},
                    },
                },
            )),
            _tool_reply(("finish", {"status": "done", "answer": "Updated Ada Lovelace's email to ada.new@example.test."})),
        ])
        self.assertIn("updates", capabilities_of(goal_port))
        with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
            result = executor.execute({
                "message_id": "m1",
                "text": "Change Ada Lovelace's email to ada.new@example.test.",
                "page_url": self.app.origin + "/contacts",
            })
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        # First model call saw propose_operation and full operation catalog without load_skill
        first_sys = chat.calls[0][0][0]["content"]
        self.assertIn("update_record(", first_sys)
        self.assertIn("toggle_record_status(", first_sys)
        self.assertEqual(len(channel.approvals), 1)
        ada = next(c for c in self.app.workspaces[DEMO_WORKSPACE] if c["id"] == "ada-lovelace")
        self.assertEqual(ada, {"id": "ada-lovelace", "name": "Ada Lovelace", "email": "ada.new@example.test"})
        events = [json.loads(line)["event"] for line in audit_path.read_text().splitlines()]
        self.assertIn("business_skill_loaded", events)
        self.assertIn("business_mutation_result", events)

    def test_unsourced_value_is_refused_before_any_approval_card_or_write(self):
        executor, channel, _chat, _gp, _audit = self._executor([
            _tool_reply((
                "propose_operation",
                {
                    "operation": "update_record",
                    "inputs": {
                        "entity": "contacts",
                        "target": "Ada Lovelace",
                        "changes": [{"field": "email", "value": "invented@example.test"}],
                    },
                },
            )),
            _tool_reply(("finish", {"status": "need_input", "answer": "What email address should I set?"})),
        ])
        with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
            result = executor.execute({
                "message_id": "m2",
                "text": "Update Ada Lovelace's email address.",
                "page_url": self.app.origin + "/contacts",
            })
        self.assertEqual(result.terminal_reason, "needs_clarification")
        self.assertEqual(channel.approvals, [])
        self.assertEqual(self.app.write_requests, 0)

    def test_concurrent_record_drift_after_approval_is_refused_at_pre_dispatch_reread(self):
        def mutate_between_prepare_and_dispatch(_proposal):
            ada = next(c for c in self.app.workspaces[DEMO_WORKSPACE] if c["id"] == "ada-lovelace")
            ada["name"] = "Ada Byron"

        executor, channel, _chat, _gp, _audit = self._executor(
            [
                _tool_reply((
                    "propose_operation",
                    {
                        "operation": "update_record",
                        "inputs": {
                            "entity": "contacts",
                            "target": "Ada Lovelace",
                            "changes": [{"field": "email", "value": "ada.new@example.test"}],
                        },
                    },
                )),
            ],
            on_approval=mutate_between_prepare_and_dispatch,
        )
        with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
            result = executor.execute({
                "message_id": "m3",
                "text": "Change Ada Lovelace's email to ada.new@example.test.",
                "page_url": self.app.origin + "/contacts",
            })
        self.assertEqual(result.terminal_reason, "business_mutation_refused")
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(self.app.write_requests, 0)
        ada = next(c for c in self.app.workspaces[DEMO_WORKSPACE] if c["id"] == "ada-lovelace")
        self.assertEqual(ada["email"], "ada@example.test")

    def test_two_state_status_toggle_and_workspace_isolation(self):
        executor, channel, _chat, _gp, _audit = self._executor([
            _tool_reply((
                "propose_operation",
                {
                    "operation": "toggle_record_status",
                    "inputs": {
                        "entity": "contact-subscriptions",
                        "target": "Grace Hopper",
                        "status": "disabled",
                    },
                },
            )),
            _tool_reply(("finish", {"status": "done", "answer": "Disabled Grace Hopper's subscription."})),
        ])
        with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
            result = executor.execute({
                "message_id": "m4",
                "text": "Disable Grace Hopper's contact subscription.",
                "page_url": self.app.origin + "/contacts",
            })
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(self.app.subscriptions[DEMO_WORKSPACE]["grace-hopper"], "disabled")
        # Southwind workspace's Grace Hopper remains untouched
        self.assertEqual(self.app.subscriptions[OTHER_WORKSPACE]["grace-hopper"], "enabled")

    def test_append_preservation_succeeds_and_overwrite_on_append_request_refuses_before_card(self):
        # 1. Valid append preserving existing name suffix through full SkillWrites.propose chain
        executor, channel, _chat, _gp, _audit = self._executor([
            _tool_reply((
                "propose_operation",
                {
                    "operation": "update_record",
                    "inputs": {
                        "entity": "contacts",
                        "target": "Ada Lovelace",
                        "changes": [{"field": "name", "value": "Ada Lovelace (VIP)"}],
                    },
                },
            )),
            _tool_reply(("finish", {"status": "done", "answer": "Appended (VIP) to Ada Lovelace's name."})),
        ])
        with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
            result = executor.execute({
                "message_id": "m5",
                "text": "Keep Ada Lovelace and append (VIP) to the name.",
                "page_url": self.app.origin + "/contacts",
            })
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        self.assertEqual(len(channel.approvals), 1)
        ada = next(c for c in self.app.workspaces[DEMO_WORKSPACE] if c["id"] == "ada-lovelace")
        self.assertEqual(ada["name"], "Ada Lovelace (VIP)")

    def test_optional_bounds_omission_and_boolean_provenance_anchoring(self):
        from types import SimpleNamespace
        from web_operation_agent.business.convention_writes import (
            ConventionFieldSpec,
            ConventionUpdateHandler,
            ConventionUpdateSpec,
        )
        from web_operation_agent.business.errors import BusinessToolRequestError

        spec = ConventionUpdateSpec(
            entity="settings",
            label="Setting",
            fields=(
                ConventionFieldSpec("name", "string", label="Name", required=True),
                ConventionFieldSpec("fixedEndTerm", "integer", label="结束天数", required=False, minimum=1),
                ConventionFieldSpec("isDefault", "boolean", label="是否默认", required=False),
            ),
        )
        records = {1: {"id": 1, "name": "Alpha", "fixedEndTerm": 0, "isDefault": False}}
        dispatched: list[dict] = []

        class _Port:
            def update_specs(self):
                return {"settings": spec}

            def status_specs(self):
                return {}

            def verify_actor(self, context):
                return "actor-1"

            def resolve_target(self, entity, target, *, spec):
                return dict(records[1])

            def read_record(self, entity, record_id):
                return dict(records[1])

            def encode_field(self, _spec, field, raw_value, _cur):
                return raw_value, str(_cur.get(field.name)), str(raw_value), None

            def recheck_bindings(self, bindings, *, actor_id):
                pass

            def dispatch_update(self, _spec, record_id, payload):
                dispatched.append(dict(payload))
                records[1].update(payload)

            def dispatch_status(self, _spec, record_id, status_code):
                pass

            def invalidate_reads(self, entities):
                pass

        handler = ConventionUpdateHandler(_Port())

        # Explicitly violating minimum=1 refuses before card
        with self.assertRaises(BusinessToolRequestError):
            handler.prepare(
                {"entity": "settings", "target": "Alpha", "changes": [{"field": "fixedEndTerm", "value": 0}]},
            )

        # Untouched optional field with legacy 0 is omitted from payload while preserved in snapshot + readback
        mut = handler.prepare(
            {"entity": "settings", "target": "Alpha", "changes": [{"field": "name", "value": "Beta"}]},
        )
        self.assertNotIn("fixedEndTerm", mut.args["payload"])
        self.assertEqual(mut.args["snapshot"]["fixedEndTerm"], 0)
        res = handler.dispatch(mut.args)
        self.assertTrue(res.content["verified"])
        self.assertEqual(records[1]["fixedEndTerm"], 0)

        # Boolean provenance: reject negation contradiction and unrelatedprose with 是/不; accept exact matches
        def check_bool(raw_val: bool, prompt: str):
            records[1]["isDefault"] = not raw_val
            args = {"entity": "settings", "target": "Alpha", "changes": [{"field": "isDefault", "value": raw_val}]}
            spec_m = handler.prepare(args)
            handler.validate_provenance(args, (prompt,), SimpleNamespace(effect_projection=spec_m.effect_projection))

        for bad_val, bad_prompt in [
            (True, "不允许将 Alpha 设为启用"),
            (True, "但是只改备注，不要改其他"),
            (False, "但是只改备注，不要改其他"),
        ]:
            with self.assertRaises(BusinessToolRequestError, msg=f"Should refuse {bad_val!r} on {bad_prompt!r}"):
                check_bool(bad_val, bad_prompt)

        check_bool(True, "请启用该配置")
        check_bool(True, "将默认状态改为是")
        check_bool(False, "不允许开启该配置")
        check_bool(False, "将默认状态改为否")

    def test_indeterminate_dispatch_and_readback_never_retry_on_contacts_routes(self):
        from web_operation_agent.business.errors import BusinessMutationIndeterminate

        # 1. Convention update indeterminate dispatch stops as indeterminate_write with 1 dispatch
        executor, channel, _chat, _gp, _audit = self._executor([
            _tool_reply((
                "propose_operation",
                {
                    "operation": "update_record",
                    "inputs": {
                        "entity": "contacts",
                        "target": "Ada Lovelace",
                        "changes": [{"field": "email", "value": "ada.indet@example.test"}],
                    },
                },
            )),
        ])
        conv_handler = executor.application.ports.operations.tools.get("update_record").handler
        orig_dispatch_update = conv_handler.port.dispatch_update
        dispatch_calls = {"n": 0}

        def failing_dispatch(spec, record_id, payload):
            dispatch_calls["n"] += 1
            raise BusinessMutationIndeterminate("synthetic timeout", phase="dispatch")

        conv_handler.port.dispatch_update = failing_dispatch
        try:
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({
                    "message_id": "m6",
                    "text": "Change Ada Lovelace's email to ada.indet@example.test.",
                    "page_url": self.app.origin + "/contacts",
                })
        finally:
            conv_handler.port.dispatch_update = orig_dispatch_update
        self.assertEqual(result.terminal_reason, "indeterminate_write")
        self.assertEqual(len(channel.approvals), 1)
        self.assertEqual(dispatch_calls["n"], 1)

        # 2. Skill create_contact through write_pipeline readback mismatch never retries
        executor2, channel2, _chat2, _gp2, _audit2 = self._executor([
            _tool_reply((
                "propose_operation",
                {
                    "operation": "create_contact",
                    "inputs": {
                        "name": "Linus Torvalds",
                        "email": "linus@example.test",
                    },
                },
            )),
        ])
        create_handler = executor2.application.ports.operations.tools.get("create_contact").handler
        orig_post = create_handler.executor._post
        post_calls = {"n": 0}

        def post_with_readback_mismatch(path, payload):
            post_calls["n"] += 1
            res = orig_post(path, payload)
            # Mutate the created email on the server so same-actor readback disagrees
            for row in self.app.workspaces[DEMO_WORKSPACE]:
                if row["id"] == res["contact"]["id"]:
                    row["email"] = "tampered@example.test"
            return res

        create_handler.executor._post = post_with_readback_mismatch
        try:
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result2 = executor2.execute({
                    "message_id": "m7",
                    "text": "Create contact Linus Torvalds with email linus@example.test.",
                    "page_url": self.app.origin + "/contacts",
                })
        finally:
            create_handler.executor._post = orig_post
        self.assertEqual(result2.terminal_reason, "indeterminate_write")
        self.assertEqual(len(channel2.approvals), 1)
        self.assertEqual(post_calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
