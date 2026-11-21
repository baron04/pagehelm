"""Phase 51 neutral contract proofs on Minimal Contacts and core compiled writes."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from testapps.contacts.adapter import ContactsModelGoalPort, MinimalContactsAdapter, contacts_profile
from testapps.contacts.app import DEMO_ACTORS, DEMO_SESSION_COOKIE, MinimalContactsApplication
from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.compiled_writes import CompiledCreateHandler, CompiledCreateSpec
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.convention_writes import ConventionFieldSpec
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.business.tools.contracts import BusinessMutationSpec, ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.composition import bind_application
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal.domain_packs import (
    DomainToolpack,
    build_switch_domain_schema,
    resolve_domain_pack,
    select_active_domains,
)
from web_operation_agent.goal.skill_writes import SkillWrites
from web_operation_agent.skills import SkillArtifact, SkillRegistry
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.state.ledger import SessionLedger
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


class _ScriptedChat:
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


class _DummyPort:
    def verify_actor(self, context) -> str:
        return "7"

    def create_specs(self) -> dict[str, CompiledCreateSpec]:
        return {
            "records": CompiledCreateSpec(
                entity="records",
                label="Record",
                fields=(
                    ConventionFieldSpec("parentId", "integer", "Parent", required=True, reference_entity="parents"),
                    ConventionFieldSpec("childPlanId", "integer", "Child Plan", required=True, reference_entity="child_plans"),
                    ConventionFieldSpec("memo", "string", "Memo", required=True),
                ),
            )
        }

    def encode_create_field(self, spec, f_spec, raw_val, *, actor_id, resolved_fields=None):
        resolved = dict(resolved_fields or {})
        if f_spec.name == "parentId":
            if str(raw_val) == "Parent-A":
                return 10, "Parent-A (ID=10)", {"kind": "relation", "entity": "parents", "id": 10, "name": "Parent-A"}
            raise BusinessToolRequestError("unknown parent")
        if f_spec.name == "childPlanId":
            if resolved.get("parentId") != 10:
                raise BusinessToolRequestError("missing parentId context for child plan lookup")
            if str(raw_val) in {"第一期", "第1期", "1", "501"}:
                return 501, "Plan #1 (ID=501)", {"kind": "relation", "entity": "child_plans", "id": 501, "name": "501"}
            raise BusinessToolRequestError("unknown child plan")
        return str(raw_val), str(raw_val), None


class Phase51ContractTests(unittest.TestCase):
    def setUp(self):
        self.app = MinimalContactsApplication().start()
        self.addCleanup(self.app.close)

    def _run_contacts(
        self,
        replies: list[dict],
        *,
        page_path: str = "/contacts",
        resident_convention_skill: bool = False,
        hub_routes: frozenset[str] = frozenset(),
        deferred_dom_tools: frozenset[str] = frozenset(),
        suppress_system_operation_catalog: bool = False,
    ):
        actor = DEMO_ACTORS[0]
        adapter = MinimalContactsAdapter(
            self.app.origin,
            api_token=actor.token,
            allow_mutations=True,
            mutation_journal=MemoryMutationJournal(),
            standard_skills=True,
            convention_updates=True,
            resident_convention_skill=resident_convention_skill,
        )
        principal = adapter.identity.bind(
            adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}"),
            delegation_id="p51-proof",
        )
        channel = _Channel(page_url=self.app.origin + page_path, principal=principal, decision="approved")
        goal_port = ContactsModelGoalPort(adapter, updates=True)
        chat = _ScriptedChat(replies)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit_path = Path(tmp.name) / "audit.jsonl"
        executor = ModelDrivenTurnExecutor(
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
            profile=contacts_profile(
                adapter,
                goal_port,
                hub_routes=hub_routes,
                deferred_dom_tools=deferred_dom_tools,
                suppress_system_operation_catalog=suppress_system_operation_catalog,
            ),
            control=ReadOnlyControl(),
            verify=False,
            run_id_factory=lambda _m: "p51-proof",
        )
        with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
            result = executor.execute({
                "message_id": "m1",
                "text": "Create a note titled 'Quarterly check' with body 'Follow up' and check contact creation.",
                "page_url": self.app.origin + page_path,
            })
        events = [json.loads(line) for line in audit_path.read_text().splitlines()]
        return result, chat, events, channel

    def test_resident_convention_skill_zero_step0_audit_and_tail_dynamic_skill(self):
        result, chat, events, channel = self._run_contacts([
            _tool_reply(("load_skill", {"skill": "contact-maintenance"})),
            _tool_reply((
                "create_record",
                {
                    "entity": "contact-notes",
                    "fields": [
                        {"field": "title", "value": "Quarterly check"},
                        {"field": "body", "value": "Follow up"},
                    ],
                },
            )),
            _tool_reply(("finish", {"status": "done", "answer": "Added note 'Follow up' for Ada Lovelace."})),
        ], resident_convention_skill=True)
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        names1 = [t["function"]["name"] for t in chat.calls[0][1]]
        names2 = [t["function"]["name"] for t in chat.calls[1][1]]
        self.assertIn("create_record", names1)
        self.assertNotIn("create_contact", names1)
        fin_idx = names1.index("finish")
        self.assertEqual(names2[: fin_idx + 1], names1[: fin_idx + 1])
        self.assertEqual(names2[fin_idx + 1 :], ["create_contact"])
        self.assertEqual(len(channel.approvals), 1)
        loaded_skills = [e.get("skill_id") for e in events if e.get("event") == "business_skill_loaded"]
        self.assertEqual(loaded_skills, ["minimal.contacts.contact-maintenance"])
        self.assertNotIn("minimal.contacts.record-convention-maintenance", loaded_skills)

    def test_contacts_convention_port_validate_create_payload_blocks_archived_before_approval(self):
        result, chat, events, channel = self._run_contacts([
            _tool_reply((
                "create_record",
                {
                    "entity": "contact-notes",
                    "fields": [
                        {"field": "title", "value": "Archived attempt"},
                        {"field": "category", "value": "archived"},
                    ],
                },
            )),
            _tool_reply(("finish", {"status": "cannot", "answer": "Archived category is disallowed."})),
        ], resident_convention_skill=True)
        self.assertEqual(result.terminal_reason, "agent_refused", result.user_answer)
        self.assertEqual(channel.approvals, [])
        self.assertFalse(any(e.get("event") == "approval_requested" for e in events))
        self.assertIn("archived", json.dumps(chat.calls[1][0], ensure_ascii=False))

    def test_hub_route_defers_dom_tools_until_navigation_keeping_stable_prefix(self):
        result, chat, events, _channel = self._run_contacts(
            [
                _tool_reply(("click", {"index": 0})),
                _tool_reply(("goto", {"path": "/contacts"})),
                _tool_reply(("finish", {"status": "done", "answer": "Navigated to contacts workspace."})),
            ],
            page_path="/contacts/workflows",
            resident_convention_skill=True,
            hub_routes=frozenset({"/contacts/workflows"}),
            deferred_dom_tools=frozenset({"click", "highlight"}),
            suppress_system_operation_catalog=True,
        )
        self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
        sys_prompt = chat.calls[0][0][0]["content"]
        self.assertNotIn("已安装的写入操作（* 为必填参数）", sys_prompt)
        names_hub = [t["function"]["name"] for t in chat.calls[0][1]]
        names_page = [t["function"]["name"] for t in chat.calls[2][1]]
        self.assertNotIn("click", names_hub)
        self.assertNotIn("highlight", names_hub)
        fin_idx = names_hub.index("finish")
        self.assertEqual(names_page[: fin_idx + 1], names_hub[: fin_idx + 1])
        self.assertEqual(set(names_page[fin_idx + 1 :]), {"click", "highlight"})
        deferred_results = [
            e for e in events if e.get("event") == "model_loop_result" and e.get("error_code") == "hub_route_deferred"
        ]
        self.assertEqual(len(deferred_results), 1)

    def test_domain_toolpack_routing_and_switch_schema(self):
        packs = (
            DomainToolpack("billing", "Billing", aliases=("invoice", "账单"), route_prefixes=("/billing",), default_priority=1),
            DomainToolpack("orders", "Orders", aliases=("order", "订单"), route_prefixes=("/orders",), default_priority=2),
            DomainToolpack("catalog", "Catalog", aliases=("product",), route_prefixes=("/catalog",), co_activate_domains=("inventory",)),
            DomainToolpack("inventory", "Inventory", aliases=("stock",), route_prefixes=("/inventory",)),
        )
        selected = select_active_domains(packs, page_url="https://app.test/#/orders/list?tab=1", prompt="Check invoice", top_k=2)
        self.assertEqual([p.domain_id for p in selected], ["orders", "billing"])
        pinned = select_active_domains(packs, page_url="https://app.test/#/catalog", prompt="Hi", pinned_domains=("billing",), top_k=2)
        self.assertEqual([p.domain_id for p in pinned], ["billing", "catalog", "inventory"])
        self.assertIs(resolve_domain_pack(packs, "账单"), packs[0])
        schema = build_switch_domain_schema(packs)
        self.assertEqual(schema["function"]["parameters"]["properties"]["domain"]["enum"], ["billing", "orders", "catalog", "inventory"])

    def test_create_record_description_and_load_skill_fallbacks(self):
        from web_operation_agent.business.compiled_writes import compiled_write_tool_definitions

        defs = {d.name: d for d in compiled_write_tool_definitions(_DummyPort(), scope="demo.write", entity_example="records")}
        self.assertIn("绑定本轮同账号已核实的目标实体记录", defs["create_record"].description)
        self.assertIn("非外键标量属性不跨记录借用", defs["create_record"].description)

        adapter = MinimalContactsAdapter(
            self.app.origin,
            api_token=DEMO_ACTORS[0].token,
            allow_mutations=True,
            mutation_journal=MemoryMutationJournal(),
            standard_skills=True,
            convention_updates=True,
            resident_convention_skill=True,
        )
        principal = adapter.identity.bind(
            adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={DEMO_ACTORS[0].session}"),
            delegation_id="p51-load-fallback",
        )
        channel = _Channel(page_url=self.app.origin + "/contacts", principal=principal, decision="approved")
        goal_port = ContactsModelGoalPort(adapter, updates=True)
        base_profile = contacts_profile(adapter, goal_port)
        from dataclasses import replace as _dc_replace

        profile = _dc_replace(base_profile, resolve_unknown_skill=lambda raw, _s, _t: f"domain-fallback:{raw}")
        chat = _ScriptedChat([
            _tool_reply(("load_skill", {"skill": "create_contact"})),
            _tool_reply(("load_skill", {"skill": "contacts-domain"})),
            _tool_reply(("finish", {"status": "done", "answer": "Loaded skill and resolved domain fallback."})),
        ])
        with tempfile.TemporaryDirectory() as td:
            executor = ModelDrivenTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Contacts",
                    ledger_path=Path(td) / "audit.jsonl",
                    require_identity=True,
                    deployment_profile="test",
                    allow_business_mutations=True,
                ),
                application=bind_application(adapter),
                chat=chat,
                goal_port=goal_port,
                profile=profile,
                control=ReadOnlyControl(),
                verify=False,
                run_id_factory=lambda _m: "p51-load-fallback",
            )
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                res = executor.execute({
                    "message_id": "m2",
                    "text": "Help check contact skill loading.",
                    "page_url": self.app.origin + "/contacts",
                })
        self.assertEqual(res.terminal_reason, "agent_finished")
        self.assertIn("domain-fallback:contacts-domain", chat.calls[2][0][-1]["content"])
        self.assertIn("已加载 Skill", chat.calls[1][0][-1]["content"])

    def test_compiled_create_fk_provenance_vs_scalar_blocking_and_sibling_deferral(self):
        handler = CompiledCreateHandler(_DummyPort())
        inputs = {
            "entity": "records",
            "fields": [
                {"field": "childPlanId", "value": "第一期"},
                {"field": "parentId", "value": "Parent-A"},
                {"field": "memo", "value": "Quarterly check"},
            ],
        }
        spec = handler.prepare(inputs)
        self.assertEqual(spec.args["payload"], {"childPlanId": 501, "parentId": 10, "memo": "Quarterly check"})

        with tempfile.TemporaryDirectory() as td:
            now = 1_700_000_000_000
            principal = PrincipalContext(1, "demo", "1", "7", "9", "synthetic", now - 1000, now + 60000, ("demo.write",))
            ledger = SessionLedger(Path(td) / "a.jsonl", "run")
            approvals = ApprovalWatch(ledger=ledger, run_id="run", principal=principal, session_id="s", turn_id="t",
                                      business_approval=lambda *a, **kw: None, cancelled=lambda: False)
            tools = ToolRegistry((
                ToolDefinition(
                    "create_record", "Create record",
                    json.dumps({
                        "type": "object",
                        "properties": {
                            "entity": {"type": "string", "minLength": 1, "maxLength": 64},
                            "fields": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "field": {"type": "string", "minLength": 1, "maxLength": 64},
                                        "value": {"type": "string", "maxLength": 200},
                                    },
                                    "required": ["field", "value"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["entity", "fields"],
                        "additionalProperties": False,
                    }),
                    "write", "demo.write", ToolOperation("records", "create"), handler,
                ),
            ))
            skills = SkillRegistry((SkillArtifact.from_files("demo", {
                "SKILL.md": b"---\nname: recs\ndescription: records\n---\nBody\n",
                "agents/woa.toml": b'schema_version = 1\nwrite_operations = ["create_record"]\n',
            }),))
            from web_operation_agent.business.runtime import BusinessToolRuntime
            from web_operation_agent.composition.business_operations import RegisteredOperations
            from types import SimpleNamespace as NS

            runtime = BusinessToolRuntime.from_registry(tools, application_id="demo", skill_registry=skills, require_skill_for_writes=True)
            app = NS(business_tools=runtime, ports=NS(operations=RegisteredOperations(
                tools, skills, lambda _e: None, default_loaded_skill_ids=frozenset({"demo.recs"})
            )))
            writes = SkillWrites(app, approvals)
            self.assertEqual(writes.loaded_ids, {"demo.recs"})
            self.assertEqual([d.name for d in writes.resident_definitions()], ["create_record"])
            from web_operation_agent.business.tools.contracts import BusinessMutationProposal

            fake_prop = BusinessMutationProposal(
                tool="create_record",
                args=inputs,
                effect="New record",
                targets=("records",),
                nonce="0" * 32,
                operation_context_digest="a" * 64,
                details=(),
                effect_projection=spec.effect_projection,
            )
            handler.validate_provenance(
                inputs,
                ("Create a record for Parent-A first period with memo Quarterly check.",),
                fake_prop,
                existing_values={"child_plans": ("501", "1"), "parents": ("Parent-A",), "memo": ("leaked-scalar",)},
            )
            bad_scalar = {
                "entity": "records",
                "fields": [
                    {"field": "childPlanId", "value": "501"},
                    {"field": "parentId", "value": "Parent-A"},
                    {"field": "memo", "value": "leaked-scalar"},
                ],
            }
            with self.assertRaises(BusinessToolRequestError):
                handler.validate_provenance(
                    bad_scalar,
                    ("Create a record for Parent-A",),
                    fake_prop,
                    existing_values={"child_plans": ("501",), "parents": ("Parent-A",), "memo": ("leaked-scalar",)},
                )

    def test_intent_alignment_multi_error_d1_d2_enum_and_aggregate_guidance(self):
        from dataclasses import dataclass as _dc
        from web_operation_agent.business.compiled_writes import (
            CompiledAggregateUpdateHandler,
            CompiledAggregateUpdateSpec,
            CompiledChildCollectionSpec,
            CompiledFieldDefault,
            _CANDIDATE_ID_SUFFIX,
        )
        from web_operation_agent.business.tools.contracts import BusinessMutationProposal

        m = _CANDIDATE_ID_SUFFIX.fullmatch("Ada Lovelace（employeeId=112）")
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), ("Ada Lovelace", "112"))

        class _GateHandler:
            def validate_intent_alignment(self, inputs, texts):
                joined = "\n".join(texts)
                if "downstream" in joined and "prerequisite" not in joined:
                    raise BusinessToolRequestError("cannot create prerequisite on downstream request")

            def execute(self, args, *, context=None):
                raise BusinessToolRequestError("write only")

            def prepare(self, inputs, *, context=None):
                return BusinessMutationSpec(
                    {"ok": True},
                    "Create prerequisite",
                    ("prereq",),
                    reconcile_key="k1",
                    reconciliation={"entity": "prereq"},
                )

            def dispatch(self, args, *, context=None):
                return {"id": 1}

            def verify_readback(self, args, result):
                return None

        with tempfile.TemporaryDirectory() as td:
            now = 1_700_000_000_000
            principal = PrincipalContext(1, "demo", "1", "7", "9", "synthetic", now - 1000, now + 60000, ("demo.write",))
            ledger = SessionLedger(Path(td) / "gate.jsonl", "run")
            asked_calls: list[str] = []
            approvals = ApprovalWatch(
                ledger=ledger,
                run_id="run",
                principal=principal,
                session_id="s",
                turn_id="t",
                business_approval=lambda *a, **kw: asked_calls.append("asked"),
                cancelled=lambda: False,
            )
            gate_tool = ToolDefinition(
                "prereq_create",
                "Create prerequisite",
                json.dumps({"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False}),
                "write",
                "demo.write",
                ToolOperation("prereq", "create"),
                _GateHandler(),
            )
            tools = ToolRegistry((gate_tool,))
            skills = SkillRegistry((SkillArtifact.from_files("demo", {
                "SKILL.md": b"---\nname: prereq\ndescription: prereq\n---\nBody\n",
                "agents/woa.toml": b'schema_version = 1\nwrite_operations = ["prereq_create"]\n',
            }),))
            from web_operation_agent.business.runtime import BusinessToolRuntime
            from web_operation_agent.composition.business_operations import RegisteredOperations
            from types import SimpleNamespace as NS

            runtime = BusinessToolRuntime.from_registry(tools, application_id="demo", skill_registry=skills, require_skill_for_writes=True)
            app = NS(
                business_tools=runtime,
                ports=NS(operations=RegisteredOperations(tools, skills, lambda _e: None, default_loaded_skill_ids=frozenset({"demo.prereq"}))),
            )
            writes = SkillWrites(app, approvals)
            out = writes.propose("prereq_create", {"note": "downstream missing plan"}, step=1, texts=("handle downstream missing plan",))
            self.assertEqual(out.state, "refused")
            self.assertFalse(out.asked)
            self.assertEqual(asked_calls, [])
            refused_evs = [e for e in ledger.events if e.get("event") == "skill_operation_refused"]
            self.assertTrue(refused_evs)
            self.assertFalse(refused_evs[-1].get("asked", False))

        class _RichPort(_DummyPort):
            def create_specs(self):
                return {
                    "employees": CompiledCreateSpec(
                        entity="employees",
                        label="Employee",
                        fields=(
                            ConventionFieldSpec("name", "string", "Name", required=True),
                            ConventionFieldSpec("ownerId", "integer", "Owner", reference_entity="actors"),
                            ConventionFieldSpec("status", "integer", "Status", required=True, enum_type="emp_status"),
                            ConventionFieldSpec("entryDate", "integer", "EntryDate", date_kind="epoch_ms"),
                        ),
                        defaults=(CompiledFieldDefault("ownerId", "actor", None),),
                    )
                }

            def aggregate_update_specs(self):
                return {
                    "salary/info": CompiledAggregateUpdateSpec(
                        entity="salary/info",
                        label="Salary Info",
                        fields=(ConventionFieldSpec("remark", "string", "Remark"),),
                        collections=(
                            CompiledChildCollectionSpec(
                                "salaryOptions",
                                "Salary Options",
                                (ConventionFieldSpec("name", "string", "Option Name"), ConventionFieldSpec("value", "number", "Amount")),
                            ),
                        ),
                        id_field="employeeId",
                    )
                }

            def actor_provenance_tokens(self):
                return ("DemoOwner",)

            def actor_reference_entities(self):
                return ("actors",)

            def session_anchor_dates(self):
                return ("2026-10-25", "1792886400000")

            def enum_provenance_tokens(self, f_spec, raw_val):
                if f_spec.enum_type == "emp_status" and str(raw_val) in {"1", "Formal"}:
                    return ("1", "Formal")
                return ()

            def encode_create_field(self, spec, f_spec, raw_val, *, actor_id, resolved_fields=None):
                if f_spec.name == "status":
                    return 1, "Formal", {"kind": "enum"}
                if f_spec.name == "ownerId":
                    return 7, "DemoOwner (ID=7)", None
                if f_spec.name == "entryDate":
                    return 1792886400000, "2026-10-25 00:00:00", None
                return str(raw_val), str(raw_val), None

            def encode_create_default(self, spec, f_spec, d_spec, *, actor_id):
                return 7, "DemoOwner (ID=7)", None

            def resolve_target(self, entity, target, *, spec):
                return {"employeeId": 112, "remark": "old", "salaryOptions": [{"id": 1, "name": "Base", "value": 10000}]}

            def encode_field(self, spec, f_spec, raw_val, cur_row):
                val = int(raw_val) if f_spec.kind == "integer" else (float(raw_val) if f_spec.kind == "number" else str(raw_val))
                old_v = cur_row.get(f_spec.name, "")
                return val, str(old_v), str(val), None

        rich_port = _RichPort()
        c_handler = CompiledCreateHandler(rich_port)
        with self.assertRaises(BusinessToolRequestError) as multi_ctx:
            c_handler.prepare({"entity": "employees", "fields": [{"field": "badCol", "value": "x"}]})
        self.assertIn("badCol", str(multi_ctx.exception))
        self.assertIn("Name", str(multi_ctx.exception))
        self.assertIn("Status", str(multi_ctx.exception))

        create_inputs = {
            "entity": "employees",
            "fields": [
                {"field": "name", "value": "Ada"},
                {"field": "ownerId", "value": "DemoOwner"},
                {"field": "status", "value": "1"},
                {"field": "entryDate", "value": 1792886400000},
            ],
        }
        cspec = c_handler.prepare(create_inputs)
        cprop = BusinessMutationProposal(
            tool="create_record",
            args=create_inputs,
            effect="Create",
            targets=("employees",),
            nonce="0" * 32,
            operation_context_digest="a" * 64,
            details=(),
            effect_projection=cspec.effect_projection,
        )
        c_handler.validate_provenance(create_inputs, ("Create Formal employee Ada today",), cprop)

        agg_handler = CompiledAggregateUpdateHandler(rich_port)
        with self.assertRaises(BusinessToolRequestError) as agg_ctx:
            agg_handler.prepare({
                "entity": "salary/info",
                "target": "Ada（employeeId=112）",
                "changes": [{"field": "regularSalary", "value": 15000}],
            })
        msg = str(agg_ctx.exception)
        self.assertIn("line_changes", msg)
        self.assertIn("salaryOptions", msg)
        self.assertIn("Amount", msg)

        # Arithmetic derivation without SQL receipt gives clear SQL calculation guidance
        agg_args = {
            "entity": "salary/info",
            "target": "Ada（employeeId=112）",
            "line_changes": [{"collection": "salaryOptions", "match": "Base", "changes": [{"field": "value", "value": 11500}]}],
        }
        agg_spec = agg_handler.prepare(agg_args)
        agg_prop = BusinessMutationProposal(
            tool="update_record",
            args=agg_args,
            effect="Update",
            targets=("salary/info",),
            nonce="0" * 32,
            operation_context_digest="a" * 64,
            details=(),
            effect_projection=agg_spec.effect_projection,
        )
        with self.assertRaises(BusinessToolRequestError) as arith_prov:
            agg_handler.validate_provenance(
                agg_prop.args,
                ("给员工调薪，基本工资加 1500",),
                agg_prop,
                existing_values={"__receipt__": ()},
            )
        self.assertIn("SQL 计算回执", str(arith_prov.exception))
        # Succeeds once SQL computation receipt contains 11500
        agg_handler.validate_provenance(
            agg_prop.args,
            ("给员工调薪，基本工资加 1500",),
            agg_prop,
            existing_values={"__receipt__": ("11500",)},
        )

    def test_validate_create_payload_hook_refuses_before_approval(self):
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler, CompiledCreateSpec
        from web_operation_agent.business.convention_writes import ConventionFieldSpec
        from web_operation_agent.business.errors import BusinessToolRequestError

        dispatches: list[dict] = []

        class _PrecheckPort:
            def verify_actor(self, context):
                return "9"

            def create_specs(self):
                return {
                    "contact-notes": CompiledCreateSpec(
                        entity="contact-notes",
                        label="Contact note",
                        fields=(
                            ConventionFieldSpec("title", "string", label="Title", required=True),
                            ConventionFieldSpec("category", "string", label="Category", required=True),
                        ),
                    )
                }

            def encode_field(self, spec, field, raw_val, cur_rec):
                return str(raw_val), "", str(raw_val), None

            def validate_create_payload(self, spec, payload, *, bindings=None, actor_id=""):
                if payload.get("category") == "archived":
                    raise BusinessToolRequestError("archived category refused in precheck")

            def dispatch_create(self, spec, payload):
                dispatches.append(dict(payload))
                return "1"

        handler = CompiledCreateHandler(_PrecheckPort())
        with self.assertRaises(BusinessToolRequestError) as ctx:
            handler.prepare(
                {
                    "entity": "contact-notes",
                    "fields": [
                        {"field": "title", "value": "Follow up"},
                        {"field": "category", "value": "archived"},
                    ],
                }
            )
        self.assertIn("archived category refused in precheck", str(ctx.exception))
        self.assertEqual(dispatches, [])


if __name__ == "__main__":
    unittest.main()
