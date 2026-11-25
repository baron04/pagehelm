"""Core verification suite for the declarative application onboarding SDK."""
from __future__ import annotations

from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from web_operation_agent.schemas.browser import ElementRef, PageObservation
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.dispatch_evidence import mutation_evidence
from web_operation_agent.business.errors import (
    BusinessMutationBlockedError,
    BusinessToolIdentityError,
    BusinessToolPermissionError,
)
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.composition import bind_application
from testapps.contacts.declarative import (
    DeclarativeAppSpec,
    DeclarativeApplicationAdapter,
    DeclarativeIdentityPort,
    DeclarativeModelGoalPort,
    FRONTEND_PRESETS,
    PageSpec,
    ResolvedActor,
    SubmitSpec,
    TypedWriteSpec,
    build_declarative_profile,
    entities_from_openapi,
)
from testapps.contacts.declarative.declarative_client import DeclarativeRestClient
from testapps.contacts.declarative.declarative_writes import DeclarativeSubmitPort, DeclarativeTypedWriteHandler
from web_operation_agent.execution.events import TurnEvents
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import LoopToolResult
from web_operation_agent.execution.native_submit import NativeSubmitMixin
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.execution.submit_contract import SubmitConventions
from web_operation_agent.state.identity import (
    IdentityAssertion,
    OperationContext,
    PRINCIPAL_CONTEXT_VERSION,
    PrincipalError,
)
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser


_SAMPLE_OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Warehouse Ledger", "version": "1.0.0"},
    "paths": {
        "/api/items": {
            "get": {
                "x-woa-entity": "items",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": ["id", "name"],
                                                "properties": {
                                                    "id": {"type": "integer", "title": "ID"},
                                                    "name": {"type": "string", "title": "Item Name", "maxLength": 80},
                                                    "qty": {"type": "integer", "title": "Quantity", "minimum": 0},
                                                    "active": {"type": "boolean", "title": "Active"},
                                                    "unit_cost": {"type": "number", "title": "Unit Cost"},
                                                    "notes": {"type": "string", "title": "Notes"},
                                                    "api_secret": {"type": "string", "title": "Secret"},
                                                },
                                            },
                                        }
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/items/{id}": {
            "get": {
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "item": {
                                            "type": "object",
                                            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                                        }
                                    },
                                }
                            }
                        },
                    }
                }
            },
            "put": {"responses": {"200": {"description": "Updated"}}},
        },
    },
}


class _WarehouseServer:
    def __init__(self, *, fault_on_create: bool = False):
        self.fault_on_create = fault_on_create
        self.items = [
            {
                "id": 101,
                "name": "Sensor Array",
                "qty": 12,
                "active": True,
                "unit_cost": 19.5,
                "notes": None,
                "extra_backend_meta": {"tier": 2},
                "password_hash": "leak-me",
            },
            {
                "id": 102,
                "name": "Torque Wrench",
                "qty": 0,
                "active": False,
                "unit_cost": 0.0,
                "notes": "calibrated",
                "extra_backend_meta": {"tier": 1},
                "password_hash": "leak-2",
            },
        ]
        self.puts: list[dict] = []
        self.posts: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return None

            def _send(self, code: int, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _token_kind(self) -> str:
                auth = self.headers.get("Authorization", "")
                if auth == "Bearer tok-warehouse":
                    return "writer"
                if auth == "Bearer tok-reader":
                    return "reader"
                return ""

            def do_GET(self):
                if not self._token_kind():
                    self._send(401, {"error": "unauthorized"})
                    return
                if self.path == "/healthz":
                    self._send(200, {"ready": True})
                    return
                if self.path == "/api/items":
                    self._send(200, {"items": outer.items})
                    return
                if self.path.startswith("/api/items/"):
                    raw_id = self.path.rsplit("/", 1)[-1]
                    try:
                        rid = int(raw_id)
                    except ValueError:
                        self._send(404, {"error": "not_found"})
                        return
                    found = next((r for r in outer.items if r["id"] == rid), None)
                    if found is None:
                        self._send(404, {"error": "not_found"})
                    else:
                        self._send(200, {"item": dict(found)})
                    return
                self._send(404, {"error": "unknown"})

            def do_POST(self):
                kind = self._token_kind()
                if not kind:
                    self._send(401, {"error": "unauthorized"})
                    return
                if kind != "writer":
                    self._send(403, {"error": "forbidden"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.posts.append(payload)
                if outer.fault_on_create:
                    self._send(500, {"error": "transport_ambiguous"})
                    return
                new_id = 100 + len(outer.items) + 1
                rec = {
                    "id": new_id,
                    "name": payload["name"],
                    "qty": int(payload.get("qty", 1)),
                    "active": True,
                    "unit_cost": 1.0,
                    "notes": None,
                }
                outer.items.append(rec)
                self._send(201, {"item": rec})

            def do_PUT(self):
                kind = self._token_kind()
                if not kind:
                    self._send(401, {"error": "unauthorized"})
                    return
                if kind != "writer":
                    self._send(403, {"error": "forbidden"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.puts.append(payload)
                raw_id = self.path.rsplit("/", 1)[-1]
                try:
                    rid = int(raw_id)
                except ValueError:
                    self._send(404, {"error": "not_found"})
                    return
                for rec in outer.items:
                    if rec["id"] == rid:
                        rec.update(payload)
                        self._send(200, {"item": dict(rec)})
                        return
                self._send(404, {"error": "not_found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def _tool_reply(*calls):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"c_{idx}",
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


class _DirectLoopExecutor(ModelDrivenTurnExecutor):
    direct_skill_operations = True


def _warehouse_spec(
    actor_ok: bool = True,
    *,
    create_spec: TypedWriteSpec | None = None,
    skills_dir: Path | None = None,
    with_delegated_resolver: bool = True,
    same_origin_shared_session: bool = False,
    delegated_actor_id: str = "op-1",
    delegated_perms: frozenset[str] = frozenset({"items:read", "items:write"}),
    page_perms: frozenset[str] = frozenset({"items:read", "items:write"}),
) -> DeclarativeAppSpec:
    # No custom row_fetcher here: DeclarativeReadExecutor.fetch_rows exercises
    # DeclarativeRestClient.get_collection natively across int/float/bool/None/extra/secret fields.
    entities = entities_from_openapi(
        _SAMPLE_OPENAPI,
        entity_overrides={
            "items": {
                "list_tool_name": "list_items",
                "record_reader": lambda ex, rid: ex.client.get_record(
                    f"/api/items/{rid}", detail_key="item"
                ),
            }
        },
        default_read_scope="items:read",
        default_write_scope="items:write",
    )
    return DeclarativeAppSpec(
        application_id="warehouse-app",
        display_name="Warehouse Ledger",
        entities=entities,
        pages=(PageSpec(key="items", label="Items", route="/items", entity="items", subject_reference="items"),),
        page_resolver=lambda cred: ResolvedActor("op-1", page_perms)
        if actor_ok and cred == "sid=ok"
        else (_ for _ in ()).throw(PrincipalError("invalid credential")),
        delegated_resolver=(
            (lambda: ResolvedActor(delegated_actor_id, delegated_perms))
            if with_delegated_resolver
            else None
        ),
        same_origin_shared_session=same_origin_shared_session,
        navigation_mode=FRONTEND_PRESETS["element-plus"].navigation_mode,
        skills_dir=skills_dir,
        write_specs=(create_spec,) if create_spec else (),
        default_read_scope="items:read",
        default_write_scope="items:write",
    )


def _op_ctx(principal) -> OperationContext:
    return OperationContext.create(
        principal,
        session_id="sess-sdk",
        turn_id="turn-sdk",
        operation_id="op-sdk",
        deadline_ms=principal.expires_at,
    )


class DeclarativeSDKTests(unittest.TestCase):
    def test_openapi_strips_secrets_and_pairs_update_endpoint(self):
        entities = entities_from_openapi(
            _SAMPLE_OPENAPI,
            default_read_scope="items:read",
            default_write_scope="items:write",
        )
        self.assertEqual(len(entities), 1)
        items = entities[0]
        field_names = {f.name for f in items.fields}
        self.assertEqual(field_names, {"id", "name", "qty", "active", "unit_cost", "notes"})
        self.assertNotIn("api_secret", field_names)
        self.assertTrue(items.convention_update)
        self.assertEqual(items.detail_endpoint_template, "/api/items/{id}")
        self.assertEqual(items.update_endpoint_template, "/api/items/{id}")
        self.assertEqual(items.detail_key, "item")
        self.assertEqual(FRONTEND_PRESETS["element-plus"].helpers_dir, "frontend/src/integrations/element-plus")

    def test_identity_port_fail_closed_permissions_and_bounded_caches(self):
        self.assertEqual(ResolvedActor("u-1").permissions, frozenset())
        self.assertEqual(ResolvedActor("u-1", None).permissions, frozenset())

        port_empty = DeclarativeIdentityPort(
            application_id="warehouse-app",
            page_resolver=lambda cred: ResolvedActor("u-1") if cred.startswith("ok") else (_ for _ in ()).throw(PrincipalError("bad")),
            same_origin_shared_session=True,
            required_scopes=frozenset({"items:read"}),
        )
        page = port_empty.page_identity("ok-0")
        with self.assertRaises(PrincipalError):
            port_empty.bind(page, delegation_id="d-empty")

        for i in range(200):
            port_empty.page_identity(f"ok-{i}")
        self.assertLessEqual(len(port_empty._page_assertions), 128)

        create_spec = TypedWriteSpec(
            name="create_item",
            description="Create a warehouse item.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            entity="items",
            operation_action="create",
            required_scope="items:write",
            normalizer=lambda args: {"name": args["name"].strip(), "qty": int(args.get("qty", 1))},
            effect_summary="Create item record",
            targets_fn=lambda p: (f"items:{p['name']}",),
            reconcile_key_fn=lambda p: json.dumps({"name": p["name"]}, sort_keys=True),
            dispatch_fn=lambda ex, p: str((ex.client.post_mutation("/api/items", p, record_key="item").get("item") or {}).get("id") or ""),
            readback_fn=lambda ex, p, rid: ex.client.get_record(f"/api/items/{rid}", detail_key="item") or {},
        )
        with _WarehouseServer() as srv:
            live_perms = {"items": frozenset({"items:read"})}
            ro_spec = DeclarativeAppSpec(
                application_id="warehouse-app",
                display_name="Warehouse Ledger",
                entities=_warehouse_spec().entities,
                pages=_warehouse_spec().pages,
                page_resolver=lambda cred: ResolvedActor("op-1", frozenset({"items:read", "items:write"}))
                if cred == "sid=ok"
                else (_ for _ in ()).throw(PrincipalError("invalid credential")),
                delegated_resolver=lambda: ResolvedActor("op-1", live_perms["items"]),
                write_specs=(create_spec,),
                default_read_scope="items:read",
                default_write_scope="items:write",
            )
            adapter = DeclarativeApplicationAdapter(
                ro_spec,
                srv.origin,
                api_token="tok-warehouse",
                allow_mutations=True,
                mutation_journal=MemoryMutationJournal(),
            )
            ro_principal = adapter.identity.bind(adapter.identity.page_identity("sid=ok"), delegation_id="d-ro")
            self.assertEqual(ro_principal.scopes, ("items:read",))
            ro_ctx = _op_ctx(ro_principal)
            with self.assertRaises(PrincipalError):
                adapter.identity.verify_actor_for_write(ro_ctx, required_scope="items:write")
            handler = adapter.business_tools._executor._write_handlers["create_item"]
            with self.assertRaises(PrincipalError):
                handler.execute_verified({"name": "Denied", "qty": 1}, context=ro_ctx)

            # Even if bind saw items:write, live re-verification rejects once delegated_resolver revokes write scope.
            live_perms["items"] = frozenset({"items:read", "items:write"})
            rw_principal = adapter.identity.bind(adapter.identity.page_identity("sid=ok"), delegation_id="d-rw")
            live_perms["items"] = frozenset({"items:read"})
            with self.assertRaises(BusinessToolPermissionError):
                adapter.identity.verify_actor_for_write(_op_ctx(rw_principal), required_scope="items:write")
            with self.assertRaises(BusinessToolPermissionError):
                handler.execute_verified({"name": "Denied", "qty": 1}, context=_op_ctx(rw_principal))
            self.assertEqual(len(srv.posts), 0)

    def test_identity_port_dual_credential_and_same_origin_shared_session(self):
        with self.assertRaises(ValueError):
            DeclarativeIdentityPort(
                application_id="warehouse-app",
                page_resolver=lambda _c: ResolvedActor("u-1", frozenset({"items:read"})),
                has_delegated_credential=True,
                required_scopes=frozenset({"items:read"}),
            )
        with self.assertRaises(ValueError):
            DeclarativeApplicationAdapter(
                _warehouse_spec(with_delegated_resolver=False),
                "http://127.0.0.1:9",
                api_token="tok-warehouse",
            )

        dual_mismatch = DeclarativeIdentityPort(
            application_id="warehouse-app",
            page_resolver=lambda _c: ResolvedActor("u-1", frozenset({"items:read", "items:write"})),
            delegated_resolver=lambda: ResolvedActor("u-2", frozenset({"items:read", "items:write"})),
            has_delegated_credential=True,
            required_scopes=frozenset({"items:read", "items:write"}),
        )
        with self.assertRaises(PrincipalError):
            dual_mismatch.bind(dual_mismatch.page_identity("c"), delegation_id="d-mismatch")

        dual_missing_scope = DeclarativeIdentityPort(
            application_id="warehouse-app",
            page_resolver=lambda _c: ResolvedActor("u-1", frozenset({"items:read", "items:write"})),
            delegated_resolver=lambda: ResolvedActor("u-1", frozenset({"items:read"})),
            has_delegated_credential=True,
            required_scopes=frozenset({"items:read", "items:write"}),
        )
        principal = dual_missing_scope.bind(dual_missing_scope.page_identity("c"), delegation_id="d-ro-del")
        self.assertEqual(principal.scopes, ("items:read",))
        with self.assertRaises(PrincipalError):
            dual_missing_scope.verify_actor_for_write(_op_ctx(principal), required_scope="items:write")

        with self.assertRaises(ValueError):
            DeclarativeApplicationAdapter(
                _warehouse_spec(with_delegated_resolver=False, same_origin_shared_session=True),
                "http://127.0.0.1:9",
                api_token="tok-warehouse",
            )

        active_sessions = {"sid=ok"}
        shared = DeclarativeIdentityPort(
            application_id="warehouse-app",
            page_resolver=lambda cred: ResolvedActor("u-1", frozenset({"items:read", "items:write"}))
            if cred in active_sessions
            else (_ for _ in ()).throw(PrincipalError("session revoked")),
            same_origin_shared_session=True,
            required_scopes=frozenset({"items:read", "items:write"}),
        )
        now = int(time.time() * 1000)
        forged_assertion = IdentityAssertion(
            schema_version=PRINCIPAL_CONTEXT_VERSION,
            source="page_session",
            application_id="warehouse-app",
            tenant_id="default",
            workspace_id="default",
            actor_id="u-1",
            issued_at=now,
            expires_at=now + 60_000,
        )
        with self.assertRaises(PrincipalError):
            shared.bind(forged_assertion, delegation_id="d-forged")

        valid_assertion = shared.page_identity("sid=ok")
        bound = shared.bind(valid_assertion, delegation_id="d-shared")
        ctx = _op_ctx(bound)
        self.assertIn("items:write", shared.verify_live_actor(ctx, required_scope="items:read"))
        self.assertEqual(shared.verify_actor_for_write(ctx, required_scope="items:write"), "u-1")

        active_sessions.clear()
        with self.assertRaises(PrincipalError):
            shared.verify_live_actor(ctx, required_scope="items:read")
        with self.assertRaises(BusinessToolIdentityError):
            shared.verify_actor_for_write(ctx, required_scope="items:write")

    def test_model_loop_with_core_turn_tables_sql_and_finish_table_ref(self):
        with _WarehouseServer() as srv, tempfile.TemporaryDirectory() as tmp:
            adapter = DeclarativeApplicationAdapter(
                _warehouse_spec(),
                srv.origin,
                api_token="tok-warehouse",
            )
            page_id = adapter.identity.page_identity("sid=ok")
            principal = adapter.identity.bind(page_id, delegation_id="del-sql")
            ctx = _op_ctx(principal)

            ent_res = adapter.business_tools._executor.execute("discover_entities", {"query": "items"}, context=ctx)
            eid = ent_res.content["candidates"][0]["entity_id"]
            desc_res = adapter.business_tools._executor.execute("describe_entity", {"entity_id": eid}, context=ctx)
            field_ids = [f["field_id"] for f in desc_res.content["fields"]]
            queried = adapter.business_tools._executor.execute(
                "query_entity_records",
                {"entity_id": eid, "field_ids": field_ids},
                context=ctx,
            )
            self.assertEqual(queried.content["completeness"]["matched_count"], 2)
            by_label = {item["label"]: item["value"] for item in queried.content["records"][1]["values"]}
            self.assertEqual(by_label["Quantity"], "0")
            self.assertEqual(by_label["Active"], "False")
            rendered_query = json.dumps(queried.content, ensure_ascii=False)
            self.assertNotIn("password_hash", rendered_query)
            self.assertNotIn("leak-me", rendered_query)
            self.assertNotIn("extra_backend_meta", rendered_query)

            goal_port = DeclarativeModelGoalPort(adapter)
            profile = build_declarative_profile(adapter, goal_port, enable_sql=True, today="2026-10-20")
            chat = _ScriptedChat([
                _tool_reply(("list_items", {})),
                _tool_reply(("sql", {"query": "SELECT id, name, qty, active FROM t1 ORDER BY qty DESC"})),
                _tool_reply(("finish", {"status": "done", "answer": "", "table_ref": "q1"})),
            ])
            channel = _Channel(page_url=srv.origin + "/items", principal=principal)
            executor = ModelDrivenTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Items",
                    ledger_path=Path(tmp) / "audit.jsonl",
                    require_identity=True,
                    deployment_profile="test",
                ),
                application=bind_application(adapter),
                chat=chat,
                goal_port=goal_port,
                profile=profile,
                control=ReadOnlyControl(),
                verify=False,
                run_id_factory=lambda _m: "sql-sdk",
                turn_traces=True,
            )
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                result = executor.execute({
                    "message_id": "m-sql",
                    "text": "List items ordered by quantity descending.",
                    "page_url": srv.origin + "/items",
                })
            self.assertEqual(result.terminal_reason, "agent_finished", result.user_answer)
            trace_text = list((Path(tmp) / "turns").glob("*.json"))[0].read_text()
            self.assertNotIn("password_hash", trace_text)
            self.assertNotIn("leak-me", trace_text)
            delivery = json.loads(trace_text)["server_deliveries"][0]
            self.assertEqual(delivery["projection"]["total_rows"], 2)
            self.assertEqual(delivery["structured_table"]["columns"], ["id", "name", "qty", "active"])
            self.assertEqual(delivery["structured_table"]["row_count"], 2)
            self.assertIn("Sensor Array", delivery["rendered_text"])
            table_msg = next(p["table"] for kind, p, _ in channel.published if kind == "progress" and isinstance(p, dict) and "table" in p)
            self.assertEqual(str(table_msg["rows"][0][0]), "101")
            self.assertEqual(str(table_msg["rows"][1][0]), "102")

    def test_convention_update_and_typed_write_indeterminate(self):
        create_spec = TypedWriteSpec(
            name="create_item",
            description="Create a warehouse item.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            entity="items",
            operation_action="create",
            required_scope="items:write",
            normalizer=lambda args: {"name": args["name"].strip(), "qty": int(args.get("qty", 1))},
            effect_summary="Create item record",
            targets_fn=lambda p: (f"items:{p['name']}",),
            reconcile_key_fn=lambda p: json.dumps({"name": p["name"]}, sort_keys=True),
            dispatch_fn=lambda ex, p: str((ex.client.post_mutation("/api/items", p, record_key="item").get("item") or {}).get("id") or ""),
            readback_fn=lambda ex, p, rid: ex.client.get_record(f"/api/items/{rid}", detail_key="item") or {},
        )
        with _WarehouseServer(fault_on_create=True) as srv, tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            pkg = skills_dir / "item-create"
            (pkg / "agents").mkdir(parents=True)
            (pkg / "SKILL.md").write_text("---\nname: item-create\ndescription: Create warehouse items.\n---\nCreate one verified item.\n", encoding="utf-8")
            (pkg / "agents" / "woa.toml").write_text('schema_version = 1\nscope_entities = ["items"]\nwrite_operations = ["create_item"]\n', encoding="utf-8")
            adapter = DeclarativeApplicationAdapter(
                _warehouse_spec(create_spec=create_spec, skills_dir=skills_dir),
                srv.origin,
                api_token="tok-warehouse",
                allow_mutations=True,
                mutation_journal=MemoryMutationJournal(),
                standard_skills=True,
                convention_updates=True,
            )
            principal = adapter.identity.bind(adapter.identity.page_identity("sid=ok"), delegation_id="del-w")
            goal_port = DeclarativeModelGoalPort(adapter, updates=True)
            profile = build_declarative_profile(adapter, goal_port)

            chat_upd = _ScriptedChat([
                _tool_reply(("propose_operation", {
                    "operation": "update_record",
                    "inputs": {"entity": "items", "target": "Torque Wrench", "changes": {"name": "Torque Wrench Pro", "qty": 9}},
                })),
                _tool_reply(("finish", {"status": "done", "answer": "Updated item name and quantity."})),
            ])
            channel_upd = _Channel(page_url=srv.origin + "/items", principal=principal, decision="approved")
            exec_upd = _DirectLoopExecutor(
                channel=channel_upd,
                config=AgentTurnConfig(supported_scope="Items", ledger_path=Path(tmp) / "u.jsonl", require_identity=True, deployment_profile="test", allow_business_mutations=True),
                application=bind_application(adapter),
                chat=chat_upd,
                goal_port=goal_port,
                profile=profile,
                control=ReadOnlyControl(),
                verify=False,
                run_id_factory=lambda _m: "upd-sdk",
            )
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                res_upd = exec_upd.execute({"message_id": "m-upd", "text": "Rename Torque Wrench to Torque Wrench Pro and set qty 9.", "page_url": srv.origin + "/items"})
            self.assertEqual(res_upd.terminal_reason, "agent_finished", res_upd.user_answer)
            self.assertEqual(len(channel_upd.approvals), 1)
            self.assertEqual(srv.items[1]["name"], "Torque Wrench Pro")
            self.assertEqual(srv.items[1]["qty"], 9)

            chat_ind = _ScriptedChat([
                _tool_reply(("propose_operation", {"operation": "create_item", "inputs": {"name": "Laser Gauge", "qty": 3}})),
                _tool_reply(("finish", {"status": "cannot", "answer": "Outcome is uncertain; please verify manually."})),
            ])
            channel_ind = _Channel(page_url=srv.origin + "/items", principal=principal, decision="approved")
            exec_ind = _DirectLoopExecutor(
                channel=channel_ind,
                config=AgentTurnConfig(supported_scope="Items", ledger_path=Path(tmp) / "i.jsonl", require_identity=True, deployment_profile="test", allow_business_mutations=True),
                application=bind_application(adapter),
                chat=chat_ind,
                goal_port=goal_port,
                profile=profile,
                control=ReadOnlyControl(),
                verify=False,
                run_id_factory=lambda _m: "ind-sdk",
            )
            with patch("web_operation_agent.goal.executor.RemoteBrowserClient", _RemoteBrowser):
                res_ind = exec_ind.execute({"message_id": "m-ind", "text": "Create item Laser Gauge qty 3.", "page_url": srv.origin + "/items"})
            self.assertEqual(len(srv.posts), 1)
            self.assertEqual(res_ind.terminal_reason, "indeterminate_write")
            self.assertEqual(res_ind.status.value, "indeterminate")
            audit_rows = [json.loads(line) for line in (Path(tmp) / "i.jsonl").read_text().splitlines()]
            self.assertTrue(
                any(
                    r.get("event") == "business_mutation_indeterminate"
                    or (r.get("event") == "business_mutation_result" and r.get("outcome") == "indeterminate")
                    for r in audit_rows
                )
            )

    def test_write_guards_single_flight_and_mutation_transport(self):
        with _WarehouseServer() as srv:
            reentered_exc: list[BusinessMutationBlockedError] = []
            handler_holder: list[DeclarativeTypedWriteHandler] = []

            def reentrant_dispatch(ex, payload):
                try:
                    handler_holder[0].execute_verified(payload, context=ctx)
                except BusinessMutationBlockedError as exc:
                    reentered_exc.append(exc)
                res = ex.client.post_mutation("/api/items", payload, record_key="item")
                return str(res["item"]["id"])

            create_spec = TypedWriteSpec(
                name="create_item",
                description="Create a warehouse item.",
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                entity="items",
                operation_action="create",
                required_scope="items:write",
                normalizer=lambda args: {"name": args["name"].strip(), "qty": int(args.get("qty", 1))},
                effect_summary="Create item record",
                targets_fn=lambda p: (f"items:{p['name']}",),
                reconcile_key_fn=lambda p: json.dumps({"name": p["name"]}, sort_keys=True),
                dispatch_fn=reentrant_dispatch,
                readback_fn=lambda ex, p, rid: ex.client.get_record(f"/api/items/{rid}", detail_key="item") or {},
            )
            adapter = DeclarativeApplicationAdapter(
                _warehouse_spec(create_spec=create_spec),
                srv.origin,
                api_token="tok-warehouse",
                allow_mutations=True,
                mutation_journal=MemoryMutationJournal(),
            )
            principal = adapter.identity.bind(adapter.identity.page_identity("sid=ok"), delegation_id="d-sf")
            ctx = _op_ctx(principal)
            handler = adapter.business_tools._executor._write_handlers["create_item"]
            handler_holder.append(handler)
            result = handler.execute_verified({"name": "Calibrator", "qty": 2}, context=ctx)
            self.assertTrue(result.content["created"])
            self.assertEqual(len(reentered_exc), 1)
            self.assertEqual(reentered_exc[0].reason, "already_consumed")

            events = TurnEvents()
            proposal = SimpleNamespace(content_digest="put-digest")
            approval = SimpleNamespace(approval_id="appr-put")
            with events.turn("put-turn"):
                with mutation_evidence(proposal, approval, ctx) as ev_ok:
                    ev_ok.claimed()
                    writer_client = DeclarativeRestClient(srv.origin, api_token="tok-warehouse")
                    updated = writer_client.put_once("/api/items/101", {"name": "Sensor Array v2"})
                    self.assertEqual(updated["item"]["name"], "Sensor Array v2")
                with self.assertRaises(BusinessToolPermissionError):
                    with mutation_evidence(proposal, approval, ctx) as ev_denied:
                        ev_denied.claimed()
                        reader_client = DeclarativeRestClient(srv.origin, api_token="tok-reader")
                        reader_client.put_once("/api/items/101", {"name": "Forbidden"})
            receipts = events.snapshot("put-turn")["receipts"]
            self.assertEqual((receipts[0]["dispatch_state"], receipts[0]["dispatch_count"], receipts[0]["effect_status"]), ("dispatched", 1, "applied"))
            self.assertEqual((receipts[1]["dispatch_state"], receipts[1]["dispatch_count"], receipts[1]["effect_status"]), ("dispatched", 1, "rejected"))

    def test_declarative_submit_port_downgrades_page_form_to_prefill_handoff(self):
        spec = SubmitSpec(
            prefix="/api/items",
            collection_key="items",
            conventions=SubmitConventions(
                volatile_keys=frozenset({"modified"}),
                record_key="id",
                line_key="lineId",
                name_key="name",
                epoch_zone=timezone.utc,
                decode_write=lambda status, _b: (status, None),
            ),
            row_action_method="POST",
            row_action_template="/api/items/{id}/archive",
        )
        port = DeclarativeSubmitPort(spec, client=None)
        self.assertFalse(port.allow_form_submit)
        port.note_read("/api/items")
        self.assertEqual(port.prefixes(port.recent_reads(), mode="update", record_id="101", source="/api/items"), ())

        save_btn = ElementRef("e2", "button", "保存", "button", 1, {"form_scope": "dialog:edit", "form_instance": "i1"})
        obs = PageObservation(
            schema_version=1,
            epoch=1,
            url="https://app.example.test/items",
            title="Items",
            landmarks=["Items"],
            elements=[save_btn],
            content="Items",
            render_state="ready",
            page_route="/items",
        )

        class _SubmitHost(NativeSubmitMixin):
            def __init__(self):
                self.native_submit = port
                self.highlight_calls: list[tuple[str, dict]] = []
                self.form_submit_calls = 0

            def _submit_allowed(self, turn):
                return True

            def _element(self, s, idx):
                return save_btn if idx == 2 else None

            def _invoke_tool(self, s, name, args, turn):
                self.highlight_calls.append((name, dict(args)))
                return LoopToolResult("Highlighted.", kind="page", state="completed", marked="保存")

            def _submit_form(self, s, element, turn):
                self.form_submit_calls += 1
                raise AssertionError("page form should never call _submit_form when allow_form_submit=False")

            def _page_fingerprint(self, cur_obs):
                return f"fp:{cur_obs.epoch}"

        host = _SubmitHost()
        turn: dict = {}
        state = SimpleNamespace(obs=obs, step=0, ledger=SimpleNamespace(append=lambda *a, **kw: None))
        outcome = host._request_submit(state, {"index": 2}, turn)
        self.assertEqual((outcome.kind, outcome.state, outcome.marked), ("page", "completed", "保存"))
        self.assertEqual(host.highlight_calls, [("highlight", {"index": 2})])
        self.assertEqual(host.form_submit_calls, 0)


if __name__ == "__main__":
    unittest.main()
