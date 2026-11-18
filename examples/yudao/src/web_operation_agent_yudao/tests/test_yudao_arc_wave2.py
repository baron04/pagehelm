"""Unit tests for Wave 2 Yudao ARC-09 (concurrent reads) and ARC-12 (check specs & telemetry)."""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from web_operation_agent.composition.binding import BoundApplication
from web_operation_agent.composition.descriptor import ApplicationDescriptor
from web_operation_agent.execution.answer_check import delivery_correction
from web_operation_agent.execution.budget import TurnBudget
from web_operation_agent.execution.checks import CHECK_OUTCOMES, ProgramCheckSpec
from web_operation_agent.execution.harness import HarnessTurnContext
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.turn_trace import TurnTrace
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexPage
from web_operation_agent.goal.ports import Candidate
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.schemas.browser import PageObservation
from web_operation_agent.state.conversation import ConversationState
from web_operation_agent.state.identity import (
    PRINCIPAL_CONTEXT_VERSION,
    PrincipalContext,
)
from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
from web_operation_agent_yudao.executor.slim_loop import (
    YUDAO_CHECK_SPECS,
    ActorKnowledge,
    YudaoTurnExecutor,
)
from web_operation_agent_yudao.reads import bound_data_reads
from web_operation_agent_yudao.reads.bound_data_reads import _TURN_ACTOR_LOCAL
from web_operation_agent_yudao.writes.write_intent import WriteIntent
from web_operation_agent_yudao.tests.slim_loop_support import (
    DOCS,
    Budget,
    Channel,
    Client,
    Ledger,
    Script,
    call,
    element,
    observation,
    reply,
)


def _normalize_receipts(text: str) -> str:
    return re.sub(r"ev_[0-9a-f]{12}_(\d+)", r"ev_<HASH>_\1", text)


def _normalize_tables(tables: list[dict]) -> list[dict]:
    return [
        {**t, "receipt": _normalize_receipts(str(t.get("receipt", "")))}
        for t in tables
    ]


def _tool_call(tool_name: str, args: dict) -> dict:
    if tool_name == "call_api" and isinstance(args.get("params"), dict):
        args = {**args, "params": json.dumps(args["params"], ensure_ascii=False)}
    return {
        "id": f"call-{tool_name}-{len(args)}",
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _descriptor() -> ApplicationDescriptor:
    return ApplicationDescriptor(
        schema_version=2,
        application_id="yudao-synthetic",
        display_name="Synthetic Yudao",
        origin="https://synthetic.test",
        navigation_mode="spa",
        page_controller_module="https://synthetic.test/controller.js",
        catalog_sha256="a" * 64,
    )


def _principal(actor_id: str = "1", workspace_id: str = "101", tenant_id: str = "tenant-1") -> PrincipalContext:
    now = int(time.time() * 1000)
    return PrincipalContext(
        schema_version=PRINCIPAL_CONTEXT_VERSION,
        application_id="yudao-synthetic",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        actor_id=actor_id,
        delegation_id="del-1",
        issued_at=now - 1_000,
        expires_at=now + 3_600_000,
        scopes=("yudao.read",),
    )


class _ConcurrentTestClient(Client):
    """Thread-safe synthetic Yudao client tracking actor-bound worker reads and optional delays."""

    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        deny_path: str = "",
        expected_actor_id: str = "1",
        expected_workspace_id: str = "101",
        tenant: str = "tenant-1",
    ):
        super().__init__()
        self._lock = threading.Lock()
        self.read_generation = 1
        self.tenant = tenant
        self.delay_s = delay_s
        self.deny_path = deny_path
        self.expected_actor_id = expected_actor_id
        self.expected_workspace_id = expected_workspace_id
        self.thread_ids: list[tuple[str, int]] = []
        self.actor_checks: list[tuple[str, str]] = []

    def read_window(self, check=None):
        return contextlib.nullcontext()

    def get(self, path, **params):
        with self._lock:
            self.thread_ids.append((path, threading.get_ident()))
        if self.delay_s and path in {"orders/page", "deals/page", "channels/page", "crm/customer/page"}:
            time.sleep(self.delay_s)
        if self.deny_path and path == self.deny_path:
            raise DataRefused(f"{path}: 403 forbidden", permission_denied=True)
        if path == "system/auth/get-permission-info":
            with self._lock:
                self.calls.append((path, dict(params)))
            return {
                "user": {
                    "id": int(self.expected_actor_id),
                    "deptId": int(self.expected_workspace_id),
                    "nickname": "合成用户",
                },
                "menus": [
                    {
                        "name": "销售",
                        "path": "/sale",
                        "children": [{"name": "订单", "path": "order", "component": "sale/order/index"}],
                    }
                ],
            }
        if path == "crm/customer/page":
            with self._lock:
                self.calls.append((path, dict(params)))
            rows = [{"id": 77, "name": "星河科技"}]
            if params.get("name"):
                rows = [r for r in rows if params["name"] in r["name"]]
            return {"total": len(rows), "list": rows}
        with self._lock:
            return super().get(path, **params)


class _FakeGoalData:
    """Minimal GoalData implementation proving actor binding on worker threads."""

    def __init__(self, client: _ConcurrentTestClient):
        self._client = client
        self.code_names = {}
        self.column_names = {}
        self.query_choices = {}
        self._docs = DOCS
        self._index = ApplicationIndex(
            entities=(
                IndexEntity(
                    key="crm/customer",
                    label="客户",
                ),
            ),
            pages=(IndexPage(key="crm_customer", label="客户列表", breadcrumb="CRM / 客户管理", entity="crm/customer"),),
            conventions=(),
        )

    def docs(self):
        return self._docs

    def index(self):
        return self._index

    def _display(self, entity: str, record: dict) -> str:
        return str(record.get("name") or "")

    def find(self, entity: str, query: str, *, limit: int = 8, context=None) -> tuple[Candidate, ...]:
        scope = getattr(_TURN_ACTOR_LOCAL, "scope", None)
        if not isinstance(scope, dict) or scope.get("client") is not self._client:
            raise AssertionError("worker thread did not bind turn actor scope")
        if scope.get("generation") != self._client.read_generation or scope.get("thread_id") != threading.get_ident():
            raise AssertionError("missing generation or thread_id in turn actor scope")
        if scope.get("binding") is None:
            raise AssertionError("missing actor binding in turn actor scope")
        if context is not None:
            self._client.actor_checks.append((context.principal.actor_id, context.principal.workspace_id))
        page = self._client.get("crm/customer/page", pageNo=1, pageSize=limit, name=query)
        rows = page["list"] if isinstance(page, dict) else []
        return tuple(
            Candidate(
                kind="record",
                entity=entity,
                display=str(row["name"]),
                id=str(row["id"]),
            )
            for row in rows
        )

    def read(self, entity: str, identifier: str, fields=()) -> Candidate:
        return Candidate(kind="record", entity=entity, id=str(identifier), display="星河科技")

    def record_address(self, entity: str, identifier: str) -> str:
        return f"/{entity}/detail/{identifier}"


class _FakeGoalPort:
    def __init__(self, client: _ConcurrentTestClient):
        self.data = _FakeGoalData(client)
        self.review = None

    def capabilities(self):
        return frozenset({"fill", "fields", "facets", "sections", "paging", "lines", "values", "handoffs"})

    def index(self, context=None):
        return self.data.index()

    def find(self, entity, query, *, limit=8, context=None):
        return self.data.find(entity, query, limit=limit)

    def records_named(self, name, *, context=None, entities=()):
        candidates = self.data.find("crm/customer", name, limit=8, context=context)
        if not candidates:
            return ()
        return (("crm/customer", tuple((c.id, c.display, f"/crm/customer/detail/{c.id}") for c in candidates)),)

    def record_address(self, entity, identifier, *, context=None):
        return self.data.record_address(entity, identifier)


class _TestBoundApplication(BoundApplication):
    def __init__(self, principal: PrincipalContext):
        super().__init__(
            descriptor=_descriptor(),
            business_tools=NS(
                tool_names=frozenset(),
                mutation_tool_names=frozenset(),
                required_scopes={},
                catalog_sha256="a" * 64,
                definitions=(),
            ),
            catalog={},
            ports=NS(
                routes=NS(session_handoff_query_keys=()),
                data=NS(get=lambda *a, **k: None),
                operations=None,
            ),
        )
        self._principal = principal

    def context_for(self, message, *, scope: str, operation_id: str):
        return NS(
            application_id=self.descriptor.application_id,
            principal=self._principal,
            delegation_id=self._principal.delegation_id,
            operation_id=operation_id,
            session_id="sess-1",
            turn_id="turn-1",
            scope=scope,
            require_live=lambda **_kwargs: None,
        )


class _TestChat(Script):
    model = "scripted/test"

    def complete(self, messages, tools, run_id, *, max_tokens=None, options=None, timeout_s=None):
        return self.complete_cancellable(
            messages, tools, run_id, cancelled=lambda: False,
            max_tokens=max_tokens, options=options, timeout_s=timeout_s,
        )


def _make_executor(
    client: _ConcurrentTestClient,
    *,
    replies: list[dict],
    principal: PrincipalContext | None = None,
    docs_callable=None,
    ledger_dir: str = "",
) -> tuple[YudaoTurnExecutor, Ledger, PrincipalContext, dict]:
    actor_principal = principal or _principal()
    goal_port = _FakeGoalPort(client)
    knowledge = ActorKnowledge(
        client,
        docs_callable or (lambda: DOCS),
        time_zone="Asia/Shanghai",
    )
    bound_app = _TestBoundApplication(actor_principal)
    ledger = Ledger()
    captured_tables: list[dict] = []

    executor = YudaoTurnExecutor(
        knowledge=knowledge,
        actor_name="合成用户",
        today="2026-10-19",
        time_zone="Asia/Shanghai",
        channel=Channel(),
        config=NS(
            allow_business_mutations=False,
            disabled_actions=frozenset(),
            max_user_answer_chars=4000,
            wall_budget_s=120.0,
            ledger_path=str(Path(ledger_dir) / "ledger.jsonl") if ledger_dir else "",
            source=NS(runtime_source=False),
        ),
        chat=_TestChat(*replies),
        application=bound_app,
        goal_port=goal_port,
        control=NS(run=lambda *a, **k: None),
        run_id_factory=lambda _m: "run-wave2",
        turn_traces=bool(ledger_dir),
        disabled_page_actions=frozenset({"submit"}),
    )
    return executor, ledger, actor_principal, captured_tables


class _TestBudget(Budget):
    @staticmethod
    def check():
        return None

    @staticmethod
    def step():
        return None


def _build_turn_state(
    executor: YudaoTurnExecutor,
    prompt: str,
    ledger: Ledger,
    principal: PrincipalContext,
    obs: PageObservation,
) -> HarnessTurnContext:
    state = HarnessTurnContext(
        message=NS(text=prompt, message_id="msg-1"),
        run_id="run-wave2",
        ledger=ledger,
        browser=NS(
            execute_with_budget=lambda action, args, timeout_s=0, expect_navigation=False: ActionReceipt(
                1, f"cmd-{action}", action, args, True, observation=obs, url_before=obs.url, url_after=obs.url,
            ),
        ),
        writes=NS(
            mutations_dispatched=lambda: 0,
            records=(),
            empty=lambda: True,
            begin_control=lambda: None,
            start_observing=lambda: None,
            expect_decision=lambda _b: None,
            write_outcome=lambda receipt, step, action_may_write=False: {
                "state": "none", "permitted": 0, "refused": 0,
                "permitted_requests": [], "refused_requests": [],
            },
        ),
        approvals=NS(
            mutations_enabled=False,
            principal=principal,
            start_turn=lambda _deadline_ms: None,
            settle_operation=lambda: None,
            context=lambda _step, name: executor.application.context_for(
                None, scope="yudao.read", operation_id=f"op-{name}",
            ),
        ),
        obs=obs,
        started_at=time.monotonic(),
        budget=TurnBudget(steps=16, seconds=120.0),
        chat=executor.chat,
        chinese=True,
    )
    state.measure = lambda _name: contextlib.nullcontext()
    state.audit = ledger.append
    state.progress = lambda _facts: None
    state.operation = executor.application.context_for(None, scope="yudao.read", operation_id="op-1")
    state.page = NS(
        checkpoint=lambda: None,
        action_observations=lambda: (),
        observe=lambda: obs,
        execute=lambda *a, **k: None,
    )
    return state


def _run_turn(executor: YudaoTurnExecutor, prompt: str, ledger: Ledger, principal: PrincipalContext) -> dict:
    obs = observation()
    state = _build_turn_state(executor, prompt, ledger, principal, obs)
    executor.conversation_state = ConversationState()
    from web_operation_agent_yudao.reads.turn_tables import TurnTables

    captured = {}
    orig_close = TurnTables.close

    def spy_close(self_tables):
        captured["tables"] = self_tables.describe().get("tables", [])
        return orig_close(self_tables)

    TurnTables.close = spy_close
    try:
        outcome = executor._run(state)
    finally:
        TurnTables.close = orig_close
    return {
        "outcome": outcome,
        "state": state,
        "harness": state,
        "trace": executor._trace,
        "messages": list(executor._messages),
        "tables": captured.get("tables", []),
        "events": list(ledger.events),
    }


class Arc09ConcurrentReadTests(unittest.TestCase):
    def test_only_explicit_pure_read_tools_expose_concurrent_prepare_commit(self):
        client = _ConcurrentTestClient()
        executor, _, _, _ = _make_executor(client, replies=[])
        by_name = {
            proc.schema["function"]["name"]: proc
            for proc in executor.profile.procedures
        }
        expected_concurrent = {"call_api", "find_record", "search_api", "entity_rules", "load_domain_guide"}
        for name, proc in by_name.items():
            has_prepare = callable(getattr(proc.handler, "prepare_read", None))
            has_commit = callable(getattr(proc.handler, "commit_read", None))
            if name in expected_concurrent:
                self.assertTrue(has_prepare and has_commit, f"{name} must expose prepare_read and commit_read")
            else:
                self.assertFalse(has_prepare or has_commit, f"{name} must stay strictly sequential")

    def test_three_delayed_reads_run_concurrently_and_match_sequential_byte_for_byte(self):
        delay = 0.16
        seq_client = _ConcurrentTestClient(delay_s=0.0)
        seq_replies = [
            reply(call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10})),
            reply(_tool_call("find_record", {"kind": "客户", "name": "星河科技"})),
            reply(call("call_api", path="deals/page", params={"pageNo": 1, "pageSize": 2})),
            reply(call("finish", status="cannot", answer="测试完成", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        seq_exec, seq_ledger, seq_principal, _ = _make_executor(seq_client, replies=seq_replies)
        seq_run = _run_turn(seq_exec, "查订单、客户星河科技和商机", seq_ledger, seq_principal)

        conc_client = _ConcurrentTestClient(delay_s=delay)
        conc_replies = [
            reply(
                call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10}),
                _tool_call("find_record", {"kind": "客户", "name": "星河科技"}),
                call("call_api", path="deals/page", params={"pageNo": 1, "pageSize": 2}),
            ),
            reply(call("finish", status="cannot", answer="测试完成", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        conc_exec, conc_ledger, conc_principal, _ = _make_executor(conc_client, replies=conc_replies)
        t0 = time.monotonic()
        conc_run = _run_turn(conc_exec, "查订单、客户星河科技和商机", conc_ledger, conc_principal)
        elapsed = time.monotonic() - t0

        # 3 delayed calls of 0.16s each must overlap: wall_time < 2 * single delay (0.32s)
        self.assertLess(elapsed, delay * 2.0, f"expected concurrent reads < {delay * 2.0:.3f}s, got {elapsed:.3f}s")

        # Worker threads must execute in distinct thread IDs from the main thread and bind actor context cleanly.
        main_tid = threading.get_ident()
        worker_tids = {tid for path, tid in conc_client.thread_ids if path in {"orders/page", "deals/page", "crm/customer/page"}}
        self.assertTrue(worker_tids - {main_tid}, "expected pure reads to execute in worker threads")
        self.assertEqual(conc_client.actor_checks, [("1", "101")])
        # Main-thread actor scope must not be clobbered after concurrent turn finishes.
        self.assertIsNone(getattr(_TURN_ACTOR_LOCAL, "scope", None))

        # Byte-for-byte parity of TurnTables (t1, t2), tool outputs, and ev_*_1..3 receipts.
        self.assertEqual([t["name"] for t in conc_run["tables"]], [t["name"] for t in seq_run["tables"]])
        self.assertEqual(_normalize_tables(conc_run["tables"]), _normalize_tables(seq_run["tables"]))

        seq_tool_msgs = [_normalize_receipts(m["content"]) for m in seq_run["messages"] if m["role"] == "tool"]
        conc_tool_msgs = [_normalize_receipts(m["content"]) for m in conc_run["messages"] if m["role"] == "tool"]
        self.assertEqual(conc_tool_msgs, seq_tool_msgs)
        self.assertIn("ev_<HASH>_1", conc_tool_msgs[0])
        self.assertIn("ev_<HASH>_3", conc_tool_msgs[1])
        self.assertIn("ev_<HASH>_4", conc_tool_msgs[2])

        self.assertEqual(conc_exec.knowledge.turn_targets, seq_exec.knowledge.turn_targets)
        self.assertEqual(conc_exec.knowledge.last_keys, seq_exec.knowledge.last_keys)

    def test_error_in_earlier_concurrent_read_commits_in_order_without_corrupting_state(self):
        # Case 1: non-terminal parameter error in call #1, valid calls #2 and #3 still become t1 and t2.
        seq_client = _ConcurrentTestClient()
        seq_replies = [
            reply(call("call_api", path="orders/page", params={"pageNo": 1, "badParam": "x"})),
            reply(call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10})),
            reply(call("call_api", path="deals/page", params={"pageNo": 1, "pageSize": 2})),
            reply(call("finish", status="cannot", answer="完成", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        seq_exec, seq_ledger, seq_principal, _ = _make_executor(seq_client, replies=seq_replies)
        seq_run = _run_turn(seq_exec, "查订单与商机", seq_ledger, seq_principal)

        conc_client = _ConcurrentTestClient()
        conc_replies = [
            reply(
                call("call_api", path="orders/page", params={"pageNo": 1, "badParam": "x"}),
                call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10}),
                call("call_api", path="deals/page", params={"pageNo": 1, "pageSize": 2}),
            ),
            reply(call("finish", status="cannot", answer="完成", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        conc_exec, conc_ledger, conc_principal, _ = _make_executor(conc_client, replies=conc_replies)
        conc_run = _run_turn(conc_exec, "查订单与商机", conc_ledger, conc_principal)

        self.assertEqual([t["name"] for t in conc_run["tables"]], ["t1", "t2"])
        self.assertEqual(_normalize_tables(conc_run["tables"]), _normalize_tables(seq_run["tables"]))
        seq_tool_msgs = [_normalize_receipts(m["content"]) for m in seq_run["messages"] if m["role"] == "tool"]
        conc_tool_msgs = [_normalize_receipts(m["content"]) for m in conc_run["messages"] if m["role"] == "tool"]
        self.assertEqual(conc_tool_msgs, seq_tool_msgs)
        self.assertNotIn("ev_<HASH>", conc_tool_msgs[0])
        self.assertIn("ev_<HASH>_1", conc_tool_msgs[1])
        self.assertIn("ev_<HASH>_3", conc_tool_msgs[2])

        # Case 2: permission_denied in call #1 commits cleanly without creating a table while call #2 still commits t1.
        seq_deny_client = _ConcurrentTestClient(deny_path="orders/page")
        seq_deny_replies = [
            reply(call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10})),
            reply(call("call_api", path="deals/page", params={"pageNo": 1, "pageSize": 2})),
            reply(call("finish", status="cannot", answer="订单接口无权限", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        seq_deny_exec, seq_deny_ledger, seq_deny_principal, _ = _make_executor(seq_deny_client, replies=seq_deny_replies)
        seq_deny_run = _run_turn(seq_deny_exec, "查订单与商机", seq_deny_ledger, seq_deny_principal)

        deny_client = _ConcurrentTestClient(deny_path="orders/page")
        deny_replies = [
            reply(
                call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10}),
                call("call_api", path="deals/page", params={"pageNo": 1, "pageSize": 2}),
            ),
            reply(call("finish", status="cannot", answer="订单接口无权限", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        deny_exec, deny_ledger, deny_principal, _ = _make_executor(deny_client, replies=deny_replies)
        deny_run = _run_turn(deny_exec, "查订单与商机", deny_ledger, deny_principal)
        self.assertEqual([t["name"] for t in deny_run["tables"]], ["t1"])
        self.assertEqual(_normalize_tables(deny_run["tables"]), _normalize_tables(seq_deny_run["tables"]))
        self.assertEqual(
            [_normalize_receipts(m["content"]) for m in deny_run["messages"] if m["role"] == "tool"],
            [_normalize_receipts(m["content"]) for m in seq_deny_run["messages"] if m["role"] == "tool"],
        )
        self.assertEqual(deny_exec.knowledge.turn_targets, seq_deny_exec.knowledge.turn_targets)

    def test_concurrent_metadata_and_lazy_load_rlock_are_thread_safe(self):
        load_count = 0
        load_lock = threading.Lock()

        def slow_docs():
            nonlocal load_count
            time.sleep(0.05)
            with load_lock:
                load_count += 1
            return DOCS

        client = _ConcurrentTestClient()
        replies = [
            reply(
                call("search_api", query="订单"),
                call("entity_rules", target="orders/page"),
                call("load_domain_guide", domain="crm"),
                call("call_api", path="orders/page", params={"pageNo": 1, "pageSize": 10}),
            ),
            reply(call("finish", status="cannot", answer="完成", items=[
                {"item": "查询", "kind": "data", "status": "undelivered", "receipts": []},
            ])),
        ]
        executor, ledger, principal, _ = _make_executor(client, replies=replies, docs_callable=slow_docs)
        self.assertIsInstance(executor.knowledge._loading, type(threading.RLock()))
        run = _run_turn(executor, "查订单规则与接口", ledger, principal)
        self.assertEqual(load_count, 1)
        self.assertEqual([t["name"] for t in run["tables"]], ["t1"])

    def test_worker_read_scope_binds_exact_actor_without_mutating_main_context(self):
        client = _ConcurrentTestClient(expected_actor_id="42", expected_workspace_id="700")
        principal = _principal(actor_id="42", workspace_id="700")
        executor, _, _, _ = _make_executor(client, replies=[], principal=principal)
        ctx = executor.turn_context
        op = executor.application.context_for(None, scope="yudao.read", operation_id="op-worker")

        with bound_data_reads.turn_actor_reads(client, turn_context=ctx):
            main_scope = _TURN_ACTOR_LOCAL.scope
            self.assertIs(ctx.actor_reads, main_scope)
            result = {}

            def worker():
                proc = next(
                    p.handler for p in executor.profile.procedures
                    if p.schema["function"]["name"] == "find_record"
                )
                prepared = proc.prepare_read(
                    {"kind": "客户", "name": "星河科技"},
                    observation=observation(),
                    operation=op,
                )
                result["prepared"] = prepared
                result["worker_scope_after_exit"] = getattr(_TURN_ACTOR_LOCAL, "scope", None)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
            self.assertIsNone(result["worker_scope_after_exit"])
            self.assertIs(_TURN_ACTOR_LOCAL.scope, main_scope)
            # Only find_record call + permission check happened cleanly as actor 42/700.
            self.assertIn(("42", "700"), client.actor_checks)
            # Even with pre-seeded scope, commit must run cleanly on main thread only.
            commit_res = proc_commit_helper(executor, op, result["prepared"])
            self.assertEqual(commit_res.state, "completed")
            self.assertIn("/crm/customer/detail/77", commit_res.feedback)


def proc_commit_helper(executor: YudaoTurnExecutor, op, prepared):
    proc = next(
        p.handler for p in executor.profile.procedures
        if p.schema["function"]["name"] == "find_record"
    )
    context = NS(operation=op, page=NS(checkpoint=lambda: None), progress=lambda *_a, **_kw: None)
    return proc.commit_read(prepared, {"kind": "客户", "name": "星河科技"}, context=context)


class Arc12YudaoChecksTests(unittest.TestCase):
    def test_yudao_check_specs_are_complete_and_valid(self):
        required = {
            "unbacked_business_claim",
            "verified_empty_filter_repeated",
            "draft_only",
            "dangerous_unconfirmed",
            "unrequested_draft_field",
            "draft_handoff_required",
            "delivery_missing_receipt",
            "delivery_foreign_receipt",
            "delivery_sensitive",
            "delivery_page_handoff",
            "delivery_page_home",
            "delivery_dialog_missing",
            "delivery_page_masked",
            "delivery_write_unverified",
            "delivery_page_obligation",
            "delivery_incomplete",
        }
        self.assertTrue(required <= set(YUDAO_CHECK_SPECS))
        for name, spec in YUDAO_CHECK_SPECS.items():
            self.assertIsInstance(spec, ProgramCheckSpec)
            self.assertEqual(spec.name, name)

    def test_direct_blocking_guards_emit_check_fired_and_record_trace_and_context(self):
        client = _ConcurrentTestClient()

        with tempfile.TemporaryDirectory() as tmp:
            # 1) unbacked_business_claim (ACC-07)
            ro_exec, ro_ledger, ro_principal, _ = _make_executor(
                client,
                ledger_dir=tmp,
                replies=[
                    reply(call("finish", status="done", answer="该商机当前已成交。", items=[])),
                    reply(call("finish", status="cannot", answer="未调用查询工具核实该商机状态", items=[
                        {"item": "状态", "kind": "data", "status": "undelivered", "receipts": []},
                    ])),
                ],
            )
            ro_run = _run_turn(ro_exec, "这个商机现在是什么状态", ro_ledger, ro_principal)
            self._assert_check_recorded(ro_run, "unbacked_business_claim", outcome="returned", answer_preserved=False)

            # 2) draft_only (in _invoke_tool and in _propose)
            draft_exec, draft_ledger, draft_principal, _ = _make_executor(
                client,
                ledger_dir=tmp,
                replies=[
                    reply(call("load_skill", skill="crm_contact")),
                    reply(call("finish", status="cannot", answer="只预填不提交", items=[
                        {"item": "预填", "kind": "page", "status": "undelivered", "receipts": []},
                    ])),
                ],
            )
            draft_run = _run_turn(draft_exec, "名称先填Synthetic，先不保存", draft_ledger, draft_principal)
            self._assert_check_recorded(draft_run, "draft_only", outcome="blocked", answer_preserved=False)

            # 3) dangerous_unconfirmed via _propose
            prop_exec, prop_ledger, _prop_principal, _ = _make_executor(client, replies=[])
            prop_state = NS(
                step=0,
                message=NS(text="关闭双11秒杀活动"),
                ledger=prop_ledger,
                checks=[],
            )
            prop_exec._trace = TurnTrace(Path(tmp), BASELINE_SESSION_HANDOFFS)
            prop_exec._earlier_words = []
            blocked = prop_exec._propose(prop_state, NS(), "promotion_seckill_activity_close", {"target": "双11"}, {})
            self.assertEqual((blocked.state, blocked.error_code), ("needs_input", "dangerous_unconfirmed"))
            self.assertIn(
                ("check_fired", {"check": "dangerous_unconfirmed", "outcome": "blocked", "attempt": 1, "answer_preserved": False}),
                prop_ledger.events,
            )
            self.assertEqual(prop_state.checks[0]["check"], "dangerous_unconfirmed")
            self.assertEqual(prop_exec._trace.checks[0]["check"], "dangerous_unconfirmed")

            # 4) unrequested_draft_field via fill_fields
            obs_form = PageObservation(
                1, 1, "https://synthetic.test/customer", "Customer", [],
                [
                    element(1, "textbox", "名称", "input", field_label="名称", form_scope="dialog:create", form_instance="f1", form_lifetime="l1"),
                    element(2, "textbox", "备注", "input", field_label="备注", form_scope="dialog:create", form_instance="f1", form_lifetime="l1"),
                ],
                content="[1]<input />\n[2]<input />",
                render_state="ready",
                page_route="/customer",
            )
            uf_exec, uf_ledger, uf_principal, _ = _make_executor(
                client,
                ledger_dir=tmp,
                replies=[
                    reply(call("fill_fields", fields=[{"label": "备注", "value": "私自补充"}])),
                    reply(call("finish", status="cannot", answer="未填写未请求字段", items=[
                        {"item": "预填", "kind": "page", "status": "undelivered", "receipts": []},
                    ])),
                ],
            )
            uf_run = self._run_turn_with_obs(uf_exec, "名称只填Synthetic，先不保存", uf_ledger, uf_principal, obs_form)
            self._assert_check_recorded(uf_run, "unrequested_draft_field", outcome="blocked", answer_preserved=False)

    def test_delivery_feedback_does_not_emit_duplicate_check_fired(self):
        client = _ConcurrentTestClient()
        exec_obj, ledger, principal, _ = _make_executor(
            client,
            replies=[
                reply(call("finish", status="done", answer="已处理", items=[
                    {"item": "写入", "kind": "write", "status": "delivered", "receipts": []},
                ])),
                reply(call("finish", status="cannot", answer="未实际写入", items=[
                    {"item": "写入", "kind": "write", "status": "undelivered", "receipts": []},
                ])),
            ],
        )
        run = _run_turn(exec_obj, "修改联系人名称", ledger, principal)
        check_events = [fields for event, fields in run["events"] if event == "check_fired"]
        rejected_events = [fields for event, fields in run["events"] if event == "model_delivery_rejected"]
        self.assertEqual(len(rejected_events), 1)
        expected = 1 if "record_check_fired" in inspect.getsource(delivery_correction) else 0
        self.assertEqual(len(check_events), expected)

    def _run_turn_with_obs(self, executor, prompt, ledger, principal, obs):
        state = _build_turn_state(executor, prompt, ledger, principal, obs)
        executor.conversation_state = ConversationState()
        executor._run(state)
        return {"events": list(ledger.events), "harness": state, "trace": executor._trace}

    def _assert_check_recorded(self, run: dict, check_name: str, *, outcome: str, answer_preserved: bool):
        check_events = [fields for event, fields in run["events"] if event == "check_fired" and fields["check"] == check_name]
        self.assertTrue(check_events, f"expected check_fired event for {check_name}, got {run['events']}")
        self.assertEqual(check_events[0]["outcome"], outcome)
        self.assertEqual(check_events[0]["answer_preserved"], answer_preserved)
        self.assertIn(outcome, CHECK_OUTCOMES)
        self.assertTrue(any(item["check"] == check_name for item in run["harness"].checks))
        self.assertTrue(any(item["check"] == check_name for item in run["trace"].checks))


if __name__ == "__main__":
    unittest.main()
